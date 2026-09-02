from __future__ import annotations

import re
from pathlib import Path

_SPLIT_NUMBERS = re.compile(r"(\d+)")

# When two files have the same base name, static pages should be presented
# before animation/video variants. This intentionally differs from Windows'
# extension-based ordering (e.g. 2.png comes before 2.gif here).
_EXTENSION_PRIORITY = {
    ".jpg": 10,
    ".jpeg": 10,
    ".png": 10,
    ".gif": 20,
    ".mp4": 30,
}


def natural_text_key(value: str) -> tuple[object, ...]:
    """Return a case-insensitive key where digit runs sort numerically."""
    parts = _SPLIT_NUMBERS.split(value.casefold())
    return tuple(int(part) if part.isdigit() else part for part in parts)


def natural_path_key(path: Path) -> tuple[object, ...]:
    """Natural-sort a path, with media-type priority for identical stems."""
    parent_parts = tuple(natural_text_key(part) for part in path.parts[:-1])
    name = path.name
    suffix = path.suffix.casefold()
    stem = path.stem if suffix else name
    return (
        *parent_parts,
        natural_text_key(stem),
        _EXTENSION_PRIORITY.get(suffix, 15),
        natural_text_key(suffix),
        natural_text_key(name),
    )
