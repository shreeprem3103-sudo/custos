"""Operational endpoints: health, readiness, ingest, tasks, resources, audit, AI."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from ... import __version__
from ...config import settings
from ...domain.enums import EventKind, OperatingMode, Permission, TaskStatus, freshness_band
from ...domain.schemas import (
    AnomalyOut,
    AuditEntryOut,
    HealthOut,
    IngestBatch,
    IngestResult,
    ResourceOut,
    TaskCreate,
    TaskOut,
)
from ...fusion import repository
from ...infrastructure import db
from ...observability import METRICS, Timer, configure_logging, current_correlation_id, get_logger
from ...realtime.bus import BUS, HeartbeatPublisher, LagTracker, Topics
from ...services import store
from ...services.ai import REASONER, collect_numbers
from ...services.pipeline import ORCHESTRATOR
from ...services.connectors import Reading
from ..deps import AdminDep, CommitterDep, IngestDep, ReaderDep, SnapshotDep, storage_mode

router = APIRouter(tags=["operations"])
log = get_logger("api.ops")

STARTED_AT = time.monotonic()
LAG = LagTracker()


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #
@router.get("/health", response_model=HealthOut, summary="Liveness, integrity and degradation report")
async def health(identity: ReaderDep, response: Any = None) -> HealthOut:  # noqa: ANN401 - FastAPI injects Response
    """Deliberately verbose: an EOC needs to know *what* is degraded, not just that something is."""
    checks: dict[str, dict[str, Any]] = {}
    warnings: list[str] = list(settings.boot_warnings)

    with Timer("health.db"):
        try:
            ping = await db.ping()
            counts = {}
            try:
                async with store.session_scope() as session:
                    counts = await repository.table_counts(session)
            except Exception:  # noqa: BLE001
                counts = {}
            checks["database"] = {
                "status": "ok" if ping.get("ok") else "degraded",
                "latency_ms": ping.get("latency_ms"),
                "tables": counts,
                **storage_mode(),
            }
        except Exception as exc:  # noqa: BLE001
            checks["database"] = {"status": "critical", "error": str(exc)[:200], **storage_mode()}
            warnings.append("operational database unreachable: the console is serving the last known good twin")

    orchestrator = ORCHESTRATOR.status()
    checks["pipeline"] = {
        "status": "ok" if orchestrator["last_cycle"] and not orchestrator["last_cycle"]["degraded"] else ("degraded" if orchestrator["last_cycle"] else "unknown"),
        "cycles": orchestrator["cycles"],
        "last_cycle_at": (orchestrator["last_cycle"] or {}).get("at"),
        "duration_ms": (orchestrator["last_cycle"] or {}).get("duration_ms"),
    }
    stale_connectors = [c for c in orchestrator["connectors"] if not c["ok"]]
    checks["connectors"] = {
        "status": "degraded" if stale_connectors else ("ok" if orchestrator["connectors"] else "unknown"),
        "count": len(orchestrator["connectors"]),
        "degraded": [c["name"] for c in stale_connectors],
        "detail": orchestrator["connectors"],
    }
    ai_health = await REASONER.client.health()
    checks["ai"] = {
        "status": "ok" if ai_health.get("reachable") else "degraded",
        "detail": ai_health,
        "note": "the platform is fully operable without the language model; the deterministic engines remain the source of truth",
        "stats": REASONER.snapshot_stats(),
    }
    checks["events"] = {"status": "ok", **BUS.stats()}

    try:
        async with store.session_scope() as session:
            chain = await store.verify_audit_chain(session, limit=2000)
        checks["audit_chain"] = {"status": "ok" if chain.get("valid") else "critical", **chain}
        if not chain.get("valid"):
            warnings.append(f"audit chain is broken at sequence {chain.get('broken_at_seq')}")
    except Exception as exc:  # noqa: BLE001
        checks["audit_chain"] = {"status": "unknown", "error": str(exc)[:160]}

    critical = [name for name, check in checks.items() if check.get("status") == "critical"]
    degraded = [name for name, check in checks.items() if check.get("status") == "degraded"]
    overall = "critical" if critical else ("degraded" if degraded else "ok")
    mode = OperatingMode.NORMAL
    if storage_mode().get("read_only"):
        mode = OperatingMode.READ_ONLY
    elif degraded:
        mode = OperatingMode.DEGRADED
    return HealthOut(
        status=overall,  # type: ignore[arg-type]
        mode=mode,
        version=__version__,
        uptime_s=round(time.monotonic() - STARTED_AT, 1),
        city=settings.city_name,
        checks=checks,
        stages=[],
        ai=ai_health,
        metrics=METRICS.snapshot() if hasattr(METRICS, "snapshot") else {},
        warnings=warnings,
    )


@router.get("/ready", summary="Readiness for a load balancer")
async def ready() -> dict[str, Any]:
    try:
        await db.ping()
        return {"ready": True, "at": datetime.now(timezone.utc)}
    except Exception:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage is not ready")


@router.get("/stages", summary="The six-stage operating loop, as the console renders it")
async def stages(identity: ReaderDep) -> dict[str, Any]:
    status_payload = ORCHESTRATOR.status()
    last = status_payload.get("last_cycle") or {}
    return {
        "stages": last.get("stages") or [],
        "order": ["collect", "fuse", "understand", "predict", "simulate", "recommend"],
        "last_cycle_at": last.get("at"),
        "cycle_id": last.get("cycle_id"),
        "cycles": status_payload.get("cycles"),
        "prediction": status_payload.get("prediction", {}),
        "anomalies": last.get("anomalies", 0),
        "origin": "the pipeline is the only writer of this view; the console never invents a stage state",
    }


@router.post("/cycle", summary="Force a full operating-loop pass now")
async def run_cycle(identity: AdminDep, force: Annotated[bool, Query()] = True) -> dict[str, Any]:
    cycle = await ORCHESTRATOR.run_cycle(trigger=f"operator:{identity.subject}", force=force)
    return {
        "cycle_id": cycle.cycle_id,
        "duration_ms": cycle.duration_ms,
        "degraded": cycle.degraded,
        "readings_ingested": cycle.readings_ingested,
        "anomalies": len(cycle.anomalies),
        "stages": [record.to_model().model_dump(mode="json") for record in cycle.stages],
    }


@router.post("/drills/rainfall", summary="Drive the telemetry replay (training and drills)")
async def set_drill_rainfall(identity: AdminDep, payload: dict[str, Any]) -> dict[str, Any]:
    mm_h = float(payload.get("rainfall_mm_h") or 0.0)
    ORCHESTRATOR.set_scenario_rain(mm_h)
    async with store.session_scope() as session:
        await store.write_audit(session, actor=identity.subject, role=identity.role.value, action="config", resource="drill.rainfall", detail={"rainfall_mm_h": mm_h})
    return {"rainfall_mm_h": mm_h, "applies_to": "synthetic telemetry replay"}


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #
@router.post("/ingest/readings", response_model=IngestResult, summary="Store-and-forward sensor ingest")
async def ingest_readings(identity: IngestDep, batch: IngestBatch) -> IngestResult:
    """Accept a gateway batch, deduplicating by construction.

    A field gateway that lost its uplink will resend; the dedupe key means the
    resend is free and cannot double-count a flood peak.
    """
    accepted = duplicates = rejected = 0
    errors: list[str] = []
    from ...fusion.normalize import (
        canonical_metric,
        convert_value,
        dedupe_key,
        in_plausible_range,
        is_plausible_timestamp,
        quality_score,
    )

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    now = datetime.now(timezone.utc)
    for reading in batch.readings:
        try:
            metric = canonical_metric(reading.metric)
            observed_at = reading.observed_at
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            # a gateway may speak centimetres, or send the same batch twice after
            # a failed uplink: normalise the unit, and treat a repeat *inside* the
            # batch as the duplicate it is rather than letting the insert collide
            value, unit, penalty = convert_value(metric, float(reading.value), reading.unit)
            key = dedupe_key(reading.sensor_id, metric, observed_at, value)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            quality = quality_score(
                timestamp_ok=is_plausible_timestamp(observed_at),
                unit_known=bool(unit),
                in_range=in_plausible_range(metric, value),
                is_duplicate=False,
                provider_quality=max(0.0, 1.0 - penalty),
                staleness_s=max(0.0, (now - observed_at).total_seconds()),
            )
            rows.append(
                {
                    "sensor_id": reading.sensor_id,
                    "metric": metric,
                    "value": value,
                    "unit": unit or reading.unit,
                    "observed_at": observed_at,
                    "quality": quality,
                    "source": f"gateway:{batch.gateway_id}",
                    "dedupe_key": key,
                    "raw": dict(reading.raw or {}),
                }
            )
        except Exception as exc:  # noqa: BLE001
            rejected += 1
            errors.append(f"{reading.sensor_id}: {str(exc)[:120]}")
    try:
        async with store.session_scope() as session:
            for row in rows:
                if await repository.insert_reading(session, row):
                    accepted += 1
                else:
                    duplicates += 1
            await store.write_audit(
                session,
                actor=identity.subject,
                role=identity.role.value,
                action="ingest",
                resource=f"gateway:{batch.gateway_id}",
                detail={"accepted": accepted, "duplicates": duplicates, "rejected": rejected},
            )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"readings could not be stored: {str(exc)[:160]}") from exc
    if accepted:
        await BUS.publish(
            Topics.SENSOR_READING,
            {"gateway": batch.gateway_id, "accepted": accepted, "duplicates": duplicates},
            source=identity.subject,
        )
    return IngestResult(accepted=accepted, duplicates=duplicates, rejected=rejected, errors=errors[:20], server_time=datetime.now(timezone.utc))


@router.post("/ingest/observation", summary="Operator observation entered from the control room")
async def manual_observation(identity: CommitterDep, payload: dict[str, Any]) -> dict[str, Any]:
    """A telephone report is still a data source.

    Tagged with the operator's identity and a capped quality score, so the fusion
    layer weights it below instrumented telemetry when the two disagree.
    """
    metric = str(payload.get("metric") or "").strip()
    if not metric:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "metric is required (e.g. flood_depth_m)")
    try:
        value = float(payload.get("value"))
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "value must be a number")
    from ...fusion.normalize import canonical_metric, dedupe_key, quality_score

    asset_id = payload.get("asset_id")
    connector = ORCHESTRATOR.registry.get("manual-observation") if ORCHESTRATOR.registry else None
    if connector is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "manual ingest is not available on this instance")
    now = datetime.now(timezone.utc)
    canonical = canonical_metric(metric)
    reading = Reading(
        sensor_id=str(payload.get("sensor_id") or f"manual:{identity.subject}:{asset_id or 'city'}"),
        metric=canonical,
        value=value,
        unit=str(payload.get("unit") or ""),
        at=now,
        asset_id=str(asset_id) if asset_id else None,
        quality=min(0.85, quality_score(timestamp_ok=True, unit_known=True, in_range=True, is_duplicate=False)),
        source=f"operator:{identity.subject}",
        raw={"note": str(payload.get("note") or "")},
    )
    connector.submit(reading)  # type: ignore[attr-defined]
    async with store.session_scope() as session:
        if asset_id:
            await repository.touch_asset_state(
                session,
                str(asset_id),
                flood_depth_m=value if canonical in {"flood_depth_m", "water_level_m"} else None,
                source=f"operator:{identity.subject}",
                confidence=0.75,
            )
        await store.write_audit(
            session,
            actor=identity.subject,
            role=identity.role.value,
            action="ingest",
            resource=str(asset_id or "city"),
            detail={"metric": canonical, "value": value, "note": str(payload.get("note") or "")},
        )
    return {"accepted": True, "dedupe_key": dedupe_key(reading.sensor_id, canonical, now, value), "at": now}


# --------------------------------------------------------------------------- #
# tasks & resources
# --------------------------------------------------------------------------- #
@router.get("/tasks", summary="Task board")
async def list_tasks(
    identity: ReaderDep,
    status_filter: Annotated[TaskStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    async with store.session_scope() as session:
        rows = await store.list_tasks(session, status=status_filter.value if status_filter else None, limit=limit)
    return {"count": len(rows), "tasks": rows}


@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED, summary="Create a task")
async def create_task(identity: CommitterDep, payload: TaskCreate) -> TaskOut:
    async with store.session_scope() as session:
        task_id = await store.create_task(session, payload.model_dump(mode="json"), actor=identity.subject)
        rows = await store.list_tasks(session, limit=200)
    row = next((item for item in rows if item["id"] == task_id), None)
    if row is None:  # pragma: no cover - defensive
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "task was created but could not be read back")
    return TaskOut(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        priority=int(row["priority"]),
        due_at=row.get("due_at"),
        incident_id=row.get("incident_id"),
        simulation_id=row.get("simulation_id"),
        recommendation_id=row.get("recommendation_id"),
        assigned_to=row.get("assigned_to"),
        target_asset_id=row.get("target_asset_id"),
        resource_requirements=row.get("resource_requirements") or {},
        status=row["status"],
        created_at=row.get("created_at") or datetime.now(timezone.utc),
        updated_at=row.get("updated_at") or datetime.now(timezone.utc),
        created_by=row.get("created_by") or identity.subject,
        history=row.get("history") or [],
    )


@router.patch("/tasks/{task_id}", summary="Update a task")
async def update_task(identity: CommitterDep, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with store.session_scope() as session:
        ok = await store.update_task(session, task_id, changes=payload, actor=identity.subject)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"task {task_id} not found")
    await BUS.publish(Topics.TASK, {"task_id": task_id, "changes": payload}, source=identity.subject)
    return {"id": task_id, "updated": True}


@router.get("/resources", summary="Resource inventory with live availability")
async def list_resources(identity: ReaderDep, resource_type: Annotated[str | None, Query()] = None) -> dict[str, Any]:
    async with store.session_scope() as session:
        rows = await store.list_resources(session, resource_type=resource_type)
    return {
        "count": len(rows),
        "resources": [
            ResourceOut(
                id=row["id"],
                name=row["name"],
                resource_type=row["resource_type"],
                home_asset_id=row.get("home_asset_id"),
                quantity=float(row["quantity"]),
                available=float(row["available"]),
                capabilities=row.get("capabilities") or {},
                status=row.get("status") or "available",
            ).model_dump(mode="json")
            for row in rows
        ],
    }


# --------------------------------------------------------------------------- #
# anomalies / events / audit
# --------------------------------------------------------------------------- #
@router.get("/anomalies", response_model=list[AnomalyOut], summary="Sensor anomalies with their evidence")
async def list_anomalies(
    identity: ReaderDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    since_minutes: Annotated[int | None, Query(ge=1, le=1440)] = None,
) -> list[AnomalyOut]:
    async with store.session_scope() as session:
        rows = await store.list_anomalies(session, limit=limit, since_minutes=since_minutes)
    return [
        AnomalyOut(
            id=row["id"],
            sensor_id=row["sensor_id"],
            metric=row["metric"],
            observed_at=row["observed_at"],
            observed_value=float(row["value"]),
            expected_value=float(row["expected"]),
            z_score=float(row["z_score"]),
            severity=row["severity"],
            method=row["method"],
            explanation=row["explanation"],
            confidence=float(row["confidence"]),
            acknowledged=bool(row["acknowledged"]),
            evidence=row.get("evidence") or {},
        )
        for row in rows
    ]


@router.post("/anomalies/{anomaly_id}/acknowledge", summary="Acknowledge an anomaly")
async def acknowledge(identity: CommitterDep, anomaly_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    note = str((payload or {}).get("note") or "")
    async with store.session_scope() as session:
        ok = await store.acknowledge_anomaly(session, anomaly_id, actor=identity.subject, note=note)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"anomaly {anomaly_id} not found")
    return {"id": anomaly_id, "acknowledged": True}


@router.get("/events", summary="Event stream (persisted, replayable)")
async def list_events(
    identity: ReaderDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    topic: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    async with store.session_scope() as session:
        rows = await store.list_events(session, limit=limit, topic=topic)
    return {"count": len(rows), "events": rows, "bus": BUS.stats()}


@router.get("/events/replay", summary="Replay the in-memory ring (used when a browser reconnects)")
async def replay_events(identity: ReaderDep, since_seq: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> dict[str, Any]:
    events = BUS.replay(since_seq, limit=limit)
    return {"events": [event.to_wire() for event in events], "count": len(events)}


@router.get("/audit", response_model=list[AuditEntryOut], summary="Tamper-evident audit trail")
async def list_audit(
    identity: ReaderDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    actor: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
) -> list[AuditEntryOut]:
    async with store.session_scope() as session:
        rows = await store.list_audit(session, limit=limit, actor=actor, action=action)
    return [
        AuditEntryOut(
            seq=int(row["seq"]),
            at=row["at"],
            actor=row["actor"],
            role=row.get("role") or "system",
            action=row["action"],
            resource=row["resource"],
            outcome=row["outcome"],
            detail=row.get("detail") or {},
            hash=row["entry_hash"],
            prev_hash=row["previous_hash"],
            correlation_id=row.get("correlation_id") or "-",
        )
        for row in rows
    ]


@router.get("/audit/verify", summary="Recompute the audit hash chain")
async def verify_audit(identity: ReaderDep, limit: Annotated[int, Query(ge=10, le=20000)] = 5000) -> dict[str, Any]:
    async with store.session_scope() as session:
        return await store.verify_audit_chain(session, limit=limit)


@router.get("/metrics", summary="In-process metrics")
async def metrics(identity: AdminDep) -> dict[str, Any]:
    return {
        "metrics": METRICS.snapshot() if hasattr(METRICS, "snapshot") else {},
        "bus": BUS.stats(),
        "lag": LAG.snapshot(),
        "orchestrator": ORCHESTRATOR.status(),
    }
