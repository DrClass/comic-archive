from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..auth import list_users
from ..database import connect_database
from ..permissions import get_restriction, save_restriction
from ..web_forms import form_data


def register_permission_routes(app: FastAPI, templates: Jinja2Templates, database: Path) -> None:
    def target(request: Request, kind: str, target_id: str):
        if not request.state.user.is_admin:
            raise HTTPException(status_code=403, detail="Administrator access required")
        if kind not in {"series", "issue"}:
            raise HTTPException(status_code=404, detail="Comic not found")
        table = "series" if kind == "series" else "issues"
        with connect_database(database) as db:
            row = db.execute(f"SELECT title FROM {table} WHERE id = ?", (target_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Comic not found")
            restriction = get_restriction(db, kind, target_id)
        return {
            "kind": kind, "target_id": target_id, "comic_title": row[0] or "Untitled issue",
            "back_url": f"/{table}/{target_id}", "restriction": restriction,
            "users": list_users(database), "error": None,
        }

    @app.get("/admin/permissions/{kind}/{target_id}", response_class=HTMLResponse)
    def permission_page(request: Request, kind: str, target_id: str):
        return templates.TemplateResponse(request=request, name="permissions.html", context=target(request, kind, target_id))

    @app.post("/admin/permissions/{kind}/{target_id}", response_class=HTMLResponse)
    async def permission_save(request: Request, kind: str, target_id: str):
        context = target(request, kind, target_id)
        form = await form_data(request)
        mode = form.get("access", "")
        selected = {key.removeprefix("user_") for key, value in form.items() if key.startswith("user_") and value == "yes"}
        try:
            if mode not in {"all", "selected"}:
                raise ValueError("Choose who can access this comic")
            save_restriction(database, request.state.user, kind, target_id, mode == "selected", selected)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            context.update(error=str(exc), restriction={"restricted": mode == "selected", "users": selected})
            return templates.TemplateResponse(request=request, name="permissions.html", context=context, status_code=400)
        return RedirectResponse(f"/admin/permissions/{kind}/{target_id}?saved=1", status_code=303)
