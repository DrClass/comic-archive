from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import ScannedGroup, ScannedIssue, ScannedMedia, SuggestedRole
from .review import ReviewPlan, ReviewRole

SCHEMA_VERSION = 1


class StagingError(ValueError):
    pass


@dataclass(slots=True)
class StagedMedia:
    source_path: str
    relative_path: str
    mime_type: str
    media_kind: str
    size_bytes: int
    order: int


@dataclass(slots=True)
class StagedGroup:
    name: str
    relative_path: str
    role: str
    owner_type: str
    owner_key: str | None
    media: list[StagedMedia] = field(default_factory=list)


@dataclass(slots=True)
class StagedIssue:
    source_key: str
    issue_number: str | None
    title: str | None
    complete: bool | None
    groups: list[StagedGroup] = field(default_factory=list)


@dataclass(slots=True)
class StagedImport:
    schema_version: int
    staging_id: str
    created_at: str
    source_path: str
    content_root: str
    author: str
    series: str
    import_kind: str
    series_complete: bool | None = None
    issues: list[StagedIssue] = field(default_factory=list)
    series_extras: list[StagedGroup] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: str | Path) -> Path:
        destination = Path(path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return destination


def _staged_media(items: list[ScannedMedia]) -> list[StagedMedia]:
    return [
        StagedMedia(
            source_path=str(item.path),
            relative_path=str(item.relative_path),
            mime_type=item.mime_type,
            media_kind=item.media_kind.value,
            size_bytes=item.size_bytes,
            order=item.order,
        )
        for item in items
    ]


def _group(
    group: ScannedGroup,
    role: ReviewRole,
    owner_type: str,
    owner_key: str | None,
    *,
    name: str | None = None,
) -> StagedGroup:
    return StagedGroup(
        name=name or group.name,
        relative_path=str(group.relative_path),
        role=role.value,
        owner_type=owner_type,
        owner_key=owner_key,
        media=_staged_media(group.media),
    )


def _find_issue(scan_issues: list[ScannedIssue], key: Path) -> ScannedIssue:
    for issue in scan_issues:
        if issue.relative_path == key:
            return issue
    raise StagingError(f"Could not find scanned issue for {key}")


def _find_group(plan: ReviewPlan, key: Path) -> ScannedGroup:
    if key == Path(".") and plan.scan.primary is not None:
        return plan.scan.primary
    for group in plan.scan.extras:
        if group.relative_path == key:
            return group
    for issue in plan.scan.issues:
        if issue.primary and issue.primary.relative_path == key:
            return issue.primary
        for group in issue.extras:
            if group.relative_path == key:
                return group
    raise StagingError(f"Could not find scanned group for {key}")


def build_staged_import(
    plan: ReviewPlan,
    *,
    author: str,
    series: str,
    issue_metadata: dict[str, dict[str, object]] | None = None,
    series_complete: bool | None = None,
) -> StagedImport:
    errors = plan.validation_errors()
    if errors:
        raise StagingError("Review plan is invalid: " + "; ".join(errors))
    if not author.strip():
        raise StagingError("Author is required")
    if not series.strip():
        raise StagingError("Series is required")

    issue_metadata = issue_metadata or {}
    staged_issues: dict[str, StagedIssue] = {}
    series_extras: list[StagedGroup] = []

    if plan.scan.is_series_candidate:
        for item in plan.items:
            key = str(item.relative_path)
            if item.source_kind == "issue":
                issue = _find_issue(plan.scan.issues, item.relative_path)
                if item.role is ReviewRole.ISSUE:
                    meta = issue_metadata.get(key, {})
                    staged = StagedIssue(
                        source_key=key,
                        issue_number=_optional_text(meta.get("issue_number", issue.name)),
                        title=_optional_text(meta.get("title")),
                        complete=_optional_bool(meta.get("complete")),
                    )
                    if issue.primary:
                        staged.groups.append(_group(issue.primary, ReviewRole.PRIMARY, "issue", key))
                    for extra in issue.extras:
                        extra_item = plan.find(extra.relative_path)
                        if extra_item.role is ReviewRole.ISSUE_EXTRA:
                            staged.groups.append(_group(extra, extra_item.role, "issue", key, name=extra_item.name))
                        elif extra_item.role is ReviewRole.SERIES_EXTRA:
                            series_extras.append(_group(extra, extra_item.role, "series", None, name=extra_item.name))
                    staged_issues[key] = staged
                elif item.role is ReviewRole.SERIES_EXTRA:
                    # Preserve the issue container as one named extra group rather than flattening files.
                    media: list[ScannedMedia] = []
                    if issue.primary:
                        media.extend(issue.primary.media)
                    for extra in issue.extras:
                        media.extend(extra.media)
                    synthetic = ScannedGroup(item.name, issue.relative_path, issue.primary.suggested_role if issue.primary else SuggestedRole.EXTRA, media)
                    series_extras.append(_group(synthetic, ReviewRole.SERIES_EXTRA, "series", None))

        # Root-level groups, including groups manually reclassified as issues.
        for item in plan.items:
            if item.source_kind != "group" or item.issue_path is not None:
                continue
            group = _find_group(plan, item.relative_path)
            key = str(item.relative_path)
            if item.role is ReviewRole.SERIES_EXTRA:
                series_extras.append(_group(group, item.role, "series", None, name=item.name))
            elif item.role is ReviewRole.ISSUE:
                meta = issue_metadata.get(key, {})
                staged_issues[key] = StagedIssue(
                    source_key=key,
                    issue_number=_optional_text(meta.get("issue_number", item.name)),
                    title=_optional_text(meta.get("title")),
                    complete=_optional_bool(meta.get("complete")),
                    groups=[_group(group, ReviewRole.PRIMARY, "issue", key, name=item.name)],
                )

        # Issue-owned groups changed during review.
        for item in plan.items:
            if item.source_kind != "group" or item.issue_path is None:
                continue
            if item.role is ReviewRole.SERIES_EXTRA:
                group = _find_group(plan, item.relative_path)
                if not any(extra.relative_path == str(group.relative_path) for extra in series_extras):
                    series_extras.append(_group(group, item.role, "series", None, name=item.name))
            elif item.role is ReviewRole.PRIMARY:
                owner = str(item.issue_path)
                if owner in staged_issues:
                    group = _find_group(plan, item.relative_path)
                    staged_issues[owner].groups.append(_group(group, item.role, "issue", owner, name=item.name))
    else:
        key = "."
        meta = issue_metadata.get(key, {})
        staged = StagedIssue(
            source_key=key,
            issue_number=_optional_text(meta.get("issue_number")),
            title=_optional_text(meta.get("title")),
            complete=_optional_bool(meta.get("complete")),
        )
        for item in plan.items:
            group = _find_group(plan, item.relative_path)
            if item.role in {ReviewRole.PRIMARY, ReviewRole.ISSUE_EXTRA}:
                staged.groups.append(_group(group, item.role, "issue", key, name=None if item.role is ReviewRole.PRIMARY else item.name))
            elif item.role is ReviewRole.SERIES_EXTRA:
                series_extras.append(_group(group, item.role, "series", None, name=item.name))
        staged_issues[key] = staged

    return StagedImport(
        schema_version=SCHEMA_VERSION,
        staging_id=str(uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(),
        source_path=str(plan.scan.source),
        content_root=str(plan.scan.content_root),
        author=author.strip(),
        series=series.strip(),
        series_complete=series_complete,
        import_kind="series" if plan.scan.is_series_candidate else "issue",
        issues=list(staged_issues.values()),
        series_extras=series_extras,
    )



def create_staged_issue_extra(staged: StagedImport, issue_key: str, name: str) -> str:
    name = name.strip()
    if not name:
        raise StagingError("Extra group name cannot be empty")
    issue = next((item for item in staged.issues if item.source_key == issue_key), None)
    if issue is None:
        raise StagingError(f"Staged issue not found: {issue_key}")
    if any(group.name.casefold() == name.casefold() for group in issue.groups):
        raise StagingError(f"This issue already has a group named {name!r}")
    group_key = f"virtual/{uuid4()}"
    issue.groups.append(
        StagedGroup(
            name=name,
            relative_path=group_key,
            role=ReviewRole.ISSUE_EXTRA.value,
            owner_type="issue",
            owner_key=issue_key,
            media=[],
        )
    )
    return group_key


def remove_empty_staged_group(staged: StagedImport, issue_key: str, group_path: str) -> None:
    issue = next((item for item in staged.issues if item.source_key == issue_key), None)
    if issue is None:
        raise StagingError(f"Staged issue not found: {issue_key}")
    group = next((item for item in issue.groups if item.relative_path == group_path), None)
    if group is None:
        raise StagingError(f"Staged group not found: {group_path}")
    if group.role == ReviewRole.PRIMARY.value:
        raise StagingError("The primary comic group cannot be removed")
    if group.media:
        raise StagingError("Only empty extra groups can be removed")
    issue.groups.remove(group)


def move_staged_media(
    staged: StagedImport,
    issue_key: str,
    source_path: str,
    target_group_path: str,
) -> None:
    issue = next((item for item in staged.issues if item.source_key == issue_key), None)
    if issue is None:
        raise StagingError(f"Staged issue not found: {issue_key}")
    target = next((group for group in issue.groups if group.relative_path == target_group_path), None)
    if target is None:
        raise StagingError(f"Target staged group not found: {target_group_path}")

    source_group = None
    media = None
    for group in issue.groups:
        for item in group.media:
            if item.source_path == source_path:
                source_group = group
                media = item
                break
        if media is not None:
            break
    if media is None or source_group is None:
        raise StagingError(f"Staged media not found: {source_path}")
    if source_group is target:
        return

    source_group.media.remove(media)
    target.media.append(media)
    for group in {id(source_group): source_group, id(target): target}.values():
        group.media.sort(key=lambda item: item.order)
        for index, item in enumerate(group.media, start=1):
            item.order = index

def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: object) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"y", "yes", "true", "1", "complete"}:
        return True
    if text in {"n", "no", "false", "0", "incomplete"}:
        return False
    raise StagingError(f"Invalid completeness value: {value!r}")
