"""What-if simulation and scenario-comparison endpoints.

The commander's core loop: pose a scenario, watch it run, compare it against
what is already happening, and commit an action. Three design commitments:

* **Runs survive a browser refresh.** The accepted response carries a poll URL
  and a stream URL; the run continues server-side and its progress events are
  persisted, so closing the laptop mid-run loses nothing.
* **Every run is reproducible.** The determinism token and the request are stored
  together; re-running the same request against the same twin returns the same
  numbers.
* **Nothing is committed from here without an audit entry.** Turning a
  recommendation into a task is a deliberate, attributed act.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status

from ...config import settings
from ...domain.enums import TaskStatus
from ...domain.kpi_meta import as_payload as kpi_metadata
from ...domain.schemas import (
    ComparisonRequest,
    ComparisonResult,
    Recommendation,
    SimulationRequest,
    SimulationResult,
    SimulationRunAccepted,
    SimulationSummary,
    TaskCreate,
    TaskOut,
)
from ...engines.simulation import ASSUMPTIONS
from ...realtime.bus import BUS, Topics
from ...services import store
from ...services.pipeline import ORCHESTRATOR
from ..deps import CommitterDep, ReaderDep, SimulatorDep, SnapshotDep

router = APIRouter(prefix="/simulations", tags=["simulations"])

#: in-flight runs, so a poll can report progress before the row is written
_RUNS: dict[str, dict[str, Any]] = {}


def _summary_from_row(row: dict[str, Any]) -> SimulationSummary:
    kpis = row.get("kpis") or {}
    return SimulationSummary(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        created_at=row["created_at"],
        completed_at=row.get("completed_at"),
        horizon_minutes=row.get("horizon_minutes") or 360,
        timestep_minutes=row.get("timestep_minutes") or 15,
        ensemble_size=row.get("ensemble_size") or 48,
        created_by=row.get("created_by") or "system",
        severity_delta=float(kpis.get("kpi_score") or 0.0),
        headline=(row.get("narrative") or "")[:280],
        progress=1.0 if row["status"] == "complete" else 0.0,
    )


@router.post("", response_model=SimulationRunAccepted, status_code=status.HTTP_202_ACCEPTED, summary="Queue a what-if run")
async def start_simulation(
    identity: SimulatorDep,
    background: BackgroundTasks,
    request: SimulationRequest,
) -> SimulationRunAccepted:
    run_id = f"sim_{uuid.uuid4().hex[:12]}"
    _RUNS[run_id] = {
        "id": run_id,
        "status": "running",
        "progress": 0.0,
        "message": "accepted",
        "request": request.model_dump(mode="json"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "identity": identity.subject,
    }

    def _progress(fraction: float, message: str, extra: dict[str, Any]) -> None:
        state = _RUNS.get(run_id)
        if state is not None:
            state["progress"] = round(float(fraction), 3)
            state["message"] = message
            state["detail"] = dict(extra or {})

    async def _run() -> None:
        try:
            result = await ORCHESTRATOR.simulate(request, persist=True, with_recommendations=True, progress=_progress, baseline_id=request.baseline_simulation_id)
            state = _RUNS.get(run_id)
            if state is not None:
                state.update({"status": "complete", "progress": 1.0, "message": "complete", "result_id": result.id})
        except Exception as exc:  # noqa: BLE001 - a failed run is reported, never swallowed
            state = _RUNS.get(run_id)
            if state is not None:
                state.update({"status": "failed", "message": f"{type(exc).__name__}: {exc}"[:300]})
            await BUS.publish(Topics.SIM_PROGRESS, {"simulation_id": run_id, "status": "failed", "error": str(exc)[:200]}, source="simulations")

    background.add_task(_run)
    await BUS.publish(
        Topics.SIM_PROGRESS,
        {"simulation_id": run_id, "status": "accepted", "name": request.name, "ensemble": request.ensemble_size},
        source=identity.subject,
    )
    return SimulationRunAccepted(id=run_id, status="running", poll_url=f"/api/v1/simulations/{run_id}/status", stream_url="/api/v1/stream")


@router.get("/{run_id}/status", summary="Poll a running simulation")
async def simulation_status(identity: ReaderDep, run_id: str) -> dict[str, Any]:
    state = _RUNS.get(run_id)
    if state is not None:
        return {
            "id": run_id,
            "status": state["status"],
            "progress": state.get("progress", 0.0),
            "message": state.get("message", ""),
            "result_id": state.get("result_id"),
            "detail": state.get("detail", {}),
        }
    async with store.session_scope() as session:
        stored = await store.get_simulation(session, run_id)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"simulation {run_id} not found")
    return {"id": run_id, "status": stored["status"], "progress": 1.0, "message": "complete", "result_id": run_id, "detail": {}}


async def _result_for(run_id: str) -> SimulationResult | None:
    state = _RUNS.get(run_id)
    result_id = state.get("result_id") if state else run_id
    async with store.session_scope() as session:
        stored = await store.get_simulation(session, str(result_id or run_id))
    if stored is None:
        return None
    payload = stored.get("result") or {}
    if not payload:
        return None
    payload["id"] = stored["id"]
    return SimulationResult(**payload)


@router.get("/{run_id}", response_model=SimulationResult, summary="Full simulation result")
async def get_simulation(identity: ReaderDep, run_id: str) -> SimulationResult:
    result = await _result_for(run_id)
    if result is None:
        state = _RUNS.get(run_id)
        if state is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, f"simulation {run_id} is {state['status']}: {state.get('message', '')}")
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"simulation {run_id} not found")
    return result


@router.get("", summary="Simulation history")
async def list_simulations(
    identity: ReaderDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
    name_contains: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    async with store.session_scope() as session:
        rows = await store.list_simulations(session, limit=limit, name_contains=name_contains)
    return {
        "count": len(rows),
        "running": [
            {"id": state["id"], "status": state["status"], "progress": state.get("progress", 0.0), "message": state.get("message", "")}
            for state in _RUNS.values()
            if state["status"] == "running"
        ],
        "simulations": [
            {
                "id": row["id"],
                "name": row["name"],
                "status": row["status"],
                "created_at": row["created_at"],
                "runtime_ms": row["runtime_ms"],
                "ensemble_size": row["ensemble_size"],
                "degraded": row["degraded"],
                "determinism_token": row["determinism_token"],
                "kpis": row["kpis"],
                "recommendation_count": row["recommendation_count"],
            }
            for row in rows
        ],
    }


@router.post("/compare", response_model=ComparisonResult, summary="Compare two scenarios KPI by KPI")
async def compare(identity: SimulatorDep, request: ComparisonRequest) -> ComparisonResult:
    try:
        return await ORCHESTRATOR.compare(request)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"scenario {exc.args[0]} not found") from exc


@router.post("/{run_id}/sensitivity", summary="Driver sensitivity for a scenario")
async def sensitivity(identity: SimulatorDep, run_id: str, drivers: Annotated[str | None, Query()] = None) -> dict[str, Any]:
    state = _RUNS.get(run_id)
    if state is None:
        async with store.session_scope() as session:
            stored = await store.get_simulation(session, run_id)
        if stored is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"simulation {run_id} not found")
        request = SimulationRequest(**stored["result"]["request"])
    else:
        request = SimulationRequest(**state["request"])
    request.ensemble_size = max(4, min(12, request.ensemble_size // 6))
    items = await ORCHESTRATOR.sensitivity(request, drivers=[d.strip() for d in drivers.split(",")] if drivers else None)
    return {
        "simulation_id": run_id,
        "drivers": [item.model_dump(mode="json") for item in items],
        "method": "one-at-a-time perturbation across the ensemble; ranked by swing in the composite severity score",
    }


@router.post("/{run_id}/recommendations/{recommendation_id}/commit", response_model=TaskOut, summary="Turn a recommendation into a task")
async def commit_recommendation(
    identity: CommitterDep,
    run_id: str,
    recommendation_id: str,
    payload: dict[str, Any] | None = None,
) -> TaskOut:
    """Commit an action: creates a task, reserves resources, writes the audit trail."""
    result = await _result_for(run_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"simulation {run_id} not found")
    recommendation: Recommendation | None = next(
        (rec for rec in result.recommendations if rec.id == recommendation_id or str(rec.rank) == recommendation_id), None
    )
    if recommendation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"recommendation {recommendation_id} is not part of run {run_id}")
    body = payload or {}
    async with store.session_scope() as session:
        task_id = await store.create_task(
            session,
            {
                "title": str(body.get("title") or f"{recommendation.action} ({recommendation.window})"),
                "description": str(body.get("description") or f"{recommendation.rationale}\n\nCounterfactual: {recommendation.counterfactual}"),
                "priority": int(body.get("priority") or max(1, 6 - recommendation.urgency)),
                "status": TaskStatus.DISPATCHED.value if body.get("assigned_to") else TaskStatus.PROPOSED.value,
                "simulation_id": result.id,
                "recommendation_id": recommendation.id,
                "assigned_to": body.get("assigned_to"),
                "target_asset_id": recommendation.target_id,
                "resource_requirements": dict(recommendation.resources_required or body.get("resource_requirements") or {}),
            },
            actor=identity.subject,
        )
        if body.get("resources"):
            await store.assign_resources(session, task_id=task_id, assignments=body["resources"], actor=identity.subject)
        stored = await store.list_tasks(session, limit=1)
    await BUS.publish(
        Topics.TASK,
        {"task_id": task_id, "recommendation_id": recommendation.id, "simulation_id": result.id, "action": recommendation.action},
        source=identity.subject,
    )
    row = next((item for item in stored if item["id"] == task_id), None)
    if row is None:
        row = {
            "id": task_id,
            "title": recommendation.action,
            "description": recommendation.rationale,
            "status": TaskStatus.PROPOSED.value,
            "priority": 3,
            "due_at": None,
            "incident_id": None,
            "simulation_id": result.id,
            "recommendation_id": recommendation.id,
            "assigned_to": body.get("assigned_to"),
            "target_asset_id": recommendation.target_id,
            "resource_requirements": recommendation.resources_required,
            "created_by": identity.subject,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "history": [],
        }
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


@router.get("/meta/assumptions", summary="Every model assumption the UI is allowed to show")
async def assumptions(identity: ReaderDep) -> dict[str, Any]:
    """Publish the assumption register.

    A commander is entitled to know what the model believes. Publishing it also
    keeps us honest: an assumption that is written down is one we will be asked
    to defend, and one we can revise when the evidence changes.
    """
    return {
        "hydrology": [
            "Lumped mass balance per ward; not a 2-D hydrodynamic solution.",
            "Ponding is concentrated into 28% of the ward area rather than spread evenly - a ward is not a bathtub.",
            "Ward drainage is sized from its design rainfall and pumped capacity; surcharged inlets cannot take more water whatever the depth.",
            "Tide is astronomical (semi-diurnal, 12.42 h); surge is an operator-declared boundary condition.",
        ],
        "cascade": [
            "Transmission = kind weight x attenuation x criticality factor x diversity factor.",
            "Buffered assets fail after their declared autonomy expires, not at first loss of supply.",
            "Cascade stops at the horizon; recovery is reported, not simulated.",
        ],
        "exposure": [
            "22% of an affected population seeks public shelter (district planning ratio).",
            "Rescue demand rises once impact passes 45% of a ward.",
            "Equity is only reported above a 2% affected-population floor, below which it is noise.",
        ],
        "interventions": ASSUMPTIONS,
        "simulation": [
            f"Default {settings.sim_ensemble_size} members; counterfactual re-simulation uses 4-8 members for latency.",
            "Recommendations are ranked by measured KPI change, never by opinion.",
        ],
    }


@router.get("/meta/kpis", summary="KPI dictionary: label, unit and direction of good")
async def kpi_dictionary(identity: ReaderDep) -> dict[str, Any]:
    return {"kpis": kpi_metadata()}
