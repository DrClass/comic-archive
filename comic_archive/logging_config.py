"""Process-wide logging for the single-process application and CLI."""
from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5


class ResilientRotatingFileHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # Disk/rotation errors must not interrupt imports. Keep the record
        # visible through stderr even when the primary destination is unusable.
        try:
            sys.stderr.write("Comic Archive log file unavailable; stderr fallback\n")
            sys.stderr.write(self.format(record) + "\n")
        except Exception:
            pass


class SafeStreamHandler(logging.StreamHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        pass


def configure_logging(log_file: str | Path, *, level: str = "INFO") -> Path:
    level = level.upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError(f"Invalid log level: {level}")
    path = Path(log_file).expanduser().absolute()
    formatter = logging.Formatter(
        "%(asctime)sZ %(levelname)s %(name)s CA_DIAG %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime
    handlers: list[logging.Handler] = []
    error: Exception | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(ResilientRotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        ))
    except OSError as exc:
        error = exc
    console = SafeStreamHandler()
    console.setLevel(logging.ERROR if handlers else getattr(logging, level))
    handlers.append(console)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler._comic_archive_owned = True

    # Replace only handlers installed here. Repeated app construction must
    # neither duplicate records nor keep previous file descriptors open.
    old_handlers: set[logging.Handler] = set()
    for name in ("comic_archive", "uvicorn"):
        target = logging.getLogger(name)
        for handler in list(target.handlers):
            if getattr(handler, "_comic_archive_owned", False):
                target.removeHandler(handler)
                old_handlers.add(handler)
        target.setLevel(getattr(logging, level))
        for handler in handlers:
            target.addHandler(handler)
    for handler in old_handlers:
        handler.close()
    if error is not None:
        logging.getLogger("comic_archive").error(
            "Cannot open log file %s: %s; using stderr", path, error,
        )
    return path
