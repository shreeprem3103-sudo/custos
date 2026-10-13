"""Operational records store: incidents, tasks, simulations, anomalies, audit.

Split from :mod:`custos.fusion.repository` on purpose. That module answers *"what
is the state of the city right now?"* and is on the hot path of every engine.
This module answers *"what has the command centre decided and recorded?"* and is
on the hot path of every request. Keeping them apart means a heavy audit query
can never evict the twin cache, and it makes the two different retention
policies (snapshot: seconds; records: years) obvious to whoever maintains this.

Every write here is also an audit event: an after-action review must be able to
reconstruct *who* ordered *what*, *when*, on the strength of *which* model run.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.enums import IncidentCategory, IncidentStatus, SimulationStatus, TaskStatus
from ..domain.schemas import (
    AuditEntryOut,
    IncidentOut,
    KPISet,
    KPIUncertainty,
    Recommendation,
    SimulationRequest,
    SimulationResult,
    SimulationSummary,
    TaskOut,
)
from ..infrastructure import db
from ..infrastructure.models import (
    Anomaly,
    Asset,
    AuditEntry,
    CascadeEdge,
    EventLog,
    Incident,
    IncidentAsset,
    IncidentEvent,
    IncidentUpdate,
    RecommendationRecord,
    Resource,
    Simulation,
    SimulationEvent,
    Task,
    utcnow,
)
from ..observability import get_logger

log = get_logger("services.store")

#: re-exported so callers have a single import for "talk to storage"
session_scope = db.session_scope


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# --------------------------------------------------------------------------- #
# twin-adjacent upserts the seeder needs
# --------------------------------------------------------------------------- #
async def upsert_zone(session: AsyncSession, zone: Mapping[str, Any]) -> None:
    from ..infrastructure.models import Zone

    existing = await session.get(Zone, str(zone["id"]))
    # Only declared columns are mapped to columns. Everything else - including
    # scalars such as area_km2 and design_rainfall_mm_h, which the hydrology
    # engine reads from the zone state - is preserved in `attributes`. A seeder
    # must never be able to make a schema mistake look like a data problem.
    columns = {column.key for column in Zone.__table__.columns}
    data = {
        k: v
        for k, v in zone.items()
        if k in columns and k not in {"id", "attributes"} and not isinstance(v, (list, dict))
    }
    attributes = dict(zone.get("attributes") or {})
    for key, value in zone.items():
        if key in {"id", "attributes"}:
            continue
        if key not in columns or isinstance(value, (list, dict)):
            attributes[key] = value
    if existing is None:
        session.add(Zone(id=str(zone["id"]), attributes=attributes, **data))
        return
    for key, value in data.items():
        setattr(existing, key, value)
    existing.attributes = {**(existing.attributes or {}), **attributes}


async def upsert_resource(session: AsyncSession, resource: Mapping[str, Any]) -> None:
    existing = await session.get(Resource, str(resource["id"]))
    data = {k: v for k, v in resource.items() if k != "id"}
    if existing is None:
        session.add(Resource(id=str(resource["id"]), **data))
        return
    for key, value in data.items():
        setattr(existing, key, value)


async def upsert_incident(session: AsyncSession, incident: Mapping[str, Any]) -> str:
    incident_id = str(incident.get("id") or "")
    existing = await session.get(Incident, incident_id) if incident_id else None
    data = {k: v for k, v in incident.items() if k not in {"id", "timeline", "asset_links"}}
    if existing is None:
        row = Incident(**{**data, **({"id": incident_id} if incident_id else {})})
        session.add(row)
        incident_id = row.id or ""
    else:
        for key, value in data.items():
            setattr(existing, key, value)
    await _link_assets(session, incident_id, incident.get("asset_links") or [], replace=False)
    return incident_id


async def _link_assets(
    session: AsyncSession, incident_id: str, links: Sequence[Mapping[str, Any]], *, replace: bool
) -> None:
    """Attach assets to an incident, using only columns the table actually has."""
    from sqlalchemy import delete as _delete

    columns = {column.key for column in IncidentAsset.__table__.columns}
    if replace:
        await session.execute(_delete(IncidentAsset).where(IncidentAsset.incident_id == incident_id))
    for link in links:
        raw_impact = link.get("impact")
        row: dict[str, Any] = {
            "incident_id": incident_id,
            "asset_id": str(link["asset_id"]),
            # `role` is the operator-facing word ("hazard_source", "at_risk");
            # `impact` is the 0..1 weight the cascade uses. Keeping them in
            # separate columns is what stops a word being used as a number.
            "role": str(link.get("role") or (raw_impact if isinstance(raw_impact, str) else "affected")),
            "impact": float(link.get("weight") or (raw_impact if isinstance(raw_impact, (int, float)) else 0.5)),
        }
        row = {key: value for key, value in row.items() if key in columns}
        session.add(IncidentAsset(**row))


# --------------------------------------------------------------------------- #
# readings
# --------------------------------------------------------------------------- #
async def recent_readings(session: AsyncSession, *, minutes: int = 60, limit: int = 20_000) -> list[dict[str, Any]]:
    """Readings inside the fusion window, oldest first."""
    from ..infrastructure.models import SensorReading

    cutoff = utcnow() - timedelta(minutes=minutes)
    stmt = (
        select(
            SensorReading.sensor_id,
            SensorReading.metric,
            SensorReading.value,
            SensorReading.unit,
            SensorReading.quality,
            SensorReading.observed_at,
        )
        .where(SensorReading.observed_at >= cutoff)
        .order_by(SensorReading.observed_at)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "sensor_id": row.sensor_id,
            "metric": row.metric,
            "value": row.value,
            "unit": row.unit,
            "quality": row.quality,
            "observed_at": row.observed_at,
        }
        for row in rows
    ]


async def reading_counts_by_metric(session: AsyncSession, *, minutes: int = 60) -> dict[str, int]:
    from ..infrastructure.models import SensorReading

    cutoff = utcnow() - timedelta(minutes=minutes)
    stmt = (
        select(SensorReading.metric, func.count())
        .where(SensorReading.observed_at >= cutoff)
        .group_by(SensorReading.metric)
    )
    return {str(metric): int(count) for metric, count in (await session.execute(stmt)).all()}


async def record_anomaly(session: AsyncSession, anomaly: Mapping[str, Any]) -> None:
    severity = "critical" if abs(float(anomaly.get("z_score") or 0)) >= 8 else "warning" if abs(float(anomaly.get("z_score") or 0)) >= 5 else "watch"
    session.add(
        Anomaly(
            sensor_id=str(anomaly["sensor_id"]),
            metric=str(anomaly.get("metric") or ""),
            observed_at=anomaly.get("at") or utcnow(),
            observed_value=float(anomaly.get("value") or 0.0),
            expected_value=float(anomaly.get("baseline") or 0.0),
            z_score=float(anomaly.get("z_score") or 0.0),
            severity=severity,
            method="robust-zscore",
            explanation=str(anomaly.get("explanation") or ""),
            confidence=float(anomaly.get("confidence") or min(0.95, abs(float(anomaly.get("z_score") or 0)) / 12.0)),
            evidence={"sigma": anomaly.get("sigma"), "band": str(anomaly.get("band"))},
        )
    )


async def list_anomalies(session: AsyncSession, *, limit: int = 100, since_minutes: int | None = None) -> list[dict[str, Any]]:
    stmt = select(Anomaly).order_by(desc(Anomaly.observed_at)).limit(limit)
    if since_minutes:
        stmt = stmt.where(Anomaly.observed_at >= utcnow() - timedelta(minutes=since_minutes))
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": row.id,
            "sensor_id": row.sensor_id,
            "metric": row.metric,
            "observed_at": _iso(row.observed_at),
            "value": row.observed_value,
            "expected": row.expected_value,
            "z_score": row.z_score,
            "severity": row.severity,
            "method": row.method,
            "explanation": row.explanation,
            "confidence": row.confidence,
            "acknowledged": row.acknowledged,
            "evidence": row.evidence,
        }
        for row in rows
    ]


async def acknowledge_anomaly(session: AsyncSession, anomaly_id: str, *, actor: str, note: str = "") -> bool:
    row = await session.get(Anomaly, anomaly_id)
    if row is None:
        return False
    row.acknowledged = True
    row.explanation = (row.explanation + f" [acknowledged by {actor}: {note}]").strip()
    await write_audit(session, actor=actor, action="anomaly.acknowledge", resource=anomaly_id, outcome="ok", detail={"note": note})
    return True


# --------------------------------------------------------------------------- #
# simulations
# --------------------------------------------------------------------------- #
async def save_simulation(session: AsyncSession, result: SimulationResult, request: SimulationRequest, *, created_by: str = "system") -> str:
    row = Simulation(
        name=request.name,
        description=request.description,
        status=SimulationStatus.COMPLETE.value if hasattr(SimulationStatus.COMPLETE, "value") else str(SimulationStatus.COMPLETE),
        created_by=created_by,
        baseline_id=result.baseline_id,
        horizon_minutes=request.horizon_minutes,
        timestep_minutes=request.timestep_minutes,
        ensemble_size=request.ensemble_size,
        random_seed=request.random_seed or 0,
        request=json.loads(request.model_dump_json()),
        result=json.loads(result.model_dump_json()),
        kpis=result.kpis.model_dump(),
        uncertainty=result.uncertainty.model_dump(),
        timeline={"frames": [frame.model_dump(mode="json") for frame in result.timeline]},
        cascade={"impacts": [impact.model_dump(mode="json") for impact in result.cascade]},
        recommendations={"items": [rec.model_dump(mode="json") for rec in result.recommendations]},
        explainability=result.explainability.model_dump(mode="json"),
        sensitivity={"items": [item.model_dump(mode="json") for item in result.sensitivity]},
        narrative=result.narrative,
        determinism_token=result.determinism_token,
        degraded=result.degraded,
        runtime_ms=int(result.runtime_ms),
        completed_at=utcnow(),
    )
    if result.id and not result.id.startswith("preview"):
        row.id = result.id
    session.add(row)
    await session.flush()

    for impact in result.cascade[:400]:
        # the persisted cascade edge is the same shape as the API's CascadeImpact:
        # a reviewer reading the database and a reviewer reading the console must
        # see the identical record, not two interpretations of one event
        session.add(
            CascadeEdge(
                simulation_id=row.id,
                timestep_min=int(impact.timestep_min),
                cause_asset_id=impact.cause_asset_id,
                target_asset_id=impact.target_asset_id,
                mechanism=str(impact.mechanism)[:64],
                relation_kind=str(impact.relation_kind) if impact.relation_kind else None,
                hop=int(impact.hop),
                magnitude=float(impact.magnitude),
                probability=float(impact.probability),
                status_after=str(impact.status_after),
                evidence_ids=list(impact.evidence_ids or []),
            )
        )
    for rec in result.recommendations:
        session.add(
            RecommendationRecord(
                simulation_id=row.id,
                rank=int(rec.rank),
                action=rec.action,
                intervention_type=str(rec.intervention_type),
                target_id=rec.target_id,
                window=rec.window,
                rationale=rec.rationale,
                expected_kpi_delta=rec.expected_kpi_delta,
                resources_required=rec.resources_required,
                counterfactual=rec.counterfactual,
                confidence=rec.confidence,
                feasibility=rec.feasibility,
                urgency=rec.urgency,
                evidence_ids=rec.evidence_ids or [],
            )
        )
    return row.id


async def get_simulation(session: AsyncSession, simulation_id: str) -> dict[str, Any] | None:
    row = await session.get(Simulation, simulation_id)
    if row is None:
        return None
    return _simulation_summary(row) | {"result": row.result, "uncertainty": row.uncertainty, "explainability": row.explainability}


async def list_simulations(session: AsyncSession, *, limit: int = 50, name_contains: str | None = None) -> list[dict[str, Any]]:
    stmt = select(Simulation).order_by(desc(Simulation.created_at)).limit(limit)
    if name_contains:
        stmt = stmt.where(Simulation.name.ilike(f"%{name_contains}%"))
    rows = (await session.execute(stmt)).scalars().all()
    return [_simulation_summary(row) for row in rows]


def _simulation_summary(row: Simulation) -> dict[str, Any]:
    kpis = row.kpis or {}
    return {
        "id": row.id,
        "name": row.name,
        "status": row.status,
        "created_at": _iso(row.created_at),
        "completed_at": _iso(row.completed_at),
        "created_by": row.created_by,
        "baseline_id": row.baseline_id,
        "horizon_minutes": row.horizon_minutes,
        "ensemble_size": row.ensemble_size,
        "runtime_ms": row.runtime_ms,
        "determinism_token": row.determinism_token,
        "degraded": row.degraded,
        "narrative": row.narrative,
        "kpis": kpis,
        "recommendation_count": len((row.recommendations or {}).get("items", [])),
        "cascade_edges": len((row.cascade or {}).get("impacts", [])),
    }


async def record_simulation_event(session: AsyncSession, simulation_id: str, *, seq: int, stage: str, progress: float, message: str, payload: Mapping[str, Any] | None = None) -> None:
    session.add(
        SimulationEvent(
            simulation_id=simulation_id,
            seq=seq,
            stage=stage,
            progress=progress,
            message=message,
            payload=dict(payload or {}),
        )
    )


# --------------------------------------------------------------------------- #
# incidents & tasks
# --------------------------------------------------------------------------- #
async def create_incident(session: AsyncSession, payload: Mapping[str, Any], *, actor: str) -> str:
    row = Incident(
        title=str(payload["title"]),
        category=str(payload.get("category") or IncidentCategory.URBAN_FLOOD.value),
        severity=int(payload.get("severity") or 3),
        status=str(payload.get("status") or IncidentStatus.DETECTED.value),
        confidence=float(payload.get("confidence") or 0.6),
        summary=str(payload.get("summary") or ""),
        lon=float(payload.get("lon") or 0.0),
        lat=float(payload.get("lat") or 0.0),
        radius_m=float(payload.get("radius_m") or 400.0),
        zone=payload.get("zone"),
        population_exposed=int(payload.get("population_exposed") or 0),
        reported_by=str(payload.get("reported_by") or actor),
        detected_at=payload.get("detected_at") or utcnow(),
        explainability=dict(payload.get("explainability") or {}),
        attributes=dict(payload.get("attributes") or {}),
    )
    session.add(row)
    await session.flush()
    await _link_assets(session, row.id, payload.get("asset_links") or [], replace=False)
    session.add(
        IncidentEvent(
            incident_id=row.id,
            stage="collect",
            actor=actor,
            detail=f"incident opened: {row.title} ({row.category}, severity {row.severity})",
        )
    )
    await write_audit(session, actor=actor, action="incident.create", resource=row.id, outcome="ok", detail={"severity": row.severity, "category": row.category})
    return row.id


async def list_incidents(session: AsyncSession, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    stmt = select(Incident).order_by(desc(Incident.detected_at)).limit(limit)
    if status:
        stmt = stmt.where(Incident.status == status)
    rows = (await session.execute(stmt)).scalars().all()
    return [_incident_view(row) for row in rows]


async def get_incident(session: AsyncSession, incident_id: str) -> dict[str, Any] | None:
    row = await session.get(Incident, incident_id)
    if row is None:
        return None
    view = _incident_view(row)
    events = (
        await session.execute(select(IncidentEvent).where(IncidentEvent.incident_id == incident_id).order_by(IncidentEvent.at))
    ).scalars().all()
    updates = (
        await session.execute(select(IncidentUpdate).where(IncidentUpdate.incident_id == incident_id).order_by(IncidentUpdate.at))
    ).scalars().all()
    view["timeline"] = [
        {
            "at": e.at,
            "stage": e.stage,
            "actor": e.actor,
            "detail": e.detail,
            "evidence_ids": list(e.evidence_ids or []),
        }
        for e in events
    ]
    view["journal"] = [
        {"at": _iso(u.at), "author": u.author, "kind": u.kind, "body": u.body} for u in updates
    ]
    links = (await session.execute(select(IncidentAsset).where(IncidentAsset.incident_id == incident_id))).scalars().all()
    view["asset_links"] = [{"asset_id": link.asset_id, "role": link.role, "impact": link.impact} for link in links]
    return view


def _incident_view(row: Incident) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "category": row.category,
        "severity": row.severity,
        "status": row.status,
        "confidence": row.confidence,
        "summary": row.summary,
        "lon": row.lon,
        "lat": row.lat,
        "radius_m": row.radius_m,
        "zone": row.zone,
        "population_exposed": row.population_exposed,
        "reported_by": row.reported_by,
        "detected_at": _iso(row.detected_at),
        "resolved_at": _iso(row.resolved_at),
        "explainability": row.explainability,
        "attributes": row.attributes,
    }


async def update_incident(session: AsyncSession, incident_id: str, *, changes: Mapping[str, Any], actor: str, note: str = "") -> bool:
    row = await session.get(Incident, incident_id)
    if row is None:
        return False
    before = {"status": row.status, "severity": row.severity}
    for key in ("status", "severity", "summary", "confidence", "population_exposed", "zone"):
        if key in changes and changes[key] is not None:
            setattr(row, key, changes[key])
    if str(row.status) in {IncidentStatus.RESOLVED.value, IncidentStatus.CLOSED.value}:
        row.resolved_at = utcnow()
    session.add(
        IncidentEvent(
            incident_id=incident_id,
            # the stage a change came from is the interesting part of the record:
            # it tells the reviewer whether a human or an engine moved this incident
            stage=str(changes.get("stage") or "understand"),
            actor=actor,
            detail=note or f"updated from {before} to status={row.status} severity={row.severity}",
            evidence_ids=[f"operator:{actor}"] if actor != "system" else [],
        )
    )
    await write_audit(session, actor=actor, action="incident.update", resource=incident_id, outcome="ok", detail={"changes": dict(changes)})
    return True


async def add_incident_update(session: AsyncSession, incident_id: str, *, kind: str, body: str, author: str) -> bool:
    if await session.get(Incident, incident_id) is None:
        return False
    session.add(IncidentUpdate(incident_id=incident_id, kind=kind, body=body, author=author))
    await write_audit(session, actor=author, action="incident.note", resource=incident_id, outcome="ok", detail={"kind": kind})
    return True


async def create_task(session: AsyncSession, payload: Mapping[str, Any], *, actor: str) -> str:
    row = Task(
        title=str(payload["title"]),
        description=str(payload.get("description") or ""),
        status=str(payload.get("status") or TaskStatus.PROPOSED.value),
        priority=int(payload.get("priority") or 3),
        due_at=payload.get("due_at"),
        incident_id=payload.get("incident_id"),
        simulation_id=payload.get("simulation_id"),
        recommendation_id=payload.get("recommendation_id"),
        assigned_to=payload.get("assigned_to"),
        target_asset_id=payload.get("target_asset_id"),
        resource_requirements=dict(payload.get("resource_requirements") or {}),
        created_by=actor,
        history=[{"at": utcnow().isoformat(), "actor": actor, "event": "created"}],
    )
    session.add(row)
    await session.flush()
    await write_audit(session, actor=actor, action="task.create", resource=row.id, outcome="ok", detail={"priority": row.priority, "priority_note": "task opened"})
    return row.id


async def list_tasks(session: AsyncSession, *, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    stmt = select(Task).order_by(desc(Task.priority), desc(Task.created_at)).limit(limit)
    if status:
        stmt = stmt.where(Task.status == status)
    rows = (await session.execute(stmt)).scalars().all()
    return [_task_view(row) for row in rows]


def _task_view(row: Task) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "description": row.description,
        "status": row.status,
        "priority": row.priority,
        "due_at": _iso(row.due_at),
        "incident_id": row.incident_id,
        "simulation_id": row.simulation_id,
        "recommendation_id": row.recommendation_id,
        "assigned_to": row.assigned_to,
        "target_asset_id": row.target_asset_id,
        "resource_requirements": row.resource_requirements,
        "created_by": row.created_by,
        "created_at": _iso(row.created_at),
        "history": row.history,
    }


async def update_task(session: AsyncSession, task_id: str, *, changes: Mapping[str, Any], actor: str) -> bool:
    row = await session.get(Task, task_id)
    if row is None:
        return False
    for key in ("status", "priority", "assigned_to", "due_at", "description"):
        if key in changes and changes[key] is not None:
            setattr(row, key, changes[key])
    history = list(row.history or [])
    history.append({"at": utcnow().isoformat(), "actor": actor, "event": "updated", "changes": dict(changes)})
    row.history = history
    await write_audit(session, actor=actor, action="task.update", resource=task_id, outcome="ok", detail={"changes": dict(changes)})
    return True


async def list_resources(session: AsyncSession, *, resource_type: str | None = None) -> list[dict[str, Any]]:
    stmt = select(Resource).order_by(Resource.resource_type, Resource.name)
    if resource_type:
        stmt = stmt.where(Resource.resource_type == resource_type)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "resource_type": row.resource_type,
            "home_asset_id": row.home_asset_id,
            "quantity": row.quantity,
            "available": row.available,
            "capabilities": row.capabilities,
            "status": row.status,
        }
        for row in rows
    ]


async def assign_resources(session: AsyncSession, *, task_id: str, assignments: Sequence[Mapping[str, Any]], actor: str) -> int:
    from ..infrastructure.models import ResourceAssignment

    created = 0
    for assignment in assignments:
        resource = await session.get(Resource, str(assignment["resource_id"]))
        if resource is None:
            continue
        quantity = float(assignment.get("quantity") or 1.0)
        if resource.available < quantity:
            continue  # never over-commit a resource: the UI shows what is actually free
        resource.available -= quantity
        session.add(
            ResourceAssignment(
                resource_id=resource.id,
                task_id=task_id,
                quantity=quantity,
                from_asset_id=assignment.get("from_asset_id"),
                note=str(assignment.get("note") or ""),
            )
        )
        created += 1
    await write_audit(session, actor=actor, action="resource.assign", resource=task_id, outcome="ok", detail={"assigned": created})
    return created


# --------------------------------------------------------------------------- #
# events & audit
# --------------------------------------------------------------------------- #
async def list_events(session: AsyncSession, *, limit: int = 200, topic: str | None = None) -> list[dict[str, Any]]:
    stmt = select(EventLog).order_by(desc(EventLog.seq)).limit(limit)
    if topic:
        stmt = stmt.where(EventLog.topic == topic)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": row.id,
            "seq": row.seq,
            "kind": row.kind,
            "topic": row.topic,
            "at": _iso(row.at),
            "payload": row.payload,
            "correlation_id": row.correlation_id,
            "source": row.source,
        }
        for row in rows
    ]


#: the chain's anchor: an empty log verifies against the genesis hash
GENESIS_HASH = "0" * 64


async def write_audit(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    resource: str = "",
    outcome: str = "ok",
    detail: Mapping[str, Any] | None = None,
    correlation_id: str | None = None,
    role: str = "commander",
) -> str:
    """Append to the tamper-evident chain.

    The chain lives in :mod:`custos.fusion.repository` because the twin's writes
    were the first thing that needed it; duplicating the hash function here would
    eventually produce two chains that disagree, which is worse than no chain.
    """
    from sqlalchemy import func

    from ..fusion.repository import canonical_audit_time, compute_audit_hash

    # The head must be read *after* flushing our own pending entries: sessions run
    # with autoflush off, and without this every audit row written inside one
    # request would chain off the same stale head, silently breaking the trail.
    await session.flush()
    head_stmt = select(AuditEntry).order_by(desc(AuditEntry.seq)).limit(1)
    if db.dialect_name() != "sqlite":
        # two operators acting at once must not both chain off the same predecessor;
        # on PostgreSQL this serialises audit writes, on SQLite the writer lock does
        head_stmt = head_stmt.with_for_update()
    last = (await session.execute(head_stmt)).scalar_one_or_none()
    prev_hash = last.hash if last is not None else GENESIS_HASH
    # The sequence is assigned here rather than by the database so the number that
    # goes into the hash is provably the number that is stored: a conflict raises a
    # unique violation and the write is retried, never silently mis-numbered.
    next_seq = (last.seq + 1) if last is not None else 1
    at = utcnow()
    entry_hash = compute_audit_hash(
        prev_hash=prev_hash,
        seq=next_seq,
        at=canonical_audit_time(at),
        actor=actor,
        action=action,
        resource=resource,
        outcome=outcome,
        detail=dict(detail or {}),
    )
    session.add(
        AuditEntry(
            seq=next_seq,
            at=at,
            actor=actor,
            role=role,
            action=action,
            resource=resource,
            outcome=outcome,
            detail=dict(detail or {}),
            hash=entry_hash,
            prev_hash=prev_hash,
            correlation_id=correlation_id,
        )
    )
    await session.flush()
    return entry_hash


async def list_audit(session: AsyncSession, *, limit: int = 200, actor: str | None = None, action: str | None = None) -> list[dict[str, Any]]:
    stmt = select(AuditEntry).order_by(desc(AuditEntry.seq)).limit(limit)
    if actor:
        stmt = stmt.where(AuditEntry.actor == actor)
    if action:
        stmt = stmt.where(AuditEntry.action == action)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "seq": row.seq,
            "id": row.id,
            "at": _iso(row.at),
            "actor": row.actor,
            "action": row.action,
            "resource": row.resource,
            "outcome": row.outcome,
            "detail": row.detail,
            "previous_hash": row.prev_hash,
            "entry_hash": row.hash,
            "role": row.role,
            "correlation_id": row.correlation_id,
        }
        for row in rows
    ]


async def verify_audit_chain(session: AsyncSession, *, limit: int = 5000) -> dict[str, Any]:
    """Recompute the chain; delegated to the single implementation in the repository."""
    from ..fusion.repository import verify_audit_chain as _verify

    return await _verify(session, limit=limit)


async def table_counts(session: AsyncSession) -> dict[str, int]:
    """Row counts for the health page: cheap, and it catches a silent wipe."""
    from sqlalchemy import text

    counts: dict[str, int] = {}
    for table in ("assets", "relationships", "zones", "sensors", "sensor_readings", "incidents", "tasks", "resources", "simulations", "anomalies", "events", "audit_log"):
        try:
            counts[table] = int((await session.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar() or 0)
        except Exception:  # noqa: BLE001 - a missing table is reported, not fatal
            counts[table] = -1
    return counts


async def asset_map(session: AsyncSession) -> dict[str, str]:
    """id -> name for every asset; used to humanise cascade and audit records."""
    rows = (await session.execute(select(Asset.id, Asset.name))).all()
    return {str(row[0]): str(row[1]) for row in rows}
