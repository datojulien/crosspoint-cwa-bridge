"""Public landing page and isolated HTTPS administration console."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from html import escape
from importlib.resources import files
import logging
import time
from typing import Any

from aiohttp import web

from . import __version__
from .admin_state import ADMIN_USERNAME, SESSION_COOKIE, safe_settings
from .cache import CACHE_SCHEMA_VERSION, CacheProfileStats
from .optimizer import OPTIMIZER_VERSION
from .runtime import BridgeRuntime


LOGGER = logging.getLogger("crosspoint_cwa_bridge")
RUNTIME_KEY = web.AppKey("bridge_runtime", BridgeRuntime)
SESSION_KEY = web.RequestKey("admin_session", object)
ASSET_TYPES = {
    "portal.css": "text/css",
    "portal.js": "application/javascript",
    "admin.js": "application/javascript",
}
PUBLIC_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
ADMIN_SECURITY_HEADERS = PUBLIC_SECURITY_HEADERS | {
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000",
}


def _asset(name: str) -> bytes:
    if name not in ASSET_TYPES and name not in {
        "landing.html",
        "login.html",
        "admin.html",
    }:
        raise web.HTTPNotFound()
    return files("crosspoint_cwa_bridge.web").joinpath(name).read_bytes()


def _html(name: str, runtime: BridgeRuntime) -> web.Response:
    body = _asset(name).decode("utf-8")
    body = body.replace("{{PUBLIC_BASE_URL}}", escape(runtime.settings.public_base_url))
    body = body.replace(
        "{{ADMIN_PUBLIC_URL}}", escape(runtime.settings.admin_public_url)
    )
    return web.Response(
        text=body,
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


def _stats_payload(stats: dict[str, CacheProfileStats]) -> dict[str, dict[str, int]]:
    return {profile: asdict(values) for profile, values in stats.items()}


@web.middleware
async def public_security_headers(request: web.Request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as exception:
        exception.headers.update(PUBLIC_SECURITY_HEADERS)
        raise
    response.headers.update(PUBLIC_SECURITY_HEADERS)
    return response


@web.middleware
async def admin_security_headers(request: web.Request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as exception:
        exception.headers.update(ADMIN_SECURITY_HEADERS)
        raise
    response.headers.update(ADMIN_SECURITY_HEADERS)
    return response


@web.middleware
async def admin_authentication(request: web.Request, handler):
    public_paths = {
        "/admin/login",
        "/admin/api/login",
        "/admin/api/health",
        "/admin/assets/portal.css",
        "/admin/assets/admin.js",
    }
    if request.path in public_paths:
        return await handler(request)
    runtime = request.app[RUNTIME_KEY]
    session = runtime.sessions.get(request.cookies.get(SESSION_COOKIE))
    if session is None:
        if request.path.startswith("/admin/api/"):
            raise web.HTTPUnauthorized(
                text='{"error":"authentication_required"}',
                content_type="application/json",
            )
        raise web.HTTPFound("/admin/login")
    request[SESSION_KEY] = session
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        supplied = request.headers.get("X-CSRF-Token", "")
        if not secrets_compare(supplied, session.csrf_token):
            raise web.HTTPForbidden(
                text='{"error":"csrf_validation_failed"}',
                content_type="application/json",
            )
    return await handler(request)


def secrets_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


async def _json_body(request: web.Request) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(
            text='{"error":"application_json_required"}',
            content_type="application/json",
        )
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise web.HTTPBadRequest(
            text='{"error":"invalid_json"}', content_type="application/json"
        ) from exc
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(
            text='{"error":"json_object_required"}',
            content_type="application/json",
        )
    return payload


async def public_landing(request: web.Request) -> web.Response:
    return _html("landing.html", request.app[RUNTIME_KEY])


async def public_asset(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    media_type = ASSET_TYPES.get(name)
    if media_type is None or name == "admin.js":
        raise web.HTTPNotFound()
    return web.Response(
        body=_asset(name),
        content_type=media_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def public_status(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    upstream = await runtime.probe_upstream()
    stats = await runtime.cache_stats()
    return web.json_response(
        {
            "status": "ok",
            "version": __version__,
            "optimizer_version": OPTIMIZER_VERSION,
            "uptime_seconds": round(time.monotonic() - runtime.started_monotonic),
            "cwa": {
                "state": upstream.state,
                "checked_at": upstream.checked_at,
            },
            "opds_url": f"{runtime.settings.public_base_url}/opds",
            "admin_url": f"{runtime.settings.admin_public_url}/admin",
            "admin_available": runtime.password_store.configured()
            and runtime.admin_listening,
            "cache": _stats_payload(stats),
        },
        headers={"Cache-Control": "no-store"},
    )


async def admin_health(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    return web.json_response(
        {
            "status": "ok",
            "version": __version__,
            "configured": runtime.password_store.configured(),
        }
    )


async def admin_login_page(request: web.Request) -> web.Response:
    if request.app[RUNTIME_KEY].sessions.get(request.cookies.get(SESSION_COOKIE)):
        raise web.HTTPFound("/admin")
    return _html("login.html", request.app[RUNTIME_KEY])


async def admin_login(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    if not runtime.password_store.configured():
        raise web.HTTPServiceUnavailable(
            text='{"error":"admin_not_configured"}',
            content_type="application/json",
        )
    client = request.remote or "unknown"
    if not runtime.login_limiter.allowed(client):
        raise web.HTTPTooManyRequests(
            text='{"error":"try_again_later"}',
            content_type="application/json",
            headers={"Retry-After": "900"},
        )
    payload = await _json_body(request)
    username = payload.get("username")
    password = payload.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise web.HTTPBadRequest(
            text='{"error":"invalid_credentials"}',
            content_type="application/json",
        )
    if not runtime.password_store.verify(username, password):
        runtime.login_limiter.failed(client)
        runtime.record("admin_login_failed", status=401)
        raise web.HTTPUnauthorized(
            text='{"error":"invalid_credentials"}',
            content_type="application/json",
        )
    runtime.login_limiter.succeeded(client)
    token, _ = runtime.sessions.create()
    runtime.record("admin_login_succeeded", status=200)
    response = web.json_response({"status": "ok", "redirect": "/admin"})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        secure=True,
        httponly=True,
        samesite="Strict",
        path="/admin",
        max_age=8 * 60 * 60,
    )
    return response


async def admin_logout(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    runtime.sessions.remove(request.cookies.get(SESSION_COOKIE))
    response = web.json_response({"status": "ok"})
    response.del_cookie(SESSION_COOKIE, path="/admin")
    return response


async def admin_dashboard(request: web.Request) -> web.Response:
    return _html("admin.html", request.app[RUNTIME_KEY])


async def admin_asset(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    media_type = ASSET_TYPES.get(name)
    if media_type is None or name == "portal.js":
        raise web.HTTPNotFound()
    return web.Response(body=_asset(name), content_type=media_type)


async def admin_status(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    upstream = await runtime.probe_upstream()
    stats = await runtime.cache_stats()
    pending, pending_error = runtime.settings_store.load_pending()
    session = request[SESSION_KEY]
    upstream_payload = asdict(upstream)
    upstream_payload.pop("checked_monotonic", None)
    return web.json_response(
        {
            "status": "ok",
            "version": __version__,
            "optimizer_version": OPTIMIZER_VERSION,
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "started_at": runtime.started_at.isoformat(timespec="seconds"),
            "uptime_seconds": round(time.monotonic() - runtime.started_monotonic),
            "upstream": upstream_payload,
            "cache": _stats_payload(stats),
            "conversion": runtime.conversion_status(),
            "storage": runtime.storage_status(),
            "tls": runtime.tls_status(),
            "active_settings": safe_settings(runtime.settings),
            "pending_settings": pending,
            "settings_error": runtime.settings_error or pending_error,
            "activity_dropped": runtime.activity.dropped,
            "csrf_token": session.csrf_token,
        }
    )


async def admin_activity(request: web.Request) -> web.Response:
    raw_limit = request.query.get("limit", "100")
    try:
        limit = int(raw_limit)
    except ValueError as exc:
        raise web.HTTPBadRequest(
            text='{"error":"invalid_limit"}', content_type="application/json"
        ) from exc
    events = await request.app[RUNTIME_KEY].activity.recent(limit)
    return web.json_response({"events": events})


async def admin_save_settings(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    payload = await _json_body(request)
    try:
        pending = runtime.settings_store.save_pending(payload)
    except (OSError, RuntimeError, ValueError) as exc:
        raise web.HTTPBadRequest(
            text='{"error":"invalid_settings"}', content_type="application/json"
        ) from exc
    active = safe_settings(runtime.settings)
    changes = {
        name: {"active": active[name], "pending": value}
        for name, value in pending.items()
        if active[name] != value
    }
    runtime.record("admin_settings_saved", status=200)
    return web.json_response({"status": "pending", "changes": changes})


async def admin_diagnostics(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    upstream = await runtime.probe_upstream(force=True)
    stats = await runtime.cache_stats(verify_checksums=True)
    pending, pending_error = runtime.settings_store.load_pending()
    runtime.record("admin_diagnostics", status=200)
    upstream_payload = asdict(upstream)
    upstream_payload.pop("checked_monotonic", None)
    return web.json_response(
        {
            "upstream": upstream_payload,
            "cache": _stats_payload(stats),
            "storage": runtime.storage_status(),
            "tls": runtime.tls_status(),
            "credentials_configured": runtime.password_store.configured(),
            "pending_changes": pending is not None,
            "settings_error": runtime.settings_error or pending_error,
        }
    )


async def admin_purge_cache(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    payload = await _json_body(request)
    scope = payload.get("scope")
    expected = {"x3": "clear-x3", "x4": "clear-x4", "all": "clear-all"}
    if scope not in expected or payload.get("confirmation") != expected[scope]:
        raise web.HTTPBadRequest(
            text='{"error":"purge_confirmation_required"}',
            content_type="application/json",
        )
    removed = await runtime.purge_cache(scope)
    entries = sum(values.entries for values in removed.values())
    output_bytes = sum(values.output_bytes for values in removed.values())
    runtime.record(
        "admin_cache_purged",
        profile=scope,
        status=200,
        output_bytes=output_bytes,
    )
    return web.json_response(
        {"status": "ok", "scope": scope, "entries": entries, "bytes": output_bytes}
    )


async def admin_change_password(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    payload = await _json_body(request)
    current = payload.get("current_password")
    new = payload.get("new_password")
    if not isinstance(current, str) or not isinstance(new, str):
        raise web.HTTPBadRequest(
            text='{"error":"invalid_password_request"}',
            content_type="application/json",
        )
    if not runtime.password_store.verify(ADMIN_USERNAME, current):
        raise web.HTTPForbidden(
            text='{"error":"current_password_incorrect"}',
            content_type="application/json",
        )
    try:
        runtime.password_store.set_password(new)
    except ValueError as exc:
        raise web.HTTPBadRequest(
            text='{"error":"new_password_invalid"}',
            content_type="application/json",
        ) from exc
    runtime.record("admin_password_changed", status=200)
    runtime.sessions.invalidate_all()
    response = web.json_response({"status": "ok", "reauthenticate": True})
    response.del_cookie(SESSION_COOKIE, path="/admin")
    return response


async def admin_restart(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    try:
        promoted = runtime.settings_store.promote_pending()
    except (OSError, RuntimeError) as exc:
        raise web.HTTPInternalServerError(
            text='{"error":"settings_promotion_failed"}',
            content_type="application/json",
        ) from exc
    runtime.record("admin_restart_requested", status=202)

    async def restart_after_response() -> None:
        await runtime.activity.flush()
        await runtime.restart_callback()

    asyncio.create_task(restart_after_response())
    return web.json_response(
        {"status": "restarting", "settings_promoted": promoted}, status=202
    )


def add_public_routes(app: web.Application) -> None:
    app.router.add_get("/", public_landing)
    app.router.add_get("/api/status", public_status)
    app.router.add_get("/assets/{name}", public_asset)


def create_admin_app(runtime: BridgeRuntime) -> web.Application:
    app = web.Application(
        client_max_size=16 * 1024,
        middlewares=[admin_security_headers, admin_authentication],
    )
    app[RUNTIME_KEY] = runtime
    app.router.add_get("/admin", admin_dashboard)
    app.router.add_get("/admin/login", admin_login_page)
    app.router.add_get("/admin/assets/{name}", admin_asset)
    app.router.add_get("/admin/api/health", admin_health)
    app.router.add_post("/admin/api/login", admin_login)
    app.router.add_post("/admin/api/logout", admin_logout)
    app.router.add_get("/admin/api/status", admin_status)
    app.router.add_get("/admin/api/activity", admin_activity)
    app.router.add_put("/admin/api/settings", admin_save_settings)
    app.router.add_post("/admin/api/diagnostics", admin_diagnostics)
    app.router.add_post("/admin/api/cache/purge", admin_purge_cache)
    app.router.add_post("/admin/api/password", admin_change_password)
    app.router.add_post("/admin/api/restart", admin_restart)
    return app


__all__ = [
    "RUNTIME_KEY",
    "add_public_routes",
    "create_admin_app",
    "public_security_headers",
]
