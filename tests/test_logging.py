import logging
import sys

import pytest

from comic_archive.logging_config import configure_logging, ResilientRotatingFileHandler
from comic_archive.importer.scanner import scan_folder


@pytest.fixture(autouse=True)
def isolate_logging():
    targets = [logging.getLogger(name) for name in ("comic_archive", "uvicorn")]
    previous = [(target.level, list(target.handlers)) for target in targets]
    for target in targets:
        target.handlers = []
    yield
    installed = {handler for target in targets for handler in target.handlers}
    for target, (level, handlers) in zip(targets, previous):
        target.handlers = handlers
        target.setLevel(level)
    for handler in installed:
        handler.close()


def test_application_scanner_pdf_and_server_logs_share_file(tmp_path, capsys):
    import fitz

    path = tmp_path / "logs/app.log"
    configure_logging(path, level="DEBUG")
    source = tmp_path / "source"
    source.mkdir()
    with fitz.open() as document:
        document.new_page()
        document.save(source / "comic.pdf")
    scan_folder(source, pdf_cache_root=tmp_path / "pdf-cache")
    logging.getLogger("comic_archive.web").info("session_id=example workspace event")
    logging.getLogger("comic_archive.thumbnails").debug("thumbnail debug event")
    logging.getLogger("uvicorn.access").info("access event")
    logging.getLogger("comic_archive.web").error("error event")
    content = path.read_text(encoding="utf-8")
    for marker in ("pdf render started", "scan started", "session_id=example", "thumbnail debug event", "access event", "error event"):
        assert content.count(marker) == 1
    assert "Z INFO comic_archive.scanner CA_DIAG" in content
    stderr = capsys.readouterr().err
    assert "error event" in stderr
    assert "workspace event" not in stderr


def test_reconfiguration_closes_old_file_and_info_filters_debug(tmp_path):
    first, second = tmp_path / "first.log", tmp_path / "second.log"
    configure_logging(first)
    old = next(h for h in logging.getLogger("comic_archive").handlers if isinstance(h, ResilientRotatingFileHandler))
    configure_logging(second)
    configure_logging(second)
    assert old.stream is None
    logging.getLogger("comic_archive.scanner").debug("hidden debug")
    logging.getLogger("comic_archive.scanner").info("single event")
    assert second.read_text().count("single event") == 1
    assert "hidden debug" not in second.read_text()
    assert "single event" not in first.read_text()


def test_rotation_is_bounded(tmp_path, monkeypatch):
    import comic_archive.logging_config as config

    monkeypatch.setattr(config, "LOG_MAX_BYTES", 150)
    monkeypatch.setattr(config, "LOG_BACKUP_COUNT", 2)
    configure_logging(tmp_path / "app.log")
    for index in range(10):
        logging.getLogger("comic_archive.scanner").info("rotation record %d %s", index, "x" * 80)
    assert {p.name for p in tmp_path.iterdir()} == {"app.log", "app.log.1", "app.log.2"}
    assert "rotation record 9" in (tmp_path / "app.log").read_text()


def test_file_setup_and_rotation_errors_fall_back_to_stderr(tmp_path, monkeypatch, capsys):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file")
    configure_logging(blocked / "app.log")
    logging.getLogger("comic_archive.scanner").info("fallback info")
    assert "fallback info" in capsys.readouterr().err
    configure_logging(tmp_path / "app.log")
    handler = next(h for h in logging.getLogger("comic_archive").handlers if isinstance(h, ResilientRotatingFileHandler))
    def fail_rotation(record):
        raise OSError("disk failure")
    monkeypatch.setattr(handler, "shouldRollover", fail_rotation)
    logging.getLogger("comic_archive.scanner").info("record survives disk failure")
    assert "record survives disk failure" in capsys.readouterr().err


def test_cli_scan_accepts_log_options(tmp_path, monkeypatch):
    from comic_archive.cli import main

    source = tmp_path / "source"
    source.mkdir()
    (source / "001.jpg").write_bytes(b"page")
    path = tmp_path / "custom.log"
    monkeypatch.setattr(sys, "argv", ["comic-import", "scan", str(source), "--log-file", str(path), "--log-level", "DEBUG"])
    assert main() == 0
    assert "scan started" in path.read_text()


def test_cli_serve_wires_file_logging_without_uvicorn_reconfiguration(tmp_path, monkeypatch):
    import uvicorn
    from comic_archive.cli import main

    path = tmp_path / "custom/server.log"
    calls = []
    def run(app, **kwargs):
        calls.append(kwargs)
        assert app.state.log_file == path
        logging.getLogger("uvicorn.error").info("server startup event")
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(sys, "argv", [
        "comic-import", "serve", "--database", str(tmp_path / "db.sqlite3"),
        "--library", str(tmp_path / "library"), "--staging", str(tmp_path / "staging"),
        "--log-file", str(path), "--log-level", "DEBUG",
    ])
    assert main() == 0
    assert calls[0]["log_config"] is None
    content = path.read_text()
    assert "diagnostics enabled" in content
    assert "server startup event" in content
