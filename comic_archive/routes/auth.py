from __future__ import annotations

import secrets
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..auth import (
    AuthError,
    authenticate,
    change_password,
    create_user,
    list_users,
    reset_password,
    set_user_active,
    set_user_admin,
)
from ..web_forms import form_data


def register_auth_routes(app: FastAPI, templates: Jinja2Templates, database: Path) -> None:
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        # Authentication middleware redirects already-authenticated users away
        # from /login; an explicit check remains unnecessary here.
        if request.session.get("user_id"):
            from ..auth import get_user
            if get_user(database, request.session["user_id"]):
                return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request=request, name="login.html", context={"error": None})

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request):
        form = await form_data(request)
        username = form.get("username", "").strip()
        client_host = request.client.host if request.client else "unknown"
        key = f"{client_host}|{username.casefold()}"
        now = time.monotonic()
        recent = [stamp for stamp in app.state.login_attempts.get(key, []) if now - stamp < 300]
        app.state.login_attempts[key] = recent
        if len(recent) >= 5:
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"error": "Too many failed login attempts. Try again in a few minutes."},
                status_code=429,
            )

        user = authenticate(database, username, form.get("password", ""))
        if user is None:
            recent.append(now)
            app.state.login_attempts[key] = recent
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"error": "Invalid username or password."},
                status_code=401,
            )

        app.state.login_attempts.pop(key, None)
        csrf_token = request.session.get("csrf_token") or secrets.token_urlsafe(32)
        request.session.clear()
        request.session["csrf_token"] = csrf_token
        request.session["user_id"] = user.id
        request.session["session_version"] = user.session_version
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        await form_data(request)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/admin/users", response_class=HTMLResponse)
    def users_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="users.html",
            context={"users": list_users(database), "error": None},
        )

    @app.post("/admin/users", response_class=HTMLResponse)
    async def users_create(request: Request):
        form = await form_data(request)
        try:
            create_user(
                database,
                form.get("username", ""),
                form.get("password", ""),
                is_admin=form.get("is_admin") == "yes",
            )
        except AuthError as exc:
            return templates.TemplateResponse(
                request=request,
                name="users.html",
                context={"users": list_users(database), "error": str(exc)},
                status_code=400,
            )
        return RedirectResponse("/admin/users", status_code=303)

    @app.post("/admin/users/{user_id}/reset-password", response_class=HTMLResponse)
    async def user_reset_password(request: Request, user_id: str):
        form = await form_data(request)
        try:
            reset_password(database, user_id, form.get("password", ""))
        except AuthError as exc:
            return templates.TemplateResponse(
                request=request,
                name="users.html",
                context={"users": list_users(database), "error": str(exc)},
                status_code=400,
            )
        return RedirectResponse("/admin/users", status_code=303)

    @app.post("/admin/users/{user_id}/toggle-active")
    async def user_toggle_active(request: Request, user_id: str):
        await form_data(request)
        target = next((u for u in list_users(database) if u.id == user_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="Account not found")
        current = request.state.user
        if target.id == current.id and target.active:
            raise HTTPException(status_code=400, detail="You cannot disable your own account")
        set_user_active(database, user_id, not target.active)
        return RedirectResponse("/admin/users", status_code=303)

    @app.post("/admin/users/{user_id}/toggle-admin")
    async def user_toggle_admin(request: Request, user_id: str):
        await form_data(request)
        target = next((u for u in list_users(database) if u.id == user_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="Account not found")
        if target.id == request.state.user.id and target.is_admin:
            raise HTTPException(status_code=400, detail="You cannot remove your own administrator access")
        set_user_admin(database, user_id, not target.is_admin)
        return RedirectResponse("/admin/users", status_code=303)

    @app.get("/account/password", response_class=HTMLResponse)
    def password_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="change_password.html",
            context={"error": None, "success": None},
        )

    @app.post("/account/password", response_class=HTMLResponse)
    async def password_save(request: Request):
        form = await form_data(request)
        new_password = form.get("new_password", "")
        if new_password != form.get("confirm_password", ""):
            return templates.TemplateResponse(
                request=request,
                name="change_password.html",
                context={"error": "New passwords do not match.", "success": None},
                status_code=400,
            )
        try:
            user = change_password(
                database,
                request.state.user.id,
                form.get("current_password", ""),
                new_password,
            )
        except AuthError as exc:
            return templates.TemplateResponse(
                request=request,
                name="change_password.html",
                context={"error": str(exc), "success": None},
                status_code=400,
            )
        request.session["session_version"] = user.session_version
        return templates.TemplateResponse(
            request=request,
            name="change_password.html",
            context={
                "error": None,
                "success": "Password changed. Other existing sessions for this account are now invalid.",
            },
        )
