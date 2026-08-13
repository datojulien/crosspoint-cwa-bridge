"""Shared runtime state for the OPDS and administration applications."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import signal
import ssl
import time
from typing import AsyncIterator, Awaitable, Callable

from aiohttp import ClientError, ClientSession

from .admin_state import (
    ActivityStore,
    LoginLimiter,
    PasswordStore,
    SessionStore,
    SettingsStore,
)
from .cache import DerivativeCache
from .config import Settings


class CacheGate:
    """Reader/writer coordination for downloads and destructive cache maintenance."""

    def __init__(self):
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @asynccontextmanager
    async def reader(self) -> AsyncIterator[None]:
        async with self._condition:
            while self._writer or self._waiting_writers:
                await self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @asynccontextmanager
    async def writer(self) -> AsyncIterator[None]:
        async with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    await self._condition.wait()
                self._writer = True
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            async with self._condition:
                self._writer = False
                self._condition.notify_all()


@dataclass(slots=True)
class UpstreamStatus:
    state: str = "unknown"
    status: int | None = None
    latency_ms: int | None = None
    checked_at: str | None = None
    checked_monotonic: float = 0.0


RestartCallback = Callable[[], Awaitable[None]]


class BridgeRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        settings_error: str | None = None,
        restart_callback: RestartCallback | None = None,
    ):
        self.settings = settings
        self.settings_error = settings_error
        self.started_at = datetime.now(timezone.utc)
        self.started_monotonic = time.monotonic()
        self.cache = DerivativeCache(settings.cache_dir)
        self.cache_gate = CacheGate()
        self.conversion_semaphore = asyncio.Semaphore(1)
        self.settings_store = SettingsStore(settings.admin_state_dir)
        self.password_store = PasswordStore(settings.admin_state_dir)
        self.sessions = SessionStore()
        self.login_limiter = LoginLimiter()
        self.activity = ActivityStore(settings.admin_state_dir)
        self.client_session: ClientSession | None = None
        self.upstream = UpstreamStatus()
        self._upstream_lock = asyncio.Lock()
        self.active_conversion_profile: str | None = None
        self.active_conversion_started: float | None = None
        self.restart_callback = restart_callback or self._default_restart
        self.admin_listening = False

    async def _default_restart(self) -> None:
        await asyncio.sleep(0.75)
        os.kill(os.getpid(), signal.SIGTERM)

    def record(self, event: str, **fields) -> None:
        self.activity.record(event, **fields)

    def begin_conversion(self, profile: str) -> None:
        self.active_conversion_profile = profile
        self.active_conversion_started = time.monotonic()

    def end_conversion(self) -> None:
        self.active_conversion_profile = None
        self.active_conversion_started = None

    def conversion_status(self) -> dict[str, str | int | None]:
        duration: int | None = None
        if self.active_conversion_started is not None:
            duration = round(time.monotonic() - self.active_conversion_started)
        return {
            "active": self.active_conversion_profile is not None,
            "profile": self.active_conversion_profile,
            "duration_seconds": duration,
        }

    async def cache_stats(self, *, verify_checksums: bool = False):
        async with self.cache_gate.reader():
            return await asyncio.to_thread(
                self.cache.inspect, verify_checksums=verify_checksums
            )

    async def purge_cache(self, scope: str):
        async with self.conversion_semaphore:
            async with self.cache_gate.writer():
                return await asyncio.to_thread(self.cache.purge, scope)

    async def probe_upstream(self, *, force: bool = False) -> UpstreamStatus:
        now = time.monotonic()
        if not force and now - self.upstream.checked_monotonic < 60:
            return self.upstream
        async with self._upstream_lock:
            now = time.monotonic()
            if not force and now - self.upstream.checked_monotonic < 60:
                return self.upstream
            if self.client_session is None:
                return self.upstream
            started = time.monotonic()
            status: int | None = None
            state = "unreachable"
            try:
                response = await self.client_session.get(
                    self.settings.upstream_url / "opds",
                    headers={"Accept-Encoding": "identity"},
                    allow_redirects=False,
                )
                status = response.status
                response.close()
                state = "reachable"
            except (ClientError, asyncio.TimeoutError):
                pass
            self.upstream = UpstreamStatus(
                state=state,
                status=status,
                latency_ms=round((time.monotonic() - started) * 1000),
                checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                checked_monotonic=time.monotonic(),
            )
            return self.upstream

    def storage_status(self) -> dict[str, int | bool]:
        try:
            usage = shutil.disk_usage(self.settings.cache_dir)
            cache_writable = os.access(self.settings.cache_dir, os.W_OK)
        except OSError:
            usage = shutil.disk_usage(Path("/tmp"))
            cache_writable = False
        return {
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "cache_writable": cache_writable,
            "state_writable": os.access(self.settings.admin_state_dir, os.W_OK),
        }

    def tls_status(self) -> dict[str, str | bool | None]:
        certificate = self.settings.admin_tls_certificate
        private_key = self.settings.admin_tls_private_key
        configured = (
            certificate.is_file()
            and not certificate.is_symlink()
            and private_key.is_file()
            and not private_key.is_symlink()
        )
        result: dict[str, str | bool | None] = {
            "configured": configured,
            "listening": self.admin_listening,
            "expires_at": None,
        }
        if not configured:
            return result
        try:
            decoded = ssl._ssl._test_decode_cert(str(certificate))
            result["expires_at"] = decoded.get("notAfter")
        except (OSError, ssl.SSLError, ValueError):
            result["configured"] = False
        return result


__all__ = ["BridgeRuntime", "CacheGate", "UpstreamStatus"]
