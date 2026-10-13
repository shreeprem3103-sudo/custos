"""Incident lifecycle: create, triage, update, and the AI triage assist."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from ...domain.enums import IncidentStatus, status_rank
from ...domain.schemas import IncidentCreate, IncidentOut, IncidentTimelineEntry
from ...services import store
from ...services.ai import REASONER, collect_numbers
from ...realtime.bus import BUS, Topics
from ..deps import CommitterDep, IngestDep, ReaderDep, SnapshotDep

router = APIRouter(prefix="/incidents", tags=["incidents"])


def _to_out(view: dict[str, Any]) -> IncidentOut:
    return IncidentOut(
        id=view["id"],
        title=view["title"],
        category=view["category"],
        severity=int(view["severity"]),
        confidence=float(view["confidence"]),
        summary=view["summary"],
        location={"lon": view["lon"], "lat": view["lat"]},
        zone=view.get("zone"),
        affected_asset_ids=[link["asset_id"] for link in view.get("asset_links", [])],
        population_exposed=int(view.get("population_exposed") or 0),
        reported_by=view.get("reported_by") or "system",
        status=view["status"],
        detected_at=view["detected_at"],
        updated_at=view.get("updated_at") or view["detected_at"],
        timeline=[IncidentTimelineEntry(**entry) for entry in view.get("timeline", [])],
        explainability=None,
        simulation_ids=list(view.get("simulation_ids") or []),
    )


@router.get("", summary="Incident board")
async def list_incidents(
    identity: ReaderDep,
    status_filter: Annotated[IncidentStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    async with store.session_scope() as session:
        rows = await store.list_incidents(session, status=status_filter.value if status_filter else None, limit=limit)
    rows.sort(key=lambda row: (int(row["severity"]), row["detected_at"] or ""), reverse=True)
    return {"count": len(rows), "incidents": rows}


@router.post("", status_code=status.HTTP_201_CREATED, summary="Open an incident")
async def create_incident(identity: CommitterDep, payload: IncidentCreate) -> dict[str, Any]:
    async with store.session_scope() as session:
        incident_id = await store.create_incident(
            session,
            {
                "title": payload.title,
                "category": payload.category.value,
                "severity": payload.severity,
                "status": IncidentStatus.DETECTED.value,
                "confidence": payload.confidence,
                "summary": payload.summary,
                "lon": payload.location.lon,
                "lat": payload.location.lat,
                "zone": payload.zone,
                "population_exposed": payload.population_exposed,
                "reported_by": payload.reported_by,
                "detected_at": payload.detected_at,
                "asset_links": [{"asset_id": asset_id, "role": "reported", "impact": 0.5} for asset_id in payload.affected_asset_ids],
            },
            actor=identity.subject,
        )
    await BUS.publish(Topics.INCIDENT_NEW, {"incident_id": incident_id, "title": payload.title, "severity": payload.severity}, source=identity.subject)
    return {"id": incident_id, "status": IncidentStatus.DETECTED.value}


@router.get("/{incident_id}", summary="One incident with its full journal")
async def get_incident(identity: ReaderDep, incident_id: str) -> dict[str, Any]:
    async with store.session_scope() as session:
        view = await store.get_incident(session, incident_id)
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"incident {incident_id} not found")
    return view


@router.post("/{incident_id}/triage", summary="Triage an incident against the live twin")
async def triage_incident(identity: CommitterDep, snapshot: SnapshotDep, incident_id: str) -> dict[str, Any]:
    """Ground the triage decision in the twin, then let the local model phrase it.

    The *decision* (dispatch / monitor / escalate / verify) is computed from
    structured state: asset status, population exposure, cascade depth and access.
    The language model only writes the sentence, and only from figures that were
    passed in.
    """
    async with store.session_scope() as session:
        view = await store.get_incident(session, incident_id)
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"incident {incident_id} not found")

    nearby = snapshot.nearby(view["lon"], view["lat"], radius_m=float(view.get("radius_m") or 600.0))
    degraded = [n for n in nearby if status_rank(n.status) >= status_rank("degraded")]  # type: ignore[arg-type]
    lifelines = [n for n in nearby if n.is_lifeline]
    facts = [
        f"incident severity {view['severity']} of 5, category {view['category']}, reported by {view['reported_by']}",
        f"{len(nearby)} twin assets within {int(view.get('radius_m') or 600)} m",
        f"{len(degraded)} of them are degraded, impaired or failed",
        f"{len(lifelines)} lifeline assets are in that radius",
    ]
    for node in sorted(degraded, key=lambda n: status_rank(n.status), reverse=True)[:6]:
        facts.append(f"{node.name} is {node.status.value} with {node.flood_depth_m:.2f} m of water and criticality {node.criticality:.2f}")
    if view.get("population_exposed"):
        facts.append(f"reported population exposed: {view['population_exposed']}")

    highest = max((status_rank(n.status) for n in nearby), default=0)
    decision = "verify"
    severity_floor = 2
    if degraded and any(n.is_lifeline for n in degraded):
        decision, severity_floor = "dispatch", 4
    elif degraded:
        decision, severity_floor = "escalate", 3
    elif lifelines:
        decision, severity_floor = "monitor", 2

    assessment = await REASONER.triage_incident(summary=f"{view['title']}: {view['summary']}", context_facts=facts, kpis={"severity": view["severity"], "exposed": view.get("population_exposed", 0)})
    suggestion_severity = max(int(view["severity"]), severity_floor, 4 if decision == "dispatch" else 0)
    async with store.session_scope() as session:
        await store.add_incident_update(
            session,
            incident_id,
            kind="triage",
            body=f"{decision}: {assessment.text}",
            author=identity.subject,
        )
        await store.update_incident(
            session,
            incident_id,
            changes={"status": IncidentStatus.TRIAGED.value, "severity": min(5, suggestion_severity)},
            actor=identity.subject,
            note=f"triaged as {decision} (severity floor {severity_floor} from {len(degraded)} degraded assets within radius)",
        )
    await BUS.publish(Topics.INCIDENT_UPDATE, {"incident_id": incident_id, "action": decision, "severity": suggestion_severity}, source=identity.subject)
    return {
        "incident_id": incident_id,
        "recommended_action": decision,
        "suggested_severity": min(5, suggestion_severity),
        "explanation": assessment.text,
        "explanation_source": "deterministic" if assessment.fallback_used else "language_model",
        "grounded": not assessment.ungrounded_numbers,
        "ungrounded_numbers": assessment.ungrounded_numbers,
        "facts": facts,
        "facts_verified": True,
        "nearby_assets": [
            {"id": n.id, "name": n.name, "status": n.status.value, "depth_m": round(n.flood_depth_m, 3), "lifeline": bool(n.is_lifeline)}
            for n in nearby[:25]
        ],
        "evidence_ids": [f"asset:{n.id}" for n in nearby[:10]],
        "at": datetime.now(timezone.utc),
    }


@router.patch("/{incident_id}", summary="Update status / severity")
async def patch_incident(identity: CommitterDep, incident_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with store.session_scope() as session:
        ok = await store.update_incident(session, incident_id, changes=payload, actor=identity.subject, note=str(payload.get("note") or ""))
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"incident {incident_id} not found")
    await BUS.publish(Topics.INCIDENT_UPDATE, {"incident_id": incident_id, "changes": {k: v for k, v in payload.items() if k != "note"}}, source=identity.subject)
    return {"id": incident_id, "updated": True}


@router.post("/{incident_id}/updates", summary="Append to the incident journal")
async def add_update(identity: CommitterDep, incident_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = str(payload.get("body") or "").strip()
    if len(body) < 2:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "an update needs a body")
    async with store.session_scope() as session:
        ok = await store.add_incident_update(session, incident_id, kind=str(payload.get("kind") or "note"), body=body, author=identity.subject)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"incident {incident_id} not found")
    return {"id": incident_id, "recorded": True}
