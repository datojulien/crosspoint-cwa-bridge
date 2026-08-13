"""Private, bridge-owned state for the local administration console."""

from __future__ import annotations

import asyncio
import base64
from collections import defaultdict, deque
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import secrets
import sqlite3
import stat
import tempfile
import threading
import time
from typing import Any, Mapping

from .config import Settings


ADMIN_USERNAME = "bridge-admin"
SESSION_COOKIE = "crosspoint_admin_session"
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 1024
SESSION_IDLE_SECONDS = 30 * 60
SESSION_MAX_SECONDS = 8 * 60 * 60
ACTIVITY_RETENTION_SECONDS = 7 * 24 * 60 * 60
ACTIVITY_MAX_ROWS = 10_000
ACTIVITY_QUEUE_SIZE = 1_000
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60

_ACTIVITY_FIELDS = (
    "profile",
    "status",
    "duration_ms",
    "original_bytes",
    "output_bytes",
    "savings_percent",
    "image_count",
    "repair_count",
)
_ACTIVITY_EVENTS = {
    "admin_cache_purged",
    "admin_diagnostics",
    "admin_login_failed",
    "admin_login_succeeded",
    "admin_password_changed",
    "admin_restart_requested",
    "admin_settings_saved",
    "cache_hit",
    "cache_invalid",
    "cache_lookup_failed",
    "cache_miss",
    "cache_store",
    "cache_store_failed",
    "feed_rewrite_failed",
    "feed_rewritten",
    "optimizer_complete",
    "optimizer_fallback",
    "optimizer_start",
    "response_proxied",
    "upstream_unavailable",
}

SAFE_SETTING_SPECS: dict[str, tuple[type, float, float]] = {
    "feed_max_bytes": (int, 64 * 1024, 64 * 1024 * 1024),
    "optimizer_jpeg_quality": (int, 1, 95),
    "optimizer_max_image_pixels": (int, 1_000_000, 200_000_000),
    "optimizer_max_epub_bytes": (int, 16 * 1024 * 1024, 4 * 1024**3),
    "connect_timeout_seconds": (float, 1.0, 60.0),
    "read_timeout_seconds": (float, 5.0, 600.0),
}


class AdminStateError(RuntimeError):
    """An administration state file is missing, unsafe, or invalid."""


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        raise AdminStateError("admin state path must be a real directory")
    path.chmod(0o700)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    ensure_private_directory(path.parent)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.chmod(0o600)
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _read_private_json(path: Path, maximum_bytes: int = 64 * 1024) -> dict[str, Any]:
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            raise AdminStateError("state file must be a regular file")
        if path.stat().st_size > maximum_bytes:
            raise AdminStateError("state file exceeds its size limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdminStateError("state file could not be read") from exc
    if not isinstance(payload, dict):
        raise AdminStateError("state file must contain a JSON object")
    return payload


class PasswordStore:
    """Store and verify one salted scrypt password hash."""

    N = 2**14
    R = 8
    P = 1
    DKLEN = 32

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "credentials.json"

    @staticmethod
    def validate_new_password(password: str) -> None:
        if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
            raise ValueError(
                f"password must be {PASSWORD_MIN_LENGTH}–{PASSWORD_MAX_LENGTH} characters"
            )

    @classmethod
    def _derive(cls, password: str, *, salt: bytes, n: int, r: int, p: int) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=cls.DKLEN,
            maxmem=32 * 1024 * 1024,
        )

    def configured(self) -> bool:
        try:
            self._load()
        except (FileNotFoundError, AdminStateError):
            return False
        return True

    def set_password(self, password: str) -> None:
        self.validate_new_password(password)
        salt = secrets.token_bytes(16)
        derived = self._derive(password, salt=salt, n=self.N, r=self.R, p=self.P)
        atomic_json_write(
            self.path,
            {
                "schema": 1,
                "username": ADMIN_USERNAME,
                "algorithm": "scrypt",
                "n": self.N,
                "r": self.R,
                "p": self.P,
                "salt": base64.b64encode(salt).decode("ascii"),
                "digest": base64.b64encode(derived).decode("ascii"),
            },
        )

    def _load(self) -> tuple[bytes, bytes, int, int, int]:
        payload = _read_private_json(self.path)
        try:
            if (
                payload.get("schema") != 1
                or payload.get("username") != ADMIN_USERNAME
                or payload.get("algorithm") != "scrypt"
            ):
                raise ValueError
            n = int(payload["n"])
            r = int(payload["r"])
            p = int(payload["p"])
            if (n, r, p) != (self.N, self.R, self.P):
                raise ValueError
            salt = base64.b64decode(payload["salt"], validate=True)
            digest = base64.b64decode(payload["digest"], validate=True)
            if len(salt) != 16 or len(digest) != self.DKLEN:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise AdminStateError("credential record is invalid") from exc
        return salt, digest, n, r, p

    def verify(self, username: str, password: str) -> bool:
        if username != ADMIN_USERNAME or len(password) > PASSWORD_MAX_LENGTH:
            return False
        try:
            salt, expected, n, r, p = self._load()
            actual = self._derive(password, salt=salt, n=n, r=r, p=p)
        except (FileNotFoundError, AdminStateError, ValueError):
            return False
        return hmac.compare_digest(actual, expected)


