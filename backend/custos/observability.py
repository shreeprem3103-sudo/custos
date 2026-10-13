"""Structured logging, request correlation and in-process metrics.

Every log line is JSON so that it can be shipped to an on-premise SIEM without a
parser, and every line carries the correlation id of the originating request so
that a commander's action can be replayed end-to-end during a post-incident
review. Credentials are never logged: :func:`redact` is applied to structured
payloads before they are emitted.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Iterable, Mapping

from .config import settings

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("custos_correlation_id", default="-")

_SENSITIVE_KEYS = (
    "password",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "x-api-key",
    "jwt",
    "refresh_token",
    "access_token",
)


def redact(value: Any) -> Any:
    """Best-effort structural redaction of secrets before they hit a log sink."""
    if isinstance(value, Mapping):
        return {
            str(k): ("***" if any(s in str(k).lower() for s in _SENSITIVE_KEYS) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact(v) for v in value]
    if isinstance(value, str) and len(value) > 512:
        return value[:512] + "…"
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: D102
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": correlation_id.get(),
            "env": settings.environment,
        }
        extra = getattr(record, "custos", None)
        if extra:
            payload.update(redact(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: str | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel((level or settings.log_level).upper())
    for noisy in ("uvicorn.access", "httpx", "httpcore", "sqlalchemy.engine.Engine"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, logging.getLogger(noisy).level))


def set_correlation_id(value: str | None = None) -> str:
    """Bind a correlation id to the current task/request context.

    Generated when omitted so that a code path which forgot to propagate an id
    still produces *traceable* lines rather than a row of dashes.
    """
    import uuid

    cid = value or uuid.uuid4().hex[:16]
    correlation_id.set(cid)
    return cid


def current_correlation_id() -> str:
    return correlation_id.get()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"custos.{name}")


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"custos": fields})


# --------------------------------------------------------------------------- #
# metrics: a bounded, dependency-free registry. A Prometheus exporter can be
# attached later; what matters during an incident is that the counters exist.
# --------------------------------------------------------------------------- #


@dataclass
class Metrics:
    counters: dict[str, float] = field(default_factory=dict)
    histograms: dict[str, Deque[float]] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)

    def inc(self, name: str, value: float = 1.0, **labels: Any) -> None:
        self.counters[self._key(name, labels)] = self.counters.get(self._key(name, labels), 0.0) + value

    def observe(self, name: str, value: float, **labels: Any) -> None:
        key = self._key(name, labels)
        bucket = self.histograms.get(key)
        if bucket is None:
            bucket = self.histograms[key] = deque(maxlen=2048)
        bucket.append(value)

    @staticmethod
    def _key(name: str, labels: Mapping[str, Any]) -> str:
        if not labels:
            return name
        suffix = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{suffix}}}"

    def summary(self) -> dict[str, Any]:
        import statistics

        out: dict[str, Any] = {"uptime_s": round(time.time() - self.started_at, 1), "counters": dict(self.counters)}
        for key, values in self.histograms.items():
            if not values:
                continue
            ordered = sorted(values)
            out.setdefault("latency", {})[key] = {
                "n": len(ordered),
                "p50_ms": round(statistics.median(ordered) * 1000, 2),
                "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1] * 1000, 2),
                "max_ms": round(ordered[-1] * 1000, 2),
            }
        return out

    def snapshot_keys(self) -> Iterable[str]:
        return tuple(self.counters) + tuple(self.histograms)


METRICS = Metrics()


class Timer:
    """Context manager recording a duration into :data:`METRICS`."""

    def __init__(self, name: str, **labels: Any) -> None:
        self.name = name
        self.labels = labels
        self.t0 = 0.0

    def __enter__(self) -> "Timer":
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        METRICS.observe(self.name, time.perf_counter() - self.t0, **self.labels)
