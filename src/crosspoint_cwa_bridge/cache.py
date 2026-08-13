"""Persistent, content-validated cache for optimized EPUB derivatives."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Mapping


CACHE_SCHEMA_VERSION = 1
MAX_METADATA_BYTES = 64 * 1024
_CACHE_KEY_PATTERN = re.compile(r"[0-9a-f]{64}")
_PROFILES = {"x3", "x4"}


class CacheError(RuntimeError):
    """A cache operation failed without invalidating the source or derivative."""


@dataclass(frozen=True, slots=True)
class CacheHit:
    path: Path
    original_bytes: int
    output_bytes: int

    @property
    def savings_percent(self) -> float:
        if not self.original_bytes:
            return 0.0
        return (self.original_bytes - self.output_bytes) * 100.0 / self.original_bytes


@dataclass(frozen=True, slots=True)
class CacheLookup:
    hit: CacheHit | None
    invalid_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CacheProfileStats:
    entries: int = 0
    original_bytes: int = 0
    output_bytes: int = 0
    invalid_entries: int = 0


def http_source_version(headers: Mapping[str, str]) -> dict[str, str] | None:
    """Return a reusable HTTP representation identity, or ``None`` if unsafe.

    A digest or entity tag is independently usable. Last-Modified is only used
    with Content-Length so a timestamp alone cannot alias differently sized
    source files. Header values become input to a hash and are never persisted.
    """

    length = headers.get("Content-Length", "")
    normalized_length = length if length.isdigit() else ""
    content_digest = headers.get("Content-Digest") or headers.get("Digest")
    etag = headers.get("ETag")
    last_modified = headers.get("Last-Modified")

    if content_digest:
        version = {"content_digest": content_digest}
        if normalized_length:
            version["content_length"] = normalized_length
        return version
    if etag:
        version = {"etag": etag}
        if normalized_length:
            version["content_length"] = normalized_length
        return version
    if last_modified and normalized_length:
        return {
            "last_modified": last_modified,
            "content_length": normalized_length,
        }
    return None


def content_source_version(source_sha256: str, source_bytes: int) -> dict[str, str]:
    if not _CACHE_KEY_PATTERN.fullmatch(source_sha256):
        raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
    if source_bytes < 0:
        raise ValueError("source_bytes must not be negative")
    return {"sha256": source_sha256, "content_length": str(source_bytes)}


def build_cache_key(
    *,
    source_identity: str,
    source_version: Mapping[str, str],
    profile: str,
    optimizer_version: str,
    jpeg_quality: int,
    max_image_pixels: int,
) -> str:
    """Build a stable opaque key without exposing the route as a path."""

    if profile not in _PROFILES:
        raise ValueError(f"unsupported cache profile: {profile}")
    if not source_identity or not source_version or not optimizer_version:
        raise ValueError("cache identity fields must not be empty")
    if not 1 <= jpeg_quality <= 95:
        raise ValueError("jpeg_quality must be between 1 and 95")
    if max_image_pixels <= 0:
        raise ValueError("max_image_pixels must be positive")

    payload = {
        "schema": CACHE_SCHEMA_VERSION,
        "source_identity_sha256": hashlib.sha256(
            source_identity.encode("utf-8")
        ).hexdigest(),
        "source_version": dict(sorted(source_version.items())),
        "profile": profile,
        "optimizer_version": optimizer_version,
        "jpeg_quality": jpeg_quality,
        "max_image_pixels": max_image_pixels,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DerivativeCache:
    """Manage atomically published X3/X4 derivatives beneath one owned root."""

    def __init__(self, root: Path):
        self.root = Path(root)

    @staticmethod
    def _validate_coordinates(key: str, profile: str) -> None:
        if profile not in _PROFILES:
            raise ValueError(f"unsupported cache profile: {profile}")
        if not _CACHE_KEY_PATTERN.fullmatch(key):
            raise ValueError("cache key must be a lowercase SHA-256 digest")

    def _paths(self, key: str, profile: str) -> tuple[Path, Path]:
        self._validate_coordinates(key, profile)
        directory = self.root / profile / key[:2]
        return directory / f"{key}.epub", directory / f"{key}.json"

    @staticmethod
    def _is_regular_file(path: Path) -> bool:
        try:
            return stat.S_ISREG(path.lstat().st_mode)
        except FileNotFoundError:
            return False

    @staticmethod
    def _remove(paths: tuple[Path, Path]) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def lookup(self, key: str, *, profile: str) -> CacheLookup:
        paths = self._paths(key, profile)
        epub_path, metadata_path = paths
        epub_exists = epub_path.exists() or epub_path.is_symlink()
        metadata_exists = metadata_path.exists() or metadata_path.is_symlink()
        if not epub_exists and not metadata_exists:
            return CacheLookup(None)
        if not self._is_regular_file(epub_path) or not self._is_regular_file(
            metadata_path
        ):
            self._remove(paths)
            return CacheLookup(None, "incomplete_or_unsafe_entry")

        try:
            if metadata_path.stat().st_size > MAX_METADATA_BYTES:
                raise ValueError("metadata_too_large")
            with metadata_path.open("rb") as stream:
                metadata = json.loads(stream.read(MAX_METADATA_BYTES + 1))
            if not isinstance(metadata, dict):
                raise ValueError("metadata_not_object")
            expected = {
                "schema": CACHE_SCHEMA_VERSION,
                "cache_key": key,
                "profile": profile,
            }
            if any(metadata.get(name) != value for name, value in expected.items()):
                raise ValueError("metadata_identity_mismatch")

            original_bytes = metadata.get("original_bytes")
            output_bytes = metadata.get("output_bytes")
            output_sha256 = metadata.get("output_sha256")
            if (
                type(original_bytes) is not int
                or original_bytes <= 0
                or type(output_bytes) is not int
                or output_bytes <= 0
                or not isinstance(output_sha256, str)
                or not _CACHE_KEY_PATTERN.fullmatch(output_sha256)
            ):
                raise ValueError("metadata_values_invalid")
            if epub_path.stat().st_size != output_bytes:
                raise ValueError("size_mismatch")
            if sha256_file(epub_path) != output_sha256:
                raise ValueError("checksum_mismatch")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._remove(paths)
            reason = str(exc) or type(exc).__name__
            return CacheLookup(None, reason)

        return CacheLookup(
            CacheHit(
                path=epub_path,
                original_bytes=original_bytes,
                output_bytes=output_bytes,
            )
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def publish(
        self,
        key: str,
        *,
        profile: str,
        derivative_path: Path,
        original_bytes: int,
    ) -> CacheHit:
        """Copy a validated derivative into the cache and publish metadata last."""

        epub_path, metadata_path = self._paths(key, profile)
        if original_bytes <= 0:
            raise ValueError("original_bytes must be positive")
        directory = epub_path.parent
        epub_temp: Path | None = None
        metadata_temp: Path | None = None

        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            epub_descriptor, epub_temp_name = tempfile.mkstemp(
                prefix=f".{key}.", suffix=".epub.tmp", dir=directory
            )
            epub_temp = Path(epub_temp_name)
            output_digest = hashlib.sha256()
            with (
                os.fdopen(epub_descriptor, "wb") as target,
                Path(derivative_path).open("rb") as source,
            ):
                for chunk in iter(lambda: source.read(64 * 1024), b""):
                    output_digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(epub_temp, 0o600)
            output_bytes = epub_temp.stat().st_size

            metadata = {
                "schema": CACHE_SCHEMA_VERSION,
                "cache_key": key,
                "profile": profile,
                "original_bytes": original_bytes,
                "output_bytes": output_bytes,
                "output_sha256": output_digest.hexdigest(),
            }
            metadata_descriptor, metadata_temp_name = tempfile.mkstemp(
                prefix=f".{key}.", suffix=".json.tmp", dir=directory
            )
            metadata_temp = Path(metadata_temp_name)
            with os.fdopen(metadata_descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    metadata,
                    stream,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(metadata_temp, 0o600)

            os.replace(epub_temp, epub_path)
            epub_temp = None
            os.replace(metadata_temp, metadata_path)
            metadata_temp = None
            self._fsync_directory(directory)
        except (OSError, ValueError) as exc:
            if epub_temp is not None:
                epub_temp.unlink(missing_ok=True)
            if metadata_temp is not None:
                metadata_temp.unlink(missing_ok=True)
            raise CacheError("failed to publish derivative cache entry") from exc

        return CacheHit(
            path=epub_path,
            original_bytes=original_bytes,
            output_bytes=output_bytes,
        )

    def inspect(
        self, *, verify_checksums: bool = False
    ) -> dict[str, CacheProfileStats]:
        """Return aggregate cache statistics without exposing keys or mutating files."""

        report: dict[str, CacheProfileStats] = {}
        for profile in sorted(_PROFILES):
            entries = original_bytes = output_bytes = invalid_entries = 0
            profile_root = self.root / profile
            if profile_root.is_symlink() or (
                profile_root.exists() and not profile_root.is_dir()
            ):
                report[profile] = CacheProfileStats(invalid_entries=1)
                continue
            metadata_paths = (
                list(profile_root.rglob("*.json")) if profile_root.is_dir() else []
            )
            metadata_epubs: set[Path] = set()
            for metadata_path in metadata_paths:
                epub_path = metadata_path.with_suffix(".epub")
                metadata_epubs.add(epub_path)
                try:
                    if (
                        not self._is_regular_file(metadata_path)
                        or not self._is_regular_file(epub_path)
                        or metadata_path.stat().st_size > MAX_METADATA_BYTES
                    ):
                        raise ValueError
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    original = metadata.get("original_bytes")
                    output = metadata.get("output_bytes")
                    digest = metadata.get("output_sha256")
                    if (
                        metadata.get("schema") != CACHE_SCHEMA_VERSION
                        or metadata.get("profile") != profile
                        or type(original) is not int
                        or original <= 0
                        or type(output) is not int
                        or output <= 0
                        or not isinstance(digest, str)
                        or not _CACHE_KEY_PATTERN.fullmatch(digest)
                        or epub_path.stat().st_size != output
                        or (verify_checksums and sha256_file(epub_path) != digest)
                    ):
                        raise ValueError
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    invalid_entries += 1
                    continue
                entries += 1
                original_bytes += original
                output_bytes += output

            epub_paths = (
                list(profile_root.rglob("*.epub")) if profile_root.is_dir() else []
            )
            invalid_entries += sum(
                1
                for epub_path in epub_paths
                if epub_path not in metadata_epubs
                or not self._is_regular_file(epub_path)
            )
            report[profile] = CacheProfileStats(
                entries=entries,
                original_bytes=original_bytes,
                output_bytes=output_bytes,
                invalid_entries=invalid_entries,
            )
        return report

    def purge(self, scope: str) -> dict[str, CacheProfileStats]:
        """Remove one or both derivative profiles beneath the owned cache root."""

        if scope not in {*_PROFILES, "all"}:
            raise ValueError("cache purge scope must be x3, x4, or all")
        before = self.inspect()
        profiles = sorted(_PROFILES) if scope == "all" else [scope]
        for profile in profiles:
            target = self.root / profile
            if target.is_symlink():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        return {profile: before[profile] for profile in profiles}


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "CacheError",
    "CacheHit",
    "CacheLookup",
    "CacheProfileStats",
    "DerivativeCache",
    "build_cache_key",
    "content_source_version",
    "http_source_version",
    "sha256_file",
]