@dataclass(slots=True)
class AdminSession:
    csrf_token: str
    created_at: float
    last_seen: float


class SessionStore:
    """In-memory opaque sessions; every bridge restart invalidates them."""

    def __init__(self):
        self._sessions: dict[str, AdminSession] = {}

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()

    def create(self, now: float | None = None) -> tuple[str, AdminSession]:
        current = time.monotonic() if now is None else now
        for key, session in list(self._sessions.items()):
            if (
                current - session.last_seen > SESSION_IDLE_SECONDS
                or current - session.created_at > SESSION_MAX_SECONDS
            ):
                self._sessions.pop(key, None)
        if len(self._sessions) >= 64:
            oldest = min(self._sessions, key=lambda key: self._sessions[key].last_seen)
            self._sessions.pop(oldest, None)
        token = secrets.token_urlsafe(32)
        session = AdminSession(
            csrf_token=secrets.token_urlsafe(32),
            created_at=current,
            last_seen=current,
        )
        self._sessions[self._key(token)] = session
        return token, session

    def get(self, token: str | None, now: float | None = None) -> AdminSession | None:
        if not token:
            return None
        current = time.monotonic() if now is None else now
        key = self._key(token)
        session = self._sessions.get(key)
        if session is None:
            return None
        if (
            current - session.last_seen > SESSION_IDLE_SECONDS
            or current - session.created_at > SESSION_MAX_SECONDS
        ):
            self._sessions.pop(key, None)
            return None
        session.last_seen = current
        return session

    def remove(self, token: str | None) -> None:
        if token:
            self._sessions.pop(self._key(token), None)

    def invalidate_all(self) -> None:
        self._sessions.clear()


