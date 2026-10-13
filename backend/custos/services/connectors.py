"""Operational connectors — the *Collect* stage of the CUSTOS loop.

Every connector implements the same contract so that the orchestrator, the API
and the tests treat a rainfall gauge, a SCADA feed and a phone call identically:

* ``name`` / ``kind``      — identity and provenance,
* ``fetch()``              — async, returns normalised readings (never raw vendor junk),
* ``degraded``             — the connector's own health, reported to the operator,
* ``last_success``         — used to age the data-freshness badge in the UI.

A connector that fails **never** raises into the pipeline: raw data is
best-effort and the platform must keep running on the last good state. Failures
are counted, surfaced as freshness/quality penalties, and published as events.

Three connectors ship with the MVP:

* :class:`SyntheticSensorConnector` — the deterministic Kochi replay used for
  demonstrations, drills and offline training. It is *not* a fake dependency: it
  drives the same normalisation, fusion and engine paths as live hardware.
* :class:`HttpFeedConnector` — a generic, credentialed REST/JSON ingest for a
  weather service or a utility SCADA gateway.
* :class:`ManualReportConnector` — operator-entered observations (a control room
  in a blackout still has a telephone).
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..config import settings
from ..fusion.normalize import (
    canonical_metric,
    convert_value,
    normalize_coordinates,
    parse_timestamp,
    quality_score,
)
from ..observability import get_logger, redact

log = get_logger("services.connectors")


@dataclass(slots=True)
class Reading:
    """One normalised observation, ready for the fusion layer."""

    sensor_id: str
    metric: str
    value: float
    unit: str
    at: datetime
    asset_id: str | None = None
    quality: float = 1.0
    source: str = "unknown"
    raw: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        """Row for ``sensor_readings``, including the idempotency key.

        The dedupe key is computed here rather than in the repository so that a
        store-and-forward replay of the same batch is provably a no-op at the
        door: the retry cannot double-count a flood.
        """
        from ..fusion.normalize import dedupe_key

        return {
            "sensor_id": self.sensor_id,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "observed_at": self.at,
            "quality": self.quality,
            "source": self.source,
            "dedupe_key": dedupe_key(self.sensor_id, self.metric, self.at, self.value),
            "raw": dict(self.raw or {}),
        }


def _pick(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """Field access that works for any Mapping, not just a dict."""
    try:
        value = row.get(key, default)
    except AttributeError:  # pragma: no cover - exotic mapping
        return default
    return default if value is None else value


def _score(*, metric: str, unit: str, value: float, sensor: Any, at: datetime) -> float:
    """Quality score a fused reading carries all the way to the console."""
    expected_min = getattr(sensor, "expected_min", None)
    expected_max = getattr(sensor, "expected_max", None)
    in_range = True
    if expected_min is not None and value < expected_min:
        in_range = False
    if expected_max is not None and value > expected_max:
        in_range = False
    age = max(0.0, (datetime.now(timezone.utc) - at.astimezone(timezone.utc)).total_seconds())
    return quality_score(
        timestamp_ok=True,
        unit_known=bool(unit) or metric in {"rainfall_mm_h", "water_level", "flood_depth", "discharge_m3s"},
        in_range=in_range,
        is_duplicate=False,
        provider_quality=1.0,
        staleness_s=age,
    )


class ConnectorError(RuntimeError):
    """Raised inside a connector; always caught by :meth:`BaseConnector.safe_fetch`."""


@dataclass
class ConnectorHealth:
    name: str
    kind: str
    ok: bool = True
    consecutive_failures: int = 0
    last_success: datetime | None = None
    last_error: str = ""
    fetched: int = 0

    @property
    def stale_seconds(self) -> float:
        if self.last_success is None:
            return float("inf")
        return (datetime.now(timezone.utc) - self.last_success).total_seconds()


class BaseConnector:
    name: str = "connector"
    kind: str = "generic"
    #: after this many consecutive failures the connector is reported unhealthy
    failure_threshold: int = 3

    def __init__(self) -> None:
        self.health = ConnectorHealth(name=self.name, kind=self.kind)

    async def fetch(self) -> Sequence[Reading]:  # pragma: no cover - interface
        raise NotImplementedError

    async def safe_fetch(self) -> list[Reading]:
        """Never raises. Records health, so the UI can show *why* data is stale."""
        try:
            readings = list(await asyncio.wait_for(self.fetch(), timeout=20.0))
        except Exception as exc:  # noqa: BLE001 - a connector is untrusted by design
            self.health.ok = False
            self.health.consecutive_failures += 1
            self.health.last_error = f"{type(exc).__name__}: {exc}"[:240]
            log.warning(
                "connector fetch failed",
                extra={"custos": {"connector": self.name, "failures": self.health.consecutive_failures, "error": self.health.last_error}},
            )
            return []
        self.health.ok = self.health.consecutive_failures < self.failure_threshold
        self.health.consecutive_failures = 0
        self.health.last_error = ""
        self.health.last_success = datetime.now(timezone.utc)
        self.health.fetched += len(readings)
        return readings


# --------------------------------------------------------------------------- #
# synthetic city replay
# --------------------------------------------------------------------------- #
class SyntheticSensorConnector(BaseConnector):
    """Deterministic sensor replay over the Kochi twin.

    The replay is driven by the *same* hydrology the forecast uses, so a drill
    exercises the whole path: sensor → normalisation → fusion → anomaly
    detection → simulation → recommendation. Values carry measurement noise and
    a configurable dropout rate, because a fusion layer that has never seen a
    gap will discover one during the flood.
    """

    name = "kochi-telemetry-replay"
    kind = "sensor_stream"

    def __init__(
        self,
        snapshot: Any,
        *,
        scenario_rain_mm_h: float = 0.0,
        dropout_rate: float = 0.03,
        noise_sigma: float = 0.05,
        seed: int | None = None,
        clock: Any = None,
    ) -> None:
        super().__init__()
        self._snapshot = snapshot
        self.scenario_rain_mm_h = scenario_rain_mm_h
        self.dropout_rate = dropout_rate
        self.noise_sigma = noise_sigma
        self._rng = random.Random(seed if seed is not None else 20261013)
        self._clock = clock

    def set_rain(self, mm_h: float) -> None:
        self.scenario_rain_mm_h = max(0.0, float(mm_h))

    async def fetch(self) -> Sequence[Reading]:
        now = self._clock() if self._clock else datetime.now(timezone.utc)
        readings: list[Reading] = []
        storm = self.scenario_rain_mm_h
        for sensor in self._snapshot.sensors.values():
            if self._rng.random() < self.dropout_rate:
                continue  # a dropped sample is normal, not an incident
            metric = canonical_metric(sensor.metric)
            base = float(sensor.last_value or 0.0)
            if metric in {"rainfall_mm_h", "rainfall"}:  # canonical names only
                value = max(0.0, storm + self._rng.gauss(0.0, max(0.4, storm * 0.08)))
                unit = "mm/h"
            elif metric in {"water_level_m", "river_level_m", "stage_m", "reservoir_level_m"}:
                # level sensors track the rainfall signal with a lag: the tide and
                # the canals respond minutes-to-hours after the cell passes
                lag_signal = storm / max(1.0, 60.0)
                value = base + lag_signal * self._rng.uniform(0.6, 1.4)
                unit = "m"
            elif metric in {"flood_depth_m", "depth_m", "street_depth_m"}:
                value = base + (storm / 120.0) * self._rng.uniform(0.5, 1.6)
                unit = "m"
            elif metric in {"pump_rate_m3s", "discharge_m3s", "flow_m3s"}:
                value = max(0.0, base * self._rng.uniform(0.85, 1.15))
                unit = "m3/s"
            elif metric in {"power_kw", "grid_frequency_hz", "feeder_load_pct"}:
                value = 50.0 + self._rng.gauss(0.0, 0.12)
                unit = "Hz"
            else:
                value = base * self._rng.uniform(0.95, 1.05)
                unit = sensor.unit or ""
            value = value * (1.0 + self._rng.gauss(0.0, self.noise_sigma) * 0.25)
            readings.append(
                Reading(
                    sensor_id=sensor.id,
                    metric=metric,
                    value=round(value, 4),
                    unit=unit,
                    at=now,
                    asset_id=sensor.asset_id,
                    quality=_score(metric=metric, unit=unit, value=value, sensor=sensor, at=now),
                    source=self.name,
                )
            )
        return readings


# --------------------------------------------------------------------------- #
# live HTTP ingest
# --------------------------------------------------------------------------- #
class HttpFeedConnector(BaseConnector):
    """Generic JSON pull from an external system (weather, SCADA, utility).

    Field mapping is declarative so integrating a new vendor is configuration,
    not code: ``{"sensor": "station_id", "value": "rain_mm", "at": "ts"}``.
    Requests carry an optional bearer token, are bounded by a timeout, and are
    never retried in a tight loop — the poller's cadence is the retry policy.
    """

    name = "http-feed"
    kind = "http"

    def __init__(
        self,
        url: str,
        *,
        mapping: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        unit_scale: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.url = url
        self.mapping = dict(mapping or {"sensor_id": "sensor_id", "metric": "metric", "value": "value", "unit": "unit", "at": "at"})
        self.params = dict(params or {})
        self.headers = {k: v for k, v in (headers or {}).items()}
        self.unit_scale = dict(unit_scale or {})

    async def fetch(self) -> Sequence[Reading]:
        import httpx  # imported lazily: the connector is optional at runtime

        async with httpx.AsyncClient(timeout=15.0, headers=self.headers) as client:
            response = await client.get(self.url, params=self.params)
            response.raise_for_status()
            payload = response.json()
        rows = payload if isinstance(payload, list) else payload.get("readings") or payload.get("data") or []
        if not isinstance(rows, list):
            raise ConnectorError("feed returned no reading list")
        readings: list[Reading] = []
        for row in rows[:5000]:
            if not isinstance(row, Mapping):
                continue
            try:
                metric = canonical_metric(dict.get(row, self.mapping["metric"], ""))
                unit = str(dict.get(row, self.mapping["unit"], "") or "")
                value = convert_value(float(dict.get(row, self.mapping["value"], 0.0)), unit, metric)
                at = parse_timestamp(dict.get(row, self.mapping["at"]))
            except Exception as exc:  # noqa: BLE001 - one bad row must not drop the batch
                log.debug("skipping malformed feed row", extra={"custos": {"error": str(exc)[:120]}})
                continue
            readings.append(
                Reading(
                    sensor_id=str(_pick(row, self.mapping["sensor_id"], "")),
                    metric=metric,
                    value=value,
                    unit=unit,
                    at=at,
                    quality=_score(metric=metric, unit=unit, value=value, sensor=None, at=at),
                    source=self.name,
                )
            )
        return readings


class ManualReportConnector(BaseConnector):
    """Operator-entered observations from the control room.

    Kept in-process and bounded; the API pushes into it. Manual entries are
    tagged with the operator's identity and carry a lower quality score than
    instrumented telemetry, which the fusion layer uses when two sources
    disagree about the same asset.
    """

    name = "manual-observation"
    kind = "manual"

    def __init__(self, max_pending: int = 500) -> None:
        super().__init__()
        self._pending: list[Reading] = []
        self._max = max_pending

    def submit(self, reading: Reading) -> None:
        if len(self._pending) >= self._max:
            self._pending.pop(0)  # bounded: a control room never blocks on a queue
        self._pending.append(reading)

    async def fetch(self) -> Sequence[Reading]:
        pending, self._pending = self._pending, []
        return pending


class ConnectorRegistry:
    """The set of connectors the *Collect* stage polls, and their health."""

    def __init__(self, connectors: Iterable[BaseConnector] = ()) -> None:
        self._connectors: dict[str, BaseConnector] = {c.name: c for c in connectors}
        self.last_poll: dict[str, float] = {}

    def register(self, connector: BaseConnector) -> None:
        self._connectors[connector.name] = connector

    def get(self, name: str) -> BaseConnector | None:
        return self._connectors.get(name)

    def __iter__(self):
        return iter(self._connectors.values())

    def health(self) -> list[dict[str, Any]]:
        now = time.time()
        out: list[dict[str, Any]] = []
        for connector in self._connectors.values():
            out.append(
                {
                    "name": connector.name,
                    "kind": connector.kind,
                    "ok": connector.health.ok,
                    "consecutive_failures": connector.health.consecutive_failures,
                    "last_success": connector.health.last_success.isoformat() if connector.health.last_success else None,
                    "stale_seconds": None if connector.health.last_success is None else round(connector.health.stale_seconds, 1),
                    "readings_total": connector.health.fetched,
                    "error": redact(connector.health.last_error),
                    "poll_age_s": round(now - self.last_poll[connector.name], 1) if connector.name in self.last_poll else None,
                }
            )
        return out

    async def poll_all(self) -> list[Reading]:
        """Poll every connector concurrently. One slow vendor must not block the city."""
        names = list(self._connectors)
        if not names:
            return []
        results = await asyncio.gather(*(self._connectors[name].safe_fetch() for name in names), return_exceptions=True)
        readings: list[Reading] = []
        for name, result in zip(names, results):
            if isinstance(result, BaseException):
                continue
            self.last_poll[name] = time.time()
            readings.extend(result)
        return readings


def build_default_registry(snapshot: Any, *, scenario_rain_mm_h: float = 0.0, clock: Any = None) -> ConnectorRegistry:
    """Connectors enabled for this deployment, honouring operator configuration."""
    registry = ConnectorRegistry([SyntheticSensorConnector(snapshot, scenario_rain_mm_h=scenario_rain_mm_h, clock=clock), ManualReportConnector()])
    if settings.enable_live_connectors and settings.weather_api_url:
        registry.register(
            HttpFeedConnector(
                settings.weather_api_url,
                mapping={"sensor_id": "station_id", "metric": "metric", "value": "value", "unit": "unit", "at": "observed_at"},
            )
        )
    if settings.enable_live_connectors and settings.sensor_stream_url:
        registry.register(HttpFeedConnector(settings.sensor_stream_url, mapping={"sensor_id": "sensor_id", "metric": "metric", "value": "value", "unit": "unit", "at": "at"}))
    if settings.enable_live_connectors and settings.utility_api_url:
        registry.register(
            HttpFeedConnector(
                settings.utility_api_url,
                mapping={"sensor_id": "feeder_id", "metric": "metric", "value": "value", "unit": "unit", "at": "ts"},
            )
        )
    return registry
