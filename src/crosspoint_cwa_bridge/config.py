"""Validated runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from yarl import URL


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def validate_upstream_url(raw: str) -> URL:
    url = URL(raw)
    if url.scheme not in {"http", "https"}:
        raise ValueError("CWA_UPSTREAM_URL must use http or https")
    if not url.host or url.user or url.password:
        raise ValueError("CWA_UPSTREAM_URL must be an origin without credentials")
    if url.path not in {"", "/"} or url.query_string or url.fragment:
        raise ValueError("CWA_UPSTREAM_URL must not include a path, query, or fragment")
    return url.with_path("")


def validate_public_url(raw: str, *, scheme: str, name: str) -> str:
    url = URL(raw)
    if (
        url.scheme != scheme
        or not url.host
        or url.user
        or url.password
        or url.path not in {"", "/"}
        or url.query_string
        or url.fragment
    ):
        raise ValueError(f"{name} must be a credential-free {scheme} origin")
    return str(url.with_path(""))


def _jpeg_quality() -> int:
    value = _positive_int("OPTIMIZER_JPEG_QUALITY", 85)
    if value > 95:
        raise ValueError("OPTIMIZER_JPEG_QUALITY must be at most 95")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    upstream_url: URL
    host: str = "0.0.0.0"
    port: int = 8094
    public_base_url: str = "http://127.0.0.1:8094"
    admin_host: str = "0.0.0.0"
    admin_port: int = 8095
    admin_public_url: str = "https://127.0.0.1:8095"
    admin_state_dir: Path = Path("/tmp/crosspoint-cwa-bridge-admin")
    admin_tls_certificate: Path = Path("/tmp/crosspoint-cwa-bridge-admin/tls.crt")
    admin_tls_private_key: Path = Path("/tmp/crosspoint-cwa-bridge-admin/tls.key")
    feed_max_bytes: int = 8 * 1024 * 1024
    cache_dir: Path = Path("/tmp/crosspoint-cwa-bridge-cache")
    work_dir: Path = Path("/tmp/crosspoint-cwa-bridge-work")
    optimizer_jpeg_quality: int = 85
    optimizer_max_image_pixels: int = 40_000_000
    optimizer_max_epub_bytes: int = 2 * 1024 * 1024 * 1024
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            upstream_url=validate_upstream_url(
                os.environ.get("CWA_UPSTREAM_URL", "http://cwa:8083")
            ),
            host=os.environ.get("BRIDGE_HOST", "0.0.0.0"),
            port=_positive_int("BRIDGE_PORT", 8094),
            public_base_url=validate_public_url(
                os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8094"),
                scheme="http",
                name="PUBLIC_BASE_URL",
            ),
            admin_host=os.environ.get("ADMIN_HOST", "0.0.0.0"),
            admin_port=_positive_int("ADMIN_PORT", 8095),
            admin_public_url=validate_public_url(
                os.environ.get("ADMIN_PUBLIC_URL", "https://127.0.0.1:8095"),
                scheme="https",
                name="ADMIN_PUBLIC_URL",
            ),
            admin_state_dir=Path(
                os.environ.get(
                    "ADMIN_STATE_DIR", "/var/lib/crosspoint-cwa-bridge/admin"
                )
            ),
            admin_tls_certificate=Path(
                os.environ.get(
                    "ADMIN_TLS_CERTIFICATE",
                    "/var/lib/crosspoint-cwa-bridge/admin/tls.crt",
                )
            ),
            admin_tls_private_key=Path(
                os.environ.get(
                    "ADMIN_TLS_PRIVATE_KEY",
                    "/var/lib/crosspoint-cwa-bridge/admin/tls.key",
                )
            ),
            feed_max_bytes=_positive_int("FEED_MAX_BYTES", 8 * 1024 * 1024),
            cache_dir=Path(
                os.environ.get("CACHE_DIR", "/var/cache/crosspoint-cwa-bridge")
            ),
            work_dir=Path(
                os.environ.get("WORK_DIR", "/var/lib/crosspoint-cwa-bridge/work")
            ),
            optimizer_jpeg_quality=_jpeg_quality(),
            optimizer_max_image_pixels=_positive_int(
                "OPTIMIZER_MAX_IMAGE_PIXELS", 40_000_000
            ),
            optimizer_max_epub_bytes=_positive_int(
                "OPTIMIZER_MAX_EPUB_BYTES", 2 * 1024 * 1024 * 1024
            ),
            connect_timeout_seconds=_positive_float(
                "UPSTREAM_CONNECT_TIMEOUT_SECONDS", 10.0
            ),
            read_timeout_seconds=_positive_float("UPSTREAM_READ_TIMEOUT_SECONDS", 60.0),
        )
