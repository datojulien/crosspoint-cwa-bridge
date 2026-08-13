"""Small structured logger with no request-header logging."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        for key in (
            "profile",
            "upstream_route",
            "status",
            "duration_ms",
            "links_rewritten",
            "auth_present",
            "original_bytes",
            "output_bytes",
            "savings_percent",
            "image_count",
            "repair_count",
            "fallback_reason",
            "cache_reason",
        ):
            value = getattr(record, key, None)
            if value is not None:
                event[key] = value
        return json.dumps(event, separators=(",", ":"), ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
