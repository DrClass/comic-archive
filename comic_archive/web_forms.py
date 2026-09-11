from __future__ import annotations

import secrets
from urllib.parse import parse_qs

from fastapi import HTTPException, Request


async def form_data(request: Request, *, check_csrf: bool = True) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    form = {key: values[-1] if values else "" for key, values in parsed.items()}
    if check_csrf:
        expected = request.session.get("csrf_token", "")
        supplied = form.get("csrf_token", "")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")
    return form


def check_csrf_value(request: Request, supplied: str) -> None:
    expected = request.session.get("csrf_token", "")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")


def bool_form(value: str) -> bool | None:
    if value == "yes":
        return True
    if value == "no":
        return False
    return None
