#!/usr/bin/env python3
"""Select sanitized benchmark copies without exposing private library paths."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sys
from zipfile import BadZipFile, ZipFile


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
TEXT_EXTENSIONS = {".xhtml", ".html", ".htm", ".css", ".ncx", ".opf", ".txt"}
MIMETYPE = b"application/epub+zip"


@dataclass(frozen=True, slots=True)
class Candidate:
    path: Path
    source_bytes: int
    image_count: int
    image_bytes: int
    text_bytes: int
    archive_bytes: int

    @property
    def image_ratio(self) -> float:
        if not self.archive_bytes:
            return 0.0
        return self.image_bytes / self.archive_bytes


def inspect_candidate(path: Path, maximum_bytes: int) -> Candidate | None:
    try:
        source_bytes = path.stat().st_size
        if source_bytes < 16 * 1024 or source_bytes > maximum_bytes:
            return None
        with ZipFile(path, "r") as archive:
            names = {info.filename: info for info in archive.infolist()}
            mimetype = names.get("mimetype")
            if mimetype is None or archive.read(mimetype) != MIMETYPE:
                return None
            image_count = 0
            image_bytes = 0
            text_bytes = 0
            archive_bytes = 0
            for info in names.values():
                if info.is_dir() or info.file_size < 0:
                    continue
                archive_bytes += info.file_size
                suffix = Path(info.filename).suffix.lower()
                if suffix in IMAGE_EXTENSIONS:
                    image_count += 1
                    image_bytes += info.file_size
                elif suffix in TEXT_EXTENSIONS:
                    text_bytes += info.file_size
    except (BadZipFile, OSError, RuntimeError):
        return None
    return Candidate(
        path=path,
        source_bytes=source_bytes,
        image_count=image_count,
        image_bytes=image_bytes,
        text_bytes=text_bytes,
        archive_bytes=archive_bytes,
    )


def choose_candidates(candidates: list[Candidate]) -> tuple[Candidate, Candidate]:
    normal_pool = [
        item
        for item in candidates
        if item.text_bytes >= 50 * 1024
        and item.image_ratio <= 0.35
        and item.source_bytes <= 25 * 1024 * 1024
    ]
    if not normal_pool:
        normal_pool = sorted(candidates, key=lambda item: item.image_ratio)[:20]
    if not normal_pool:
        raise RuntimeError("no valid text-oriented EPUB candidate was found")

    target_size = 2 * 1024 * 1024
    normal = min(
        normal_pool,
        key=lambda item: (
            abs(math.log2(max(item.source_bytes, 1) / target_size)),
            item.image_ratio,
            -item.text_bytes,
        ),
    )

    heavy_pool = [
        item
        for item in candidates
        if item.path != normal.path
        and item.image_count >= 5
        and item.image_ratio >= 0.50
    ]
    if not heavy_pool:
        raise RuntimeError("no reasonably image-heavy EPUB candidate was found")
    heavy = max(
        heavy_pool,
        key=lambda item: (item.image_bytes, item.image_count, item.source_bytes),
    )
    return normal, heavy


def copy_sanitized(candidate: Candidate, destination: Path, label: str) -> dict:
    digest = hashlib.sha256()
    with candidate.path.open("rb") as source, destination.open("xb") as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            target.write(chunk)
    destination.chmod(0o600)
    return {
        "label": label,
        "source_id": digest.hexdigest()[:12],
        "source_bytes": candidate.source_bytes,
        "image_count": candidate.image_count,
        "image_uncompressed_bytes": candidate.image_bytes,
        "text_uncompressed_bytes": candidate.text_bytes,
        "image_ratio_percent": round(candidate.image_ratio * 100, 1),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-source-mib", type=int, default=256)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_source_mib <= 0:
        raise ValueError("--max-source-mib must be positive")
    args.output.mkdir(parents=True, exist_ok=True)
    maximum_bytes = args.max_source_mib * 1024 * 1024

    candidates: list[Candidate] = []
    for path in args.library.rglob("*"):
        if not path.is_symlink() and path.is_file() and path.suffix.lower() == ".epub":
            candidate = inspect_candidate(path, maximum_bytes)
            if candidate is not None:
                candidates.append(candidate)

    normal, heavy = choose_candidates(candidates)
    report = {
        "candidate_count": len(candidates),
        "copies": [
            copy_sanitized(normal, args.output / "normal.epub", "normal"),
            copy_sanitized(heavy, args.output / "image-heavy.epub", "image-heavy"),
        ],
    }
    json.dump(report, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
