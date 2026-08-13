"""HTTP service for the CrossPoint–CWA bridge."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import logging
import os
from pathlib import Path
import re
import signal
import ssl
import stat
import tempfile
import time

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web
from multidict import CIMultiDict
from yarl import URL

from . import __version__
from .admin_state import SettingsStore
from .cache import (
    CACHE_SCHEMA_VERSION,
    CacheHit,
    DerivativeCache,
    build_cache_key,
    content_source_version,
    http_source_version,
)
from .config import Settings
from .feeds import (
    FeedError,
    PROFILES,
    build_profile_feed,
    rewrite_cwa_href,
    rewrite_feed,
)
from .logging_config import configure_logging
from .optimizer import OPTIMIZER_VERSION, OptimizationResult, optimize_epub
from .portal import (
    RUNTIME_KEY,
    add_public_routes,
    create_admin_app,
    public_security_headers,
)
from .runtime import BridgeRuntime


LOGGER = logging.getLogger("crosspoint_cwa_bridge")
CLIENT_SESSION_KEY = web.AppKey("client_session", ClientSession)
SETTINGS_KEY = web.AppKey("settings", Settings)
CONVERSION_SEMAPHORE_KEY = web.AppKey("conversion_semaphore", asyncio.Semaphore)
DERIVATIVE_CACHE_KEY = web.AppKey("derivative_cache", DerivativeCache)

FORWARDED_REQUEST_HEADERS = (
    "Authorization",
    "User-Agent",
    "Accept",
    "Accept-Language",
    "Range",
    "If-Match",
    "If-None-Match",
    "If-Modified-Since",
    "If-Unmodified-Since",
)
FORWARDED_RESPONSE_HEADERS = (
    "Accept-Ranges",
    "Cache-Control",
    "Content-Disposition",
    "Content-Length",
    "Content-Range",
    "Content-Type",
    "ETag",
    "Expires",
    "Last-Modified",
    "Retry-After",
    "WWW-Authenticate",
)


def _log_event(request: web.Request, level: int, event: str, **fields: object) -> None:
    LOGGER.log(level, event, extra=fields)
    request.app[RUNTIME_KEY].record(event, **fields)


def _safe_upstream_route(path: str) -> str:
    if path.startswith("/opds/search/"):
        return "/opds/search/<term>"
    return path


def _is_xml_content_type(value: str) -> bool:
    media_type = value.partition(";")[0].strip().lower()
    return media_type.endswith("+xml") or media_type in {
        "application/xml",
        "text/xml",
    }


def _validate_tail(tail: str) -> None:
    if "\\" in tail or "\x00" in tail:
        raise web.HTTPBadRequest(text="invalid upstream path\n")
    if any(segment in {".", ".."} for segment in tail.split("/")):
        raise web.HTTPBadRequest(text="invalid upstream path\n")


def _upstream_url(
    settings: Settings,
    *,
    profile: str,
    tail: str,
    raw_path: str,
    raw_query_string: str,
    bridge_prefix: str | None = None,
) -> URL:
    _validate_tail(tail)
    bridge_prefix = bridge_prefix or f"/opds/crosspoint/{profile}"
    if raw_path == bridge_prefix:
        raw_suffix = ""
    elif raw_path.startswith(f"{bridge_prefix}/"):
        raw_suffix = raw_path[len(bridge_prefix) :]
    else:
        raise web.HTTPBadRequest(text="invalid bridge path\n")

    # The origin is validated configuration and the path always starts with the
    # fixed /opds prefix. encoded=True prevents double-encoding valid incoming
    # percent escapes or changing plus signs in CWA's path-based search route.
    origin = settings.upstream_url.with_path("")
    target = f"{origin}/opds{raw_suffix}"
    if raw_query_string:
        target += f"?{raw_query_string}"
    return URL(target, encoded=True)


def _request_headers(
    request: web.Request, *, require_complete_body: bool = False
) -> CIMultiDict[str]:
    headers: CIMultiDict[str] = CIMultiDict()
    for name in FORWARDED_REQUEST_HEADERS:
        if require_complete_body and name in {
            "Range",
            "If-Match",
            "If-None-Match",
            "If-Modified-Since",
            "If-Unmodified-Since",
        }:
            continue
        if name in request.headers:
            headers[name] = request.headers[name]
    headers["Accept-Encoding"] = "identity"
    return headers


def _response_headers(upstream_headers) -> CIMultiDict[str]:
    headers: CIMultiDict[str] = CIMultiDict()
    for name in FORWARDED_RESPONSE_HEADERS:
        for value in upstream_headers.getall(name, []):
            headers.add(name, value)
    return headers


async def healthz(_: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "ok",
            "version": __version__,
            "optimizer_version": OPTIMIZER_VERSION,
            "cache_schema_version": CACHE_SCHEMA_VERSION,
        }
    )


async def profile_feed(_: web.Request) -> web.Response:
    return web.Response(
        body=build_profile_feed(),
        content_type="application/atom+xml",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


async def _read_bounded(response, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > maximum:
            raise FeedError("upstream XML exceeds configured size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _is_optimized_epub_request(profile: str, tail: str) -> bool:
    return profile in {"x3", "x4"} and bool(
        re.fullmatch(r"download/[^/]+/epub/?", tail, flags=re.IGNORECASE)
    )


async def _stream_file_response(
    request: web.Request,
    path: Path,
    *,
    allowed_root: Path,
    status: int,
    headers: CIMultiDict[str],
) -> web.StreamResponse:
    try:
        root = allowed_root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or path.is_symlink():
            raise ValueError("response file is outside its allowed root")
        # The resolved path is contained by a trusted bridge-owned root above.
        descriptor = os.open(  # lgtm[py/path-injection]
            resolved,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(descriptor)
            raise ValueError("response file is not a regular file")
    except (OSError, ValueError) as exc:
        raise web.HTTPInternalServerError(text="Bridge file unavailable\n") from exc

    headers["Content-Length"] = str(file_stat.st_size)
    downstream = web.StreamResponse(status=status, headers=headers)
    try:
        await downstream.prepare(request)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                await downstream.write(chunk)
    except ConnectionResetError:
        return downstream
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    with suppress(ConnectionResetError):
        await downstream.write_eof()
    return downstream


def _derivative_headers(headers: CIMultiDict[str]) -> CIMultiDict[str]:
    derivative_headers = headers.copy()
    for name in (
        "Accept-Ranges",
        "Content-Range",
        "ETag",
        "Last-Modified",
    ):
        derivative_headers.popall(name, None)
    derivative_headers["Cache-Control"] = "private, no-store"
    return derivative_headers


async def _cache_lookup(
    request: web.Request,
    *,
    cache_key: str,
    profile: str,
    status: int,
    started: float,
) -> CacheHit | None:
    try:
        lookup = await asyncio.to_thread(
            request.app[DERIVATIVE_CACHE_KEY].lookup,
            cache_key,
            profile=profile,
        )
    except Exception as exc:
        _log_event(
            request,
            logging.ERROR,
            "cache_lookup_failed",
            profile=profile,
            status=status,
            cache_reason=type(exc).__name__,
        )
        return None
    if lookup.invalid_reason:
        _log_event(
            request,
            logging.WARNING,
            "cache_invalid",
            profile=profile,
            status=status,
            cache_reason=lookup.invalid_reason,
        )
    if lookup.hit is None:
        return None

    _log_event(
        request,
        logging.INFO,
        "cache_hit",
        profile=profile,
        status=status,
        duration_ms=round((time.monotonic() - started) * 1000),
        original_bytes=lookup.hit.original_bytes,
        output_bytes=lookup.hit.output_bytes,
        savings_percent=round(lookup.hit.savings_percent, 1),
    )
    return lookup.hit


async def _optimized_epub_response(
    request: web.Request,
    upstream,
    *,
    profile: str,
    headers: CIMultiDict[str],
    settings: Settings,
    upstream_url: URL,
    started: float,
) -> web.StreamResponse:
    advertised_size = upstream.headers.get("Content-Length")
    if advertised_size and advertised_size.isdigit():
        if int(advertised_size) > settings.optimizer_max_epub_bytes:
            _log_event(
                request,
                logging.WARNING,
                "optimizer_fallback",
                profile=profile,
                status=upstream.status,
                fallback_reason="source_size_limit",
                original_bytes=int(advertised_size),
            )
            return await _stream_upstream_response(request, upstream, headers=headers)

    cache_key: str | None = None
    source_identity = str(upstream_url)
    reusable_version = http_source_version(upstream.headers)
    if reusable_version is not None:
        cache_key = build_cache_key(
            source_identity=source_identity,
            source_version=reusable_version,
            profile=profile,
            optimizer_version=OPTIMIZER_VERSION,
            jpeg_quality=settings.optimizer_jpeg_quality,
            max_image_pixels=settings.optimizer_max_image_pixels,
        )
        async with request.app[RUNTIME_KEY].cache_gate.reader():
            cache_hit = await _cache_lookup(
                request,
                cache_key=cache_key,
                profile=profile,
                status=upstream.status,
                started=started,
            )
            if cache_hit is not None:
                # Keep the reader gate through streaming so a purge cannot
                # remove this derivative between lookup and file open.
                upstream.close()
                return await _stream_file_response(
                    request,
                    cache_hit.path,
                    allowed_root=settings.cache_dir,
                    status=upstream.status,
                    headers=_derivative_headers(headers),
                )
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="crosspoint-download-", dir=settings.work_dir
    ) as temp_name:
        temp_dir = Path(temp_name)
        source_path = temp_dir / "source.epub"
        derivative_path = temp_dir / f"{profile}.epub"
        received = 0
        source_digest = hashlib.sha256()
        try:
            with source_path.open("wb") as stream:
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    received += len(chunk)
                    if received > settings.optimizer_max_epub_bytes:
                        raise web.HTTPRequestEntityTooLarge(
                            max_size=settings.optimizer_max_epub_bytes,
                            actual_size=received,
                        )
                    source_digest.update(chunk)
                    stream.write(chunk)
        except (ClientError, asyncio.TimeoutError, OSError) as exc:
            raise web.HTTPBadGateway(text="Incomplete CWA EPUB response\n") from exc

        if cache_key is None:
            cache_key = build_cache_key(
                source_identity=source_identity,
                source_version=content_source_version(
                    source_digest.hexdigest(), received
                ),
                profile=profile,
                optimizer_version=OPTIMIZER_VERSION,
                jpeg_quality=settings.optimizer_jpeg_quality,
                max_image_pixels=settings.optimizer_max_image_pixels,
            )
            async with request.app[RUNTIME_KEY].cache_gate.reader():
                cache_hit = await _cache_lookup(
                    request,
                    cache_key=cache_key,
                    profile=profile,
                    status=upstream.status,
                    started=started,
                )
                if cache_hit is not None:
                    return await _stream_file_response(
                        request,
                        cache_hit.path,
                        allowed_root=settings.cache_dir,
                        status=upstream.status,
                        headers=_derivative_headers(headers),
                    )

        derivative_headers = _derivative_headers(headers)
        response_path: Path
        async with request.app[CONVERSION_SEMAPHORE_KEY]:
            # A concurrent request may have populated this key while this
            # request downloaded its source or waited for the CPU slot.
            async with request.app[RUNTIME_KEY].cache_gate.reader():
                cache_hit = await _cache_lookup(
                    request,
                    cache_key=cache_key,
                    profile=profile,
                    status=upstream.status,
                    started=started,
                )
                if cache_hit is not None:
                    return await _stream_file_response(
                        request,
                        cache_hit.path,
                        allowed_root=settings.cache_dir,
                        status=upstream.status,
                        headers=derivative_headers,
                    )
            if cache_hit is None:
                _log_event(
                    request,
                    logging.INFO,
                    "cache_miss",
                    profile=profile,
                    status=upstream.status,
                    original_bytes=received,
                )
                _log_event(
                    request,
                    logging.INFO,
                    "optimizer_start",
                    profile=profile,
                    status=upstream.status,
                    original_bytes=received,
                )
                request.app[RUNTIME_KEY].begin_conversion(profile)
                try:
                    result: OptimizationResult = await asyncio.to_thread(
                        optimize_epub,
                        source_path,
                        derivative_path,
                        profile=profile,
                        quality=settings.optimizer_jpeg_quality,
                        max_image_pixels=settings.optimizer_max_image_pixels,
                    )
                except Exception as exc:
                    _log_event(
                        request,
                        logging.ERROR,
                        "optimizer_fallback",
                        profile=profile,
                        status=upstream.status,
                        duration_ms=round((time.monotonic() - started) * 1000),
                        original_bytes=received,
                        fallback_reason=type(exc).__name__,
                    )
                    derivative_headers = headers.copy()
                    derivative_headers["Cache-Control"] = "private, no-store"
                    response_path = source_path
                else:
                    _log_event(
                        request,
                        logging.INFO,
                        "optimizer_complete",
                        profile=profile,
                        status=upstream.status,
                        duration_ms=round(result.duration_seconds * 1000),
                        original_bytes=result.source_bytes,
                        output_bytes=result.output_bytes,
                        savings_percent=round(result.savings_percent, 1),
                        image_count=result.image_count,
                        repair_count=result.repair_count,
                    )
                    response_path = derivative_path
                    try:
                        published = await asyncio.to_thread(
                            request.app[DERIVATIVE_CACHE_KEY].publish,
                            cache_key,
                            profile=profile,
                            derivative_path=derivative_path,
                            original_bytes=result.source_bytes,
                        )
                    except Exception as exc:
                        _log_event(
                            request,
                            logging.ERROR,
                            "cache_store_failed",
                            profile=profile,
                            status=upstream.status,
                            cache_reason=type(exc).__name__,
                        )
                    else:
                        _log_event(
                            request,
                            logging.INFO,
                            "cache_store",
                            profile=profile,
                            status=upstream.status,
                            original_bytes=published.original_bytes,
                            output_bytes=published.output_bytes,
                            savings_percent=round(published.savings_percent, 1),
                        )
                finally:
                    request.app[RUNTIME_KEY].end_conversion()

        return await _stream_file_response(
            request,
            response_path,
            allowed_root=temp_dir,
            status=upstream.status,
            headers=derivative_headers,
        )


async def _stream_upstream_response(
    request: web.Request, upstream, *, headers: CIMultiDict[str]
) -> web.StreamResponse:
    downstream = web.StreamResponse(status=upstream.status, headers=headers)
    await downstream.prepare(request)
    try:
        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await downstream.write(chunk)
    except ConnectionResetError:
        return downstream
    with suppress(ConnectionResetError):
        await downstream.write_eof()
    return downstream


async def proxy_opds(request: web.Request) -> web.StreamResponse:
    profile = request.match_info["profile"]
    if profile not in PROFILES:
        raise web.HTTPNotFound(text="unknown profile\n")

    tail = request.match_info.get("tail", "")
    optimize_epub_request = request.method == "GET" and _is_optimized_epub_request(
        profile, tail
    )
    return await _proxy_opds_request(
        request,
        profile=profile,
        tail=tail,
        optimize_epub_request=optimize_epub_request,
    )


async def profile_alias(request: web.Request) -> web.StreamResponse:
    """Serve a direct X3/X4 root feed without redirecting the OPDS client."""

    requested_profile = request.match_info["profile_alias"]
    return await _proxy_opds_request(
        request,
        profile=requested_profile.lower(),
        tail="",
        optimize_epub_request=False,
        bridge_prefix=f"/opds/{requested_profile}",
    )


async def _proxy_opds_request(
    request: web.Request,
    *,
    profile: str,
    tail: str,
    optimize_epub_request: bool,
    bridge_prefix: str | None = None,
) -> web.StreamResponse:
    settings = request.app[SETTINGS_KEY]
    upstream_url = _upstream_url(
        settings,
        profile=profile,
        tail=tail,
        raw_path=request.rel_url.raw_path,
        raw_query_string=request.rel_url.raw_query_string,
        bridge_prefix=bridge_prefix,
    )
    start = time.monotonic()
    auth_present = "Authorization" in request.headers

    try:
        upstream = await request.app[CLIENT_SESSION_KEY].request(
            request.method,
            upstream_url,
            headers=_request_headers(
                request, require_complete_body=optimize_epub_request
            ),
            allow_redirects=False,
        )
    except (ClientError, asyncio.TimeoutError):
        _log_event(
            request,
            logging.ERROR,
            "upstream_unavailable",
            profile=profile,
            upstream_route=_safe_upstream_route(upstream_url.path),
            duration_ms=round((time.monotonic() - start) * 1000),
            auth_present=auth_present,
        )
        raise web.HTTPBadGateway(text="CWA upstream unavailable\n")

    try:
        headers = _response_headers(upstream.headers)
        location = upstream.headers.get("Location")
        if location:
            headers["Location"] = rewrite_cwa_href(
                location,
                profile=profile,
                upstream_origin=settings.upstream_url,
                upstream_request_url=upstream_url,
            )

        content_type = upstream.headers.get("Content-Type", "")
        if (
            optimize_epub_request
            and upstream.status == 200
            and content_type.partition(";")[0].strip().lower() == "application/epub+zip"
        ):
            return await _optimized_epub_response(
                request,
                upstream,
                profile=profile,
                headers=headers,
                settings=settings,
                upstream_url=upstream_url,
                started=start,
            )
        if (
            request.method != "HEAD"
            and upstream.status == 200
            and _is_xml_content_type(content_type)
        ):
            try:
                payload = await _read_bounded(upstream, settings.feed_max_bytes)
                rewritten, rewritten_count = rewrite_feed(
                    payload,
                    profile=profile,
                    upstream_origin=settings.upstream_url,
                    upstream_request_url=upstream_url,
                )
            except FeedError:
                _log_event(
                    request,
                    logging.ERROR,
                    "feed_rewrite_failed",
                    profile=profile,
                    upstream_route=_safe_upstream_route(upstream_url.path),
                    status=upstream.status,
                )
                raise web.HTTPBadGateway(text="Invalid CWA OPDS feed\n")

            for name in ("Content-Length", "Content-Range", "ETag", "Last-Modified"):
                headers.popall(name, None)
            headers["Cache-Control"] = "private, no-store"
            _log_event(
                request,
                logging.INFO,
                "feed_rewritten",
                profile=profile,
                upstream_route=_safe_upstream_route(upstream_url.path),
                status=upstream.status,
                duration_ms=round((time.monotonic() - start) * 1000),
                links_rewritten=rewritten_count,
                auth_present=auth_present,
            )
            return web.Response(status=upstream.status, body=rewritten, headers=headers)

        downstream = await _stream_upstream_response(request, upstream, headers=headers)
        _log_event(
            request,
            logging.INFO,
            "response_proxied",
            profile=profile,
            upstream_route=_safe_upstream_route(upstream_url.path),
            status=upstream.status,
            duration_ms=round((time.monotonic() - start) * 1000),
            auth_present=auth_present,
        )
        return downstream
    finally:
        upstream.release()


async def _client_session_context(app: web.Application):
    settings = app[SETTINGS_KEY]
    timeout = ClientTimeout(
        total=None,
        connect=settings.connect_timeout_seconds,
        sock_connect=settings.connect_timeout_seconds,
        sock_read=settings.read_timeout_seconds,
    )
    app[CLIENT_SESSION_KEY] = ClientSession(
        timeout=timeout,
        connector=TCPConnector(limit=20, limit_per_host=20),
        auto_decompress=False,
    )
    app[RUNTIME_KEY].client_session = app[CLIENT_SESSION_KEY]
    yield
    await app[CLIENT_SESSION_KEY].close()
    app[RUNTIME_KEY].client_session = None


async def _activity_context(app: web.Application):
    runtime = app[RUNTIME_KEY]
    started = False
    try:
        runtime.activity.start()
        started = True
    except Exception as exc:
        LOGGER.error("activity_unavailable", extra={"cache_reason": type(exc).__name__})
    yield
    if started:
        await runtime.activity.close()


def create_app(
    settings: Settings | None = None, runtime: BridgeRuntime | None = None
) -> web.Application:
    selected_settings = settings or Settings.from_env()
    selected_runtime = runtime or BridgeRuntime(selected_settings)
    app = web.Application(client_max_size=1024, middlewares=[public_security_headers])
    app[SETTINGS_KEY] = selected_settings
    app[RUNTIME_KEY] = selected_runtime
    app[CONVERSION_SEMAPHORE_KEY] = selected_runtime.conversion_semaphore
    app[DERIVATIVE_CACHE_KEY] = selected_runtime.cache
    app.cleanup_ctx.extend((_activity_context, _client_session_context))
    add_public_routes(app)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/opds", profile_feed)
    app.router.add_get("/opds/", profile_feed)
    for method in ("GET", "HEAD"):
        app.router.add_route(method, "/opds/{profile_alias:X3|X4|x3|x4}", profile_alias)
        app.router.add_route(
            method, "/opds/{profile_alias:X3|X4|x3|x4}/", profile_alias
        )
        app.router.add_route(
            method, "/opds/crosspoint/{profile:x3|x4|original}", proxy_opds
        )
        app.router.add_route(
            method, "/opds/crosspoint/{profile:x3|x4|original}/", proxy_opds
        )
        app.router.add_route(
            method,
            "/opds/crosspoint/{profile:x3|x4|original}/{tail:.*}",
            proxy_opds,
        )
    return app


async def _serve(settings: Settings, runtime: BridgeRuntime) -> None:
    public_runner = web.AppRunner(create_app(settings, runtime), access_log=None)
    await public_runner.setup()
    public_site = web.TCPSite(public_runner, settings.host, settings.port)
    await public_site.start()

    admin_runner = await _start_admin_listener(settings, runtime)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    try:
        await stop.wait()
    finally:
        runtime.admin_listening = False
        if admin_runner is not None:
            await admin_runner.cleanup()
        await public_runner.cleanup()


async def _start_admin_listener(
    settings: Settings, runtime: BridgeRuntime
) -> web.AppRunner | None:
    admin_runner: web.AppRunner | None = None
    if (
        settings.admin_tls_certificate.is_file()
        and not settings.admin_tls_certificate.is_symlink()
        and settings.admin_tls_private_key.is_file()
        and not settings.admin_tls_private_key.is_symlink()
    ):
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(
                settings.admin_tls_certificate, settings.admin_tls_private_key
            )
            admin_runner = web.AppRunner(create_admin_app(runtime), access_log=None)
            await admin_runner.setup()
            admin_site = web.TCPSite(
                admin_runner,
                settings.admin_host,
                settings.admin_port,
                ssl_context=context,
            )
            await admin_site.start()
        except (OSError, ssl.SSLError, ValueError) as exc:
            LOGGER.warning("admin_disabled", extra={"cache_reason": type(exc).__name__})
            if admin_runner is not None:
                await admin_runner.cleanup()
                admin_runner = None
        else:
            runtime.admin_listening = True
            LOGGER.info("admin_started")
    else:
        LOGGER.warning("admin_disabled", extra={"cache_reason": "tls_unavailable"})

    return admin_runner


def main() -> None:
    configure_logging()
    base_settings = Settings.from_env()
    settings_store = SettingsStore(base_settings.admin_state_dir)
    settings, settings_error = settings_store.apply_active(base_settings)
    runtime = BridgeRuntime(settings, settings_error=settings_error)
    LOGGER.info("bridge_starting")
    asyncio.run(_serve(settings, runtime))


if __name__ == "__main__":
    main()
