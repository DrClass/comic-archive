from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

logger = logging.getLogger("comic_archive.web")


def configure_diagnostic_logging() -> None:
    """Ensure importer diagnostics always reach stderr/journald at INFO."""
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(getattr(handler, "_comic_archive_diag", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler._comic_archive_diag = True  # type: ignore[attr-defined]
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("CA_DIAG %(message)s"))
        logger.addHandler(handler)


def process_memory_snapshot() -> dict[str, float | int | None]:
    values: dict[str, float | int | None] = {
        "rss_mib": None,
        "anon_mib": None,
        "file_mib": None,
        "vmsize_mib": None,
        "threads": None,
    }
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if key == "Threads" and parts:
                values["threads"] = int(parts[0])
            elif key in {"VmRSS", "RssAnon", "RssFile", "VmSize"} and parts:
                mapped = {
                    "VmRSS": "rss_mib",
                    "RssAnon": "anon_mib",
                    "RssFile": "file_mib",
                    "VmSize": "vmsize_mib",
                }[key]
                values[mapped] = int(parts[0]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return values


def process_rss_mib() -> float | None:
    value = process_memory_snapshot().get("rss_mib")
    return float(value) if value is not None else None


def log_memory_checkpoint(label: str, **context: object) -> None:
    memory = process_memory_snapshot()
    suffix = " ".join(f"{key}={value}" for key, value in context.items())
    logger.info(
        "memory checkpoint label=%s rss_mib=%s anon_mib=%s file_mib=%s vmsize_mib=%s threads=%s%s%s",
        label,
        f"{memory['rss_mib']:.1f}" if isinstance(memory["rss_mib"], float) else "unknown",
        f"{memory['anon_mib']:.1f}" if isinstance(memory["anon_mib"], float) else "unknown",
        f"{memory['file_mib']:.1f}" if isinstance(memory["file_mib"], float) else "unknown",
        f"{memory['vmsize_mib']:.1f}" if isinstance(memory["vmsize_mib"], float) else "unknown",
        memory["threads"] if memory["threads"] is not None else "unknown",
        " " if suffix else "",
        suffix,
    )


async def run_background_io(func, /, *args, **kwargs):
    """Run one long import operation without blocking ASGI or holding process shutdown open."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def runner() -> None:
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            loop.call_soon_threadsafe(future.set_exception, exc)
        else:
            loop.call_soon_threadsafe(future.set_result, result)

    thread = threading.Thread(
        target=runner,
        name=f"comic-archive-{getattr(func, '__name__', 'worker')}",
        daemon=True,
    )
    thread.start()
    return await future
