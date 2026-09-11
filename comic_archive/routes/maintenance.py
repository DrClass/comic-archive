from __future__ import annotations

from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..maintenance import build_maintenance_report, set_intentional_gap
from ..web_forms import form_data


def register_maintenance_routes(app: FastAPI, templates: Jinja2Templates, database: Path, library: Path) -> None:
    @app.get("/maintenance", response_class=HTMLResponse)
    def maintenance_page(request: Request):
        return templates.TemplateResponse(request=request, name="maintenance.html", context={"report": build_maintenance_report(database, library)})

    @app.post("/series/{series_id}/missing/{issue_number}")
    async def set_missing_issue_state(request: Request, series_id: str, issue_number: int):
        form = await form_data(request)
        try:
            set_intentional_gap(database, series_id, issue_number, form.get("intentional") == "yes", form.get("note", ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        target = form.get("return_to", f"/series/{series_id}")
        if not target.startswith("/"):
            target = f"/series/{series_id}"
        return RedirectResponse(target, status_code=303)
