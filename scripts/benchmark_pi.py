#!/usr/bin/env python3
"""Benchmark optimizer and cache behavior on sanitized Raspberry Pi copies."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from crosspoint_cwa_bridge import __version__
from crosspoint_cwa_bridge.app import create_app
from crosspoint_cwa_bridge.config import Settings
from crosspoint_cwa_bridge.optimizer import OPTIMIZER_VERSION, optimize_epub


AUTHORIZATION = "Basic " + base64.b64encode(b"benchmark-user:benchmark-pass").decode(
    "ascii"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def peak_rss_mib() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return value / (1024 * 1024)
    return value / 1024


def run_worker(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    result = optimize_epub(args.source, args.output, profile=args.profile)
    elapsed = time.perf_counter() - started
    report = {
        "profile": args.profile,
        "source_bytes": result.source_bytes,
        "output_bytes": result.output_bytes,
        "savings_percent": round(result.savings_percent, 1),
        "optimizer_seconds": round(elapsed, 3),
        "peak_rss_mib": round(peak_rss_mib(), 1),
        "image_count": result.image_count,
        "repair_count": result.repair_count,
    }
    json.dump(report, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


def worker_benchmark(
    script: Path,
    source: Path,
    output: Path,
    profile: str,
    timeout_seconds: int,
) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--worker",
            "--source",
            str(source),
            "--output",
            str(output),
            "--profile",
            profile,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    return json.loads(completed.stdout)


async def stream_epub(request: web.Request) -> web.StreamResponse:
    files: dict[str, Path] = request.app["files"]
    digests: dict[str, str] = request.app["digests"]
    if request.headers.get("Authorization") != AUTHORIZATION:
        return web.Response(
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Benchmark"'},
        )
    label = request.match_info["label"]
    source = files.get(label)
    if source is None:
        raise web.HTTPNotFound()

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "application/epub+zip",
            "Content-Length": str(source.stat().st_size),
            "Content-Disposition": f'attachment; filename="{label}.epub"',
            "ETag": f'"{digests[label]}"',
        },
    )
    await response.prepare(request)
    try:
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                await response.write(chunk)
    except ConnectionResetError:
        return response
    try:
        await response.write_eof()
    except ConnectionResetError:
        pass
    return response


async def timed_download(
    client: TestClient, path: str, timeout_seconds: int
) -> dict[str, float | int]:
    async def perform() -> dict[str, float | int]:
        started = time.perf_counter()
        response = await client.get(path, headers={"Authorization": AUTHORIZATION})
        if response.status != 200:
            raise RuntimeError(f"benchmark request returned HTTP {response.status}")
        first = await response.content.read(1)
        first_byte_seconds = time.perf_counter() - started
        total_bytes = len(first)
        async for chunk in response.content.iter_chunked(64 * 1024):
            total_bytes += len(chunk)
        total_seconds = time.perf_counter() - started
        return {
            "first_byte_seconds": round(first_byte_seconds, 3),
            "total_seconds": round(total_seconds, 3),
            "response_bytes": total_bytes,
        }

    return await asyncio.wait_for(perform(), timeout=timeout_seconds)


async def http_benchmarks(
    files: dict[str, Path], work_root: Path, timeout_seconds: int
) -> dict[tuple[str, str], dict]:
    upstream_app = web.Application()
    upstream_app["files"] = files
    upstream_app["digests"] = {
        label: sha256_file(path) for label, path in files.items()
    }
    upstream_app.router.add_get("/opds/download/{label}/epub/", stream_epub)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()

    settings = Settings(
        upstream_url=URL(str(upstream_server.make_url("/"))),
        cache_dir=work_root / "cache",
        work_dir=work_root / "work",
    )
    bridge_client = TestClient(TestServer(create_app(settings)))
    await bridge_client.start_server()
    results: dict[tuple[str, str], dict] = {}
    try:
        for label in files:
            for profile in ("x3", "x4"):
                path = f"/opds/crosspoint/{profile}/download/{label}/epub/"
                first = await timed_download(bridge_client, path, timeout_seconds)
                hit = await timed_download(bridge_client, path, timeout_seconds)
                if first["response_bytes"] != hit["response_bytes"]:
                    raise RuntimeError("cache hit byte count differs from cache miss")
                results[(label, profile)] = {
                    "first_response_first_byte_seconds": first["first_byte_seconds"],
                    "first_response_total_seconds": first["total_seconds"],
                    "cache_hit_first_byte_seconds": hit["first_byte_seconds"],
                    "cache_hit_total_seconds": hit["total_seconds"],
                    "http_output_bytes": first["response_bytes"],
                }
    finally:
        await bridge_client.close()
        await upstream_server.close()
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", choices=("x3", "x4"))
    parser.add_argument("--normal", type=Path)
    parser.add_argument("--image-heavy", type=Path)
    parser.add_argument("--work", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.worker:
        if args.source is None or args.output is None or args.profile is None:
            raise ValueError("worker mode requires source, output, and profile")
        return run_worker(args)
    if args.normal is None or args.image_heavy is None or args.work is None:
        raise ValueError("benchmark mode requires both inputs and --work")
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")

    args.work.mkdir(parents=True, exist_ok=True)
    files = {"normal": args.normal, "image-heavy": args.image_heavy}
    script = Path(__file__).resolve()
    report = {
        "bridge_version": __version__,
        "optimizer_version": OPTIMIZER_VERSION,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "cases": [],
    }

    with tempfile.TemporaryDirectory(prefix="run-", dir=args.work) as temp_name:
        temp_root = Path(temp_name)
        direct: dict[tuple[str, str], dict] = {}
        for label, source in files.items():
            source_id = sha256_file(source)[:12]
            for profile in ("x3", "x4"):
                output = temp_root / f"{label}-{profile}.epub"
                metrics = worker_benchmark(
                    script,
                    source,
                    output,
                    profile,
                    args.timeout_seconds,
                )
                metrics["source_id"] = source_id
                direct[(label, profile)] = metrics

        http = asyncio.run(
            http_benchmarks(files, temp_root / "http", args.timeout_seconds)
        )
        for label in files:
            for profile in ("x3", "x4"):
                metrics = direct[(label, profile)] | http[(label, profile)]
                if metrics["output_bytes"] != metrics["http_output_bytes"]:
                    raise RuntimeError(
                        "direct and HTTP optimizer output sizes do not match"
                    )
                report["cases"].append({"label": label, **metrics})

    json.dump(report, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
