from __future__ import annotations

from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

from ..database import connect_database
from ..thumbnails import THUMBNAIL_MIME, ensure_thumbnail
from ..auth import User
from ..permissions import can_view_media


_NO_PERMISSION_IMAGE = Path(__file__).resolve().parent.parent / "no-permission.jpg"


def _media_record(database: Path, media_id: str, user: User) -> tuple[str, str] | None:
    with connect_database(database) as db:
        if not can_view_media(db, user, media_id):
            return None
        row = db.execute("SELECT stored_path, mime_type FROM media WHERE id = ? AND active = 1", (media_id,)).fetchone()
    return None if row is None else (row[0], row[1])


def _safe_library_file(library_root: Path, stored_path: str) -> Path:
    candidate = (library_root / stored_path).resolve()
    try:
        candidate.relative_to(library_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Media not found") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    return candidate


def register_media_routes(app: FastAPI, database: Path, library: Path) -> None:
    @app.get("/assets/no-permission")
    def no_permission_image():
        try:
            with _NO_PERMISSION_IMAGE.open("rb") as source:
                signature = source.read(8)
        except FileNotFoundError:
            return Response(
                '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" viewBox="0 0 300 300">'
                '<rect width="300" height="300" fill="#30343b"/>'
                '<text x="150" y="150" text-anchor="middle" fill="white" font-family="sans-serif" font-size="22">Restricted</text></svg>',
                media_type="image/svg+xml",
            )
        # The configured filename ends in .jpg even when the supplied bytes are PNG.
        mime_type = "image/png" if signature == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        return FileResponse(_NO_PERMISSION_IMAGE, media_type=mime_type)

    @app.get("/thumbnail/{media_id}")
    def thumbnail(request: Request, media_id: str):
        record = _media_record(database, media_id, request.state.user)
        if record is None:
            raise HTTPException(status_code=404, detail="Media not found")
        stored_path, mime_type = record
        path = ensure_thumbnail(library, media_id=media_id, stored_path=stored_path, mime_type=mime_type)
        if path is None:
            raise HTTPException(status_code=404, detail="Thumbnail unavailable")
        return FileResponse(path, media_type=THUMBNAIL_MIME, headers={"Cache-Control": "private, no-store"})

    @app.get("/media/{media_id}")
    def media(request: Request, media_id: str):
        record = _media_record(database, media_id, request.state.user)
        if record is None:
            raise HTTPException(status_code=404, detail="Media not found")
        stored_path, mime_type = record
        return FileResponse(_safe_library_file(library, stored_path), media_type=mime_type, headers={"Cache-Control": "private, no-store"})
