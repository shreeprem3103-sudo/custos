"""Twin repository: database <-> snapshot translation, plus the degradation cache.

The cache is the platform's resilience hinge. Engines call
:func:`get_snapshot`; if storage is healthy the snapshot is rebuilt from
PostgreSQL, and if storage fails the *last good* snapshot is served, marked
``degraded=True``, so that the command console keeps rendering a crisis picture
with an explicit staleness badge instead of going dark.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import Select, and_, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..domain.enums import AssetStatus, AssetType, EventKind, FailureMode, RelationKind
from ..domain.twin import AssetState, Edge, SensorState, TwinSnapshot, ZoneState
from ..infrastructure import db
from ..observability import METRICS, get_logger
from ..infrastructure.models import (
    AIInvocation,
    Anomaly,
    Asset,
    AuditEntry,
    CascadeEdge,
    ConnectorRun,
    EventLog,
    FacilityDetail,
    Incident,
    IncidentAsset,
    IncidentEvent,
    Relationship,
    Resource,
    RoadDetail,
    Sensor,
    SensorReading,
    ShelterDetail,
    Simulation,
    SimulationEvent,
    SystemState,
    Task,
    utcnow,
)

log = get_logger("fusion.repository")


# --------------------------------------------------------------------------- #
# snapshot construction
# --------------------------------------------------------------------------- #
def _coerce_enum(value: Any, enum_cls: type, default: Any) -> Any:
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return default


async def _load_asset_states(session: AsyncSession) -> dict[str, AssetState]:
    rows = (await session.execute(select(Asset))).scalars().all()
    details: dict[str, dict[str, Any]] = {}

    for model, keys in (
        (FacilityDetail, None),
        (ShelterDetail, None),
        (RoadDetail, None),
    ):
        for detail in (await session.execute(select(model))).scalars().all():
            bucket = details.setdefault(detail.asset_id, {})
            for column in detail.__table__.columns.keys():
                if column == "asset_id":
                    continue
                bucket[column] = getattr(detail, column)

    nodes: dict[str, AssetState] = {}
    for row in rows:
        capacity = dict(row.capacity or {})
        capacity.update({k: v for k, v in details.get(row.id, {}).items() if k not in capacity})
        nodes[row.id] = AssetState(
            id=row.id,
            name=row.name,
            asset_type=_coerce_enum(row.asset_type, AssetType, AssetType.COMMUNITY_HALL),
            criticality=float(row.criticality or 0.0),
            lon=float(row.lon or 0.0),
            lat=float(row.lat or 0.0),
            elevation_m=row.elevation_m,
            status=_coerce_enum(row.status, AssetStatus, AssetStatus.UNKNOWN),
            flood_depth_m=float(row.flood_depth_m or 0.0),
            power_available=bool(row.power_available),
            access_available=bool(row.access_available),
            service_level=float(row.service_level if row.service_level is not None else 1.0),
            exposure_score=float(row.exposure_score or 0.0),
            design_rainfall=float((row.attributes or {}).get("design_rainfall_mm_h") or 0.0) or None,
            zone=row.zone,
            operator=row.operator,
            subtype=row.subtype,
            geometry=dict(row.geometry or {}),
            capacity=capacity,
            attributes=dict(row.attributes or {}),
            state_observed_at=row.state_observed_at,
            state_source=row.state_source,
            state_confidence=float(row.state_confidence if row.state_confidence is not None else 1.0),
        )
    return nodes


async def _load_edges(session: AsyncSession) -> list[Edge]:
    rows = (await session.execute(select(Relationship).where(Relationship.active.is_(True)))).scalars().all()
    return [
        Edge(
            id=row.id,
            source_id=row.source_id,
            target_id=row.target_id,
            kind=_coerce_enum(row.kind, RelationKind, RelationKind.DEPENDS_ON),
            criticality=float(row.criticality or 0.5),
            lead_time_min=int(row.lead_time_min or 0),
            attenuation=float(row.attenuation if row.attenuation is not None else 1.0),
            failure_mode=_coerce_enum(row.failure_mode, FailureMode, FailureMode.IMMEDIATE),
            recovery_hours=float(row.recovery_hours or 6.0),
            redundancy_group=row.redundancy_group,
            attributes=dict(row.attributes or {}),
        )
        for row in rows
    ]


async def _load_zones(session: AsyncSession) -> dict[str, ZoneState]:
    from ..infrastructure.models import Zone

    rows = (await session.execute(select(Zone))).scalars().all()
    return {
        row.id: ZoneState(
            id=row.id,
            name=row.name,
            lon=float(row.lon or 0.0),
            lat=float(row.lat or 0.0),
            population=int(row.population or 0),
            vulnerability_index=float(row.vulnerability_index or 0.3),
            children_pct=float(row.children_pct or 0.0),
            elderly_pct=float(row.elderly_pct or 0.0),
            population_density=float(row.population_density or 0.0),
            mean_elevation_m=float(row.mean_elevation_m or 0.0),
        )
        for row in rows
    }


async def _load_sensors(session: AsyncSession) -> dict[str, SensorState]:
    sensors = (await session.execute(select(Sensor).where(Sensor.is_active.is_(True)))).scalars().all()
    if not sensors:
        return {}
    latest_time = (
        select(SensorReading.sensor_id, func.max(SensorReading.observed_at).label("observed_at"))
        .group_by(SensorReading.sensor_id)
        .subquery()
    )
    stmt: Select[Any] = select(SensorReading).join(
        latest_time,
        and_(
            SensorReading.sensor_id == latest_time.c.sensor_id,
            SensorReading.observed_at == latest_time.c.observed_at,
        ),
    )
    latest: dict[str, SensorReading] = {}
    for reading in (await session.execute(stmt)).scalars().all():
        latest.setdefault(reading.sensor_id, reading)
    return {
        row.id: SensorState(
            id=row.id,
            name=row.name,
            metric=row.metric,
            unit=row.unit or "",
            lon=float(row.lon or 0.0),
            lat=float(row.lat or 0.0),
            asset_id=row.asset_id,
            zone=row.zone,
            baseline_median=float(row.baseline_median or 0.0),
            baseline_mad=float(row.baseline_mad or 0.0),
            expected_min=row.expected_min,
            expected_max=row.expected_max,
            last_value=latest[row.id].value if row.id in latest else None,
            last_observed_at=latest[row.id].observed_at if row.id in latest else None,
            is_active=bool(row.is_active),
        )
        for row in sensors
    }


async def build_snapshot(session: AsyncSession, *, degraded: bool = False, sources: Iterable[str] = ()) -> TwinSnapshot:
    nodes = await _load_asset_states(session)
    edges = await _load_edges(session)
    zones = await _load_zones(session)
    sensors = await _load_sensors(session)
    return TwinSnapshot(
        nodes=nodes,
        edges=edges,
        zones=zones,
        sensors=sensors,
        city_id=settings.city_id,
        city_name=settings.city_name,
        degraded=degraded,
        sources=sources,
    )


# --------------------------------------------------------------------------- #
# degradation-aware cache
# --------------------------------------------------------------------------- #
class TwinCache:
    """Last-known-good snapshot with a short TTL and explicit staleness."""

    def __init__(self, ttl_s: float = 1.5) -> None:
        self.ttl_s = ttl_s
        self._snapshot: TwinSnapshot | None = None
        self._loaded_at: float = 0.0
        self._lock = asyncio.Lock()
        self.builds = 0
        self.hits = 0
        self.stale_serves = 0

    @property
    def age_s(self) -> float | None:
        if self._loaded_at == 0.0:
            return None
        return round(time.time() - self._loaded_at, 3)

    def peek(self) -> TwinSnapshot | None:
        return self._snapshot

    def invalidate(self) -> None:
        self._loaded_at = 0.0

    async def get(self, *, force: bool = False, sources: Iterable[str] = ()) -> TwinSnapshot:
        async with self._lock:
            fresh = self._snapshot is not None and (time.time() - self._loaded_at) < self.ttl_s
            if fresh and not force:
                self.hits += 1
                return self._snapshot  # type: ignore[return-value]
            try:
                async with db.session_scope(commit=False) as session:
                    snapshot = await build_snapshot(session, sources=sources)
                self._snapshot = snapshot
                self._loaded_at = time.time()
                self.builds += 1
                METRICS.inc("twin.build")
                return snapshot
            except Exception as exc:  # noqa: BLE001
                if self._snapshot is None:
                    log.error("twin snapshot unavailable and no cache present", extra={"custos": {"error": str(exc)[:300]}})
                    raise
                self.stale_serves += 1
                METRICS.inc("twin.stale_serve")
                stale = self._snapshot.fork()
                stale.degraded = True
                stale.sources = tuple(set(stale.sources) | {"cache"})
                log.warning(
                    "serving cached twin snapshot",
                    extra={"custos": {"age_s": self.age_s, "error": str(exc)[:200]}},
                )
                return stale

    def stats(self) -> dict[str, Any]:
        return {
            "age_s": self.age_s,
            "builds": self.builds,
            "hits": self.hits,
            "stale_serves": self.stale_serves,
            "digest": self._snapshot.digest() if self._snapshot else None,
            "nodes": len(self._snapshot.nodes) if self._snapshot else 0,
        }


TWIN = TwinCache()


async def get_snapshot(*, force: bool = False) -> TwinSnapshot:
    return await TWIN.get(force=force)


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #
async def upsert_asset(session: AsyncSession, payload: Mapping[str, Any]) -> str:
    asset_id = str(payload["id"])
    existing = await session.get(Asset, asset_id)
    data = {
        "name": payload["name"],
        "asset_type": _enum_value(payload["asset_type"]),
        "subtype": payload.get("subtype"),
        "criticality": float(payload.get("criticality", 0.5)),
        "lon": float(payload.get("lon", 0.0)),
        "lat": float(payload.get("lat", 0.0)),
        "elevation_m": payload.get("elevation_m"),
        "geometry": payload.get("geometry") or {},
        "zone": payload.get("zone"),
        "operator": payload.get("operator"),
        "capacity": payload.get("capacity") or {},
        "attributes": payload.get("attributes") or {},
    }
    if existing is None:
        session.add(Asset(id=asset_id, status=AssetStatus.OK.value, **data))
        return "inserted"
    for key, value in data.items():
        setattr(existing, key, value)
    return "updated"


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


async def upsert_relationship(session: AsyncSession, payload: Mapping[str, Any]) -> str:
    stmt = select(Relationship).where(
        Relationship.source_id == payload["source_id"],
        Relationship.target_id == payload["target_id"],
        Relationship.kind == _enum_value(payload["kind"]),
    )
    existing = (await session.execute(stmt)).scalars().first()
    data = {
        "criticality": float(payload.get("criticality", 0.5)),
        "lead_time_min": int(payload.get("lead_time_min", 0)),
        "attenuation": float(payload.get("attenuation", 1.0)),
        "failure_mode": _enum_value(payload.get("failure_mode", FailureMode.IMMEDIATE)),
        "recovery_hours": float(payload.get("recovery_hours", 6.0)),
        "redundancy_group": payload.get("redundancy_group"),
        "attributes": payload.get("attributes") or {},
    }
    if existing is None:
        session.add(
            Relationship(
                source_id=str(payload["source_id"]),
                target_id=str(payload["target_id"]),
                kind=_enum_value(payload["kind"]),
                **data,
            )
        )
        return "inserted"
    for key, value in data.items():
        setattr(existing, key, value)
    return "updated"


async def insert_reading(session: AsyncSession, reading: Mapping[str, Any]) -> bool:
    """Insert a normalized reading. Returns False when it was a duplicate."""
    stmt = select(SensorReading.id).where(SensorReading.dedupe_key == reading["dedupe_key"]).limit(1)
    if (await session.execute(stmt)).scalar() is not None:
        return False
    session.add(SensorReading(**reading))
    return True


async def upsert_sensor(session: AsyncSession, sensor: Mapping[str, Any]) -> None:
    existing = await session.get(Sensor, sensor["id"])
    data = {k: v for k, v in sensor.items() if k != "id"}
    if existing is None:
        session.add(Sensor(id=str(sensor["id"]), **data))
    else:
        for key, value in data.items():
            setattr(existing, key, value)


async def touch_asset_state(
    session: AsyncSession,
    asset_id: str,
    *,
    status: AssetStatus | str | None = None,
    flood_depth_m: float | None = None,
    power_available: bool | None = None,
    access_available: bool | None = None,
    service_level: float | None = None,
    exposure_score: float | None = None,
    source: str,
    observed_at: datetime | None = None,
    confidence: float = 1.0,
) -> None:
    values: dict[str, Any] = {
        "state_source": source,
        "state_observed_at": observed_at or utcnow(),
        "state_confidence": confidence,
        "updated_at": utcnow(),
    }
    if status is not None:
        values["status"] = _enum_value(status)
    if flood_depth_m is not None:
        values["flood_depth_m"] = float(flood_depth_m)
    if power_available is not None:
        values["power_available"] = bool(power_available)
    if access_available is not None:
        values["access_available"] = bool(access_available)
    if service_level is not None:
        values["service_level"] = max(0.0, min(1.0, float(service_level)))
    if exposure_score is not None:
        values["exposure_score"] = max(0.0, min(1.0, float(exposure_score)))
    await session.execute(update(Asset).where(Asset.id == asset_id).values(**values))


async def record_event(session: AsyncSession, event: Mapping[str, Any]) -> None:
    kwargs: dict[str, Any] = dict(
        kind=str(event["kind"]),
        topic=str(event["topic"]),
        at=event.get("at") or utcnow(),
        payload=event.get("payload") or {},
        correlation_id=str(event.get("correlation_id", "-")),
        seq=int(event.get("seq", 0)),
    )
    if event.get("id"):
        kwargs["id"] = str(event["id"])
    session.add(EventLog(**kwargs))


async def record_connector_run(session: AsyncSession, run: Mapping[str, Any]) -> None:
    session.add(
        ConnectorRun(
            connector=str(run["connector"]),
            started_at=run.get("started_at") or utcnow(),
            finished_at=run.get("finished_at") or utcnow(),
            ok=bool(run.get("ok", True)),
            rows_in=int(run.get("rows_in", 0)),
            rows_new=int(run.get("rows_new", 0)),
            latency_ms=int(run.get("latency_ms", 0)),
            error=run.get("error"),
            degraded_mode=bool(run.get("degraded_mode", False)),
            detail=run.get("detail") or {},
        )
    )


async def record_ai_invocation(session: AsyncSession, payload: Mapping[str, Any]) -> None:
    session.add(
        AIInvocation(
            thread_id=payload.get("thread_id"),
            stage=str(payload.get("stage", "classify")),
            model=str(payload.get("model", "")),
            prompt_digest=str(payload.get("prompt_digest", "")),
            prompt_tokens=int(payload.get("prompt_tokens", 0)),
            completion_tokens=int(payload.get("completion_tokens", 0)),
            latency_ms=int(payload.get("latency_ms", 0)),
            ok=bool(payload.get("ok", True)),
            fell_back=bool(payload.get("fell_back", False)),
            grounded=bool(payload.get("grounded", True)),
            rejected_claims=list(payload.get("rejected_claims", [])),
            evidence_ids=list(payload.get("evidence_ids", [])),
            error=payload.get("error"),
        )
    )


async def set_system_state(session: AsyncSession, key: str, value: Mapping[str, Any]) -> None:
    row = await session.get(SystemState, key)
    if row is None:
        session.add(SystemState(key=key, value=dict(value)))
    else:
        row.value = dict(value)
        row.updated_at = utcnow()


async def get_system_state(session: AsyncSession, key: str) -> dict[str, Any] | None:
    row = await session.get(SystemState, key)
    return dict(row.value) if row else None


# --------------------------------------------------------------------------- #
# audit chain
# --------------------------------------------------------------------------- #
GENESIS_HASH = "0" * 64


def compute_audit_hash(
    *,
    prev_hash: str,
    seq: int,
    at: str,
    actor: str,
    action: str,
    resource: str,
    outcome: str,
    detail: Mapping[str, Any],
) -> str:
    payload = json.dumps(
        {
            "prev": prev_hash,
            "seq": seq,
            "at": at,
            "actor": actor,
            "action": action,
            "resource": resource,
            "outcome": outcome,
            "detail": detail,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def verify_audit_chain(session: AsyncSession, *, limit: int = 5000) -> dict[str, Any]:
    rows = (
        await session.execute(select(AuditEntry).order_by(AuditEntry.seq.asc()).limit(limit))
    ).scalars().all()
    prev = GENESIS_HASH
    for row in rows:
        expected = compute_audit_hash(
            prev_hash=prev,
            seq=row.seq,
            at=row.at.isoformat() if row.at else "",
            actor=row.actor,
            action=row.action,
            resource=row.resource,
            outcome=row.outcome,
            detail=dict(row.detail or {}),
        )
        if expected != row.hash or row.prev_hash != prev:
            return {"valid": False, "broken_at_seq": row.seq, "entries_checked": len(rows)}
        prev = row.hash
    return {"valid": True, "entries_checked": len(rows), "head": prev}


# --------------------------------------------------------------------------- #
# maintenance
# --------------------------------------------------------------------------- #
async def purge_old_readings(session: AsyncSession, *, retention_days: int = 30) -> int:
    """Retention policy: raw readings age out, aggregated twin history does not.

    Without this, an urban sensor mesh fills a 250 GB EOC volume in ~9 months.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    result = await session.execute(delete(SensorReading).where(SensorReading.observed_at < cutoff))
    return int(result.rowcount or 0)


async def table_counts(session: AsyncSession) -> dict[str, int]:
    counts: dict[str, int] = {}
    for model in (Asset, Relationship, Sensor, Simulation, Incident, Task, Resource, Anomaly, AuditEntry):
        counts[model.__tablename__] = int(
            (await session.execute(select(func.count()).select_from(model))).scalar() or 0
        )
    return counts