class LoginLimiter:
    """Small in-memory per-client limiter; addresses are never persisted."""

    def __init__(self):
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, client: str, now: float) -> deque[float]:
        failures = self._failures[client]
        cutoff = now - LOGIN_FAILURE_WINDOW_SECONDS
        while failures and failures[0] < cutoff:
            failures.popleft()
        if not failures:
            self._failures.pop(client, None)
            return deque()
        return failures

    def allowed(self, client: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        return len(self._prune(client, current)) < LOGIN_FAILURE_LIMIT

    def failed(self, client: str, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        if client not in self._failures and len(self._failures) >= 1024:
            oldest = min(
                self._failures,
                key=lambda key: self._failures[key][-1]
                if self._failures[key]
                else float("-inf"),
            )
            self._failures.pop(oldest, None)
        failures = self._prune(client, current)
        failures.append(current)
        self._failures[client] = failures

    def succeeded(self, client: str) -> None:
        self._failures.pop(client, None)


def safe_settings(settings: Settings) -> dict[str, int | float]:
    return {name: getattr(settings, name) for name in SAFE_SETTING_SPECS}


def validate_safe_settings(payload: Mapping[str, Any]) -> dict[str, int | float]:
    if set(payload) != set(SAFE_SETTING_SPECS):
        raise ValueError("settings must include exactly the supported fields")
    validated: dict[str, int | float] = {}
    for name, (expected_type, minimum, maximum) in SAFE_SETTING_SPECS.items():
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be numeric")
        if expected_type is int and not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        converted = expected_type(value)
        if not minimum <= converted <= maximum:
            raise ValueError(f"{name} is outside the allowed range")
        validated[name] = converted
    return validated


class SettingsStore:
    def __init__(self, state_dir: Path):
        root = Path(state_dir)
        self.active_path = root / "active-settings.json"
        self.pending_path = root / "pending-settings.json"

    @staticmethod
    def _load(path: Path) -> dict[str, int | float]:
        return validate_safe_settings(_read_private_json(path))

    def load_active(self) -> tuple[dict[str, int | float] | None, str | None]:
        try:
            return self._load(self.active_path), None
        except FileNotFoundError:
            return None, None
        except (AdminStateError, ValueError):
            return None, "invalid_active_settings"

    def load_pending(self) -> tuple[dict[str, int | float] | None, str | None]:
        try:
            return self._load(self.pending_path), None
        except FileNotFoundError:
            return None, None
        except (AdminStateError, ValueError):
            return None, "invalid_pending_settings"

    def apply_active(self, settings: Settings) -> tuple[Settings, str | None]:
        active, error = self.load_active()
        if active is None:
            return settings, error
        return replace(settings, **active), error

    def save_pending(self, payload: Mapping[str, Any]) -> dict[str, int | float]:
        validated = validate_safe_settings(payload)
        atomic_json_write(self.pending_path, validated)
        return validated

    def promote_pending(self) -> bool:
        pending, error = self.load_pending()
        if error:
            raise AdminStateError(error)
        if pending is None:
            return False
        atomic_json_write(self.active_path, pending)
        self.pending_path.unlink(missing_ok=True)
        _fsync_directory(self.active_path.parent)
        return True


class ActivityStore:
    """Bounded seven-day SQLite activity journal with an async write queue."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "activity.sqlite3"
        self.queue: queue.Queue[dict[str, Any] | None] = queue.Queue(
            maxsize=ACTIVITY_QUEUE_SIZE
        )
        self.dropped = 0
        self._thread: threading.Thread | None = None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        ensure_private_directory(self.path.parent)
        if self.path.exists():
            mode = self.path.lstat().st_mode
            if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                raise AdminStateError("activity database must be a regular file")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at REAL NOT NULL,
                    event TEXT NOT NULL,
                    profile TEXT,
                    status INTEGER,
                    duration_ms INTEGER,
                    original_bytes INTEGER,
                    output_bytes INTEGER,
                    savings_percent REAL,
                    image_count INTEGER,
                    repair_count INTEGER
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS events_occurred_at ON events(occurred_at)"
            )
            self._prune(connection)
        self.path.chmod(0o600)

    @staticmethod
    def _prune(connection: sqlite3.Connection) -> None:
        cutoff = time.time() - ACTIVITY_RETENTION_SECONDS
        connection.execute("DELETE FROM events WHERE occurred_at < ?", (cutoff,))
        connection.execute(
            """
            DELETE FROM events
            WHERE id NOT IN (
                SELECT id FROM events ORDER BY id DESC LIMIT ?
            )
            """,
            (ACTIVITY_MAX_ROWS,),
        )

    def start(self) -> None:
        if self._thread is not None:
            return
        self._initialize()
        self._thread = threading.Thread(
            target=self._writer, name="bridge-activity-writer", daemon=True
        )
        self._thread.start()

    def _writer(self) -> None:
        while True:
            item = self.queue.get()
            try:
                if item is None:
                    return
                with closing(self._connect()) as connection, connection:
                    columns = ("occurred_at", "event", *_ACTIVITY_FIELDS)
                    connection.execute(
                        f"INSERT INTO events ({','.join(columns)}) "
                        f"VALUES ({','.join('?' for _ in columns)})",
                        tuple(item.get(column) for column in columns),
                    )
                    self._prune(connection)
            finally:
                self.queue.task_done()

    def record(self, event: str, **fields: Any) -> None:
        if event not in _ACTIVITY_EVENTS or self._thread is None:
            return
        item: dict[str, Any] = {"occurred_at": time.time(), "event": event}
        for name in _ACTIVITY_FIELDS:
            value = fields.get(name)
            if name == "profile" and value in {"x3", "x4", "original", "all"}:
                item[name] = value
            elif (
                name != "profile"
                and not isinstance(value, bool)
                and isinstance(value, (int, float))
            ):
                item[name] = value
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            self.dropped += 1

    async def flush(self) -> None:
        await asyncio.to_thread(self.queue.join)

    async def close(self) -> None:
        if self._thread is None:
            return
        await self.flush()
        self.queue.put(None)
        await asyncio.to_thread(self._thread.join, 5)
        self._thread = None

    def _recent(self, limit: int) -> list[dict[str, Any]]:
        bounded = min(max(limit, 1), 250)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT occurred_at, event, profile, status, duration_ms,
                       original_bytes, output_bytes, savings_percent,
                       image_count, repair_count
                FROM events ORDER BY id DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            event = {name: row[name] for name in row.keys() if row[name] is not None}
            event["occurred_at"] = datetime.fromtimestamp(
                row["occurred_at"], tz=timezone.utc
            ).isoformat(timespec="seconds")
            events.append(event)
        return events

    async def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        await self.flush()
        return await asyncio.to_thread(self._recent, limit)


__all__ = [
    "ACTIVITY_MAX_ROWS",
    "ACTIVITY_RETENTION_SECONDS",
    "ADMIN_USERNAME",
    "AdminSession",
    "AdminStateError",
    "ActivityStore",
    "LoginLimiter",
    "PasswordStore",
    "SAFE_SETTING_SPECS",
    "SESSION_COOKIE",
    "SessionStore",
    "SettingsStore",
    "atomic_json_write",
    "ensure_private_directory",
    "safe_settings",
    "validate_safe_settings",
]
