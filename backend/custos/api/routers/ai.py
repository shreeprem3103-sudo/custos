"""AI endpoints. Every answer carries its evidence, its freshness and its source.

The contract, enforced in one place (:mod:`custos.services.ai`): a language model
may only restate figures that came out of the engines. These endpoints expose
that property rather than hiding it — ``grounded``, ``evidence`` and
``explanation_source`` are part of the response, and the console renders them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from ...domain.enums import AssetStatus, status_rank
from ...domain.schemas import EvidenceItem, Explainability
from ...domain.enums import confidence_band, freshness_band
from ...engines.graph import DependencyGraph, MobilityRouter
from ...engines.kpi import explain_kpi_confidence
from ...services import store
from ...services.ai import REASONER, check_grounding, collect_numbers
from ...services.pipeline import ORCHESTRATOR
from ..deps import ReaderDep, SimulatorDep, SnapshotDep

router = APIRouter(prefix="/ai", tags=["ai"])


@router.get("/status", summary="Local model availability and usage")
async def ai_status(identity: ReaderDep) -> dict[str, Any]:
    health = await REASONER.client.health()
    return {
        "ollama": health,
        "usage": REASONER.snapshot_stats(),
        "policy": {
            "grounding_required": True,
            "on_rejection": "the deterministic engine text is returned instead, and the rejection is recorded",
            "offline_behaviour": "every capability has a deterministic fallback; the platform never waits on the model",
            "data_egress": "none: the model runs on-premise and prompts contain only structured state",
        },
    }


@router.get("/explain/{kpi}", summary="Explain one KPI: drivers, uncertainty, freshness, evidence")
async def explain_kpi(
    identity: ReaderDep,
    snapshot: SnapshotDep,
    kpi: str,
    simulation_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Explainability is computed, then phrased.

    The numbers, the drivers and the confidence all come from the engines; the
    model is used only to make the sentence readable, and only after the
    grounding check passes.
    """
    from ...domain.schemas import SimulationResult

    value: float | None = None
    drivers: list[str] = []
    evidence_ids: list[str] = []
    band: tuple[float, float] | None = None
    if simulation_id:
        async with store.session_scope() as session:
            stored = await store.get_simulation(session, simulation_id)
        if stored is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"simulation {simulation_id} not found")
        payload = stored.get("result") or {}
        result = SimulationResult(**payload)
        kpis = result.kpis
        uncertainty = result.uncertainty
        if not hasattr(kpis, kpi):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown KPI {kpi!r}")
        value = float(getattr(kpis, kpi))
        band = (float(getattr(uncertainty.p10, kpi)), float(getattr(uncertainty.p90, kpi)))
        weighted = sorted(
            ((field, float(abs(getattr(kpis, field)))) for field in kpis.model_fields if field != kpi),
            key=lambda item: item[1],
            reverse=True,
        )[:4]
        drivers = [f"{field} is {amount:g} in the same run" for field, amount in weighted if amount > 0]
        evidence_ids = list(result.explainability.evidence_ids)
        confidence = result.explainability.confidence
        freshness = result.explainability.data_freshness
    else:
        insight = explain_kpi_confidence(kpi, snapshot)
        value = float(insight.get("value") or 0.0)
        band = tuple(insight.get("band") or (0.0, 0.0))  # type: ignore[assignment]
        drivers = list(insight.get("drivers") or [])
        evidence_ids = list(insight.get("evidence_ids") or [])
        confidence = float(insight.get("confidence") or 0.5)
        freshness = freshness_band((datetime.now(timezone.utc) - snapshot.computed_at).total_seconds())

    assessment = await REASONER.explain_kpi(name=kpi, value=value, drivers=drivers, evidence=[*evidence_ids, snapshot.digest()])
    try:
        async with store.session_scope() as session:
            await store.write_audit(
                session,
                actor=identity.subject,
                role=identity.role.value,
                action="read",
                resource=f"ai.explain:{kpi}",
                detail={"simulation_id": simulation_id, "model": assessment.model, "fallback": assessment.fallback_used},
            )
    except Exception:  # noqa: BLE001 - audit failure must not hide the explanation
        pass
    return {
        "kpi": kpi,
        "value": round(value, 3),
        "band": {"p10": round(band[0], 3), "p90": round(band[1], 3)} if band else None,
        "drivers": drivers,
        "explanation": assessment.text,
        "explanation_source": "language_model" if not assessment.fallback_used else "deterministic",
        "grounded": not assessment.ungrounded_numbers,
        "ungrounded_numbers": assessment.ungrounded_numbers,
        "model": assessment.model,
        "latency_ms": assessment.latency_ms,
        "confidence": round(confidence, 3),
        "confidence_band": confidence_band(confidence).value,
        "data_freshness": str(freshness),
        "evidence_ids": evidence_ids,
        "assumptions": [
            "figures are taken from the same run being explained; the model may not introduce new ones",
        ],
        "at": datetime.now(timezone.utc),
    }


@router.post("/route-advice", summary="Corridor options to a facility, with the model's justification")
async def route_advice(
    identity: ReaderDep,
    snapshot: SnapshotDep,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Rank corridors to a destination from *live* twin state.

    The ranking is the mobility engine's; the sentence is the model's. If the
    model invents a travel time, the deterministic sentence is substituted and the
    response says so.
    """
    destination_id = str(payload.get("destination_id") or payload.get("asset_id") or "")
    destination = snapshot.nodes.get(destination_id)
    if destination is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"asset {destination_id} is not in the twin")
    origins = [str(origin) for origin in payload.get("origin_ids") or []]
    if not origins:
        responders = [n for n in snapshot.nodes.values() if n.asset_type.value in {"fire_station", "police_station"}]
        origins = [n.id for n in responders][:6]
    graph = DependencyGraph(snapshot)
    router_engine = MobilityRouter(graph)
    options: list[dict[str, Any]] = []
    for origin_id in origins:
        origin = snapshot.nodes.get(origin_id)
        if origin is None:
            continue
        route = router_engine.route(origin_id, destination_id, depths={n.id: n.flood_depth_m for n in snapshot.nodes.values()})
        if route is None:
            continue
        options.append(
            {
                "origin_id": origin_id,
                "name": f"{origin.name} → {destination.name}",
                "eta_min": float(route.eta_min),
                "max_depth_m": float(route.max_depth_m),
                "distance_m": float(getattr(route, "distance_m", 0.0)),
                "status": "reachable" if route.eta_min < 240 else "severed",
                "path": list(getattr(route, "path", []) or []),
            }
        )
    options.sort(key=lambda item: item["eta_min"])
    advice = await REASONER.choose_corridor(target=destination.name, options=options[:6])
    return {
        "destination": {"id": destination.id, "name": destination.name, "status": destination.status.value},
        "options": options[:8],
        "recommendation": advice.text,
        "recommendation_source": "language_model" if not advice.fallback_used else "deterministic",
        "grounded": not advice.ungrounded_numbers,
        "ungrounded_numbers": advice.ungrounded_numbers,
        "evidence_ids": [f"asset:{destination.id}", *[f"asset:{option['origin_id']}" for option in options[:5]]],
        "assumption": "travel times include a congestion and water-depth penalty; severed pairs are shown, never averaged away",
    }


@router.post("/verify-text", summary="Check a draft against structured data (grounding verifier)")
async def verify_text(identity: SimulatorDep, payload: dict[str, Any]) -> dict[str, Any]:
    """The same verifier the pipeline uses on model output, exposed for review.

    Useful for after-action reviews: paste a briefing, point it at a stored run,
    and see exactly which figures it could not have known.
    """
    text = str(payload.get("text") or "")
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "text is required")
    allowed: set[float] = set()
    if payload.get("simulation_id"):
        async with store.session_scope() as session:
            stored = await store.get_simulation(session, str(payload["simulation_id"]))
        if stored is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "simulation not found")
        allowed |= collect_numbers(stored.get("result") or {})
    if payload.get("reference"):
        allowed |= collect_numbers(payload["reference"])
    ungrounded = check_grounding(text, allowed)
    return {
        "grounded": not ungrounded,
        "ungrounded_numbers": ungrounded,
        "allowed_number_count": len(allowed),
        "note": "a text is grounded when every numeral in it can be traced to the structured record",
    }


@router.post("/workflow", summary="Investigate an incident: the reasoning chain, checkpointed")
async def workflow(identity: SimulatorDep, snapshot: SnapshotDep, payload: dict[str, Any]) -> dict[str, Any]:
    """A bounded, inspectable reasoning chain over live state.

    Not a chat interface. Each step reads structured state, produces a finding
    with evidence, and hands the next step a narrower question. The whole chain is
    returned so the commander can see where a conclusion came from — and it is
    reproducible, because every step is deterministic except the final phrasing.
    """
    started = datetime.now(timezone.utc)
    incident_id = payload.get("incident_id")
    focus = [str(asset) for asset in payload.get("focus_asset_ids") or []]
    steps: list[dict[str, Any]] = []
    citations: list[str] = []

    incident: dict[str, Any] | None = None
    if incident_id:
        async with store.session_scope() as session:
            incident = await store.get_incident(session, str(incident_id))
        if incident is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"incident {incident_id} not found")
        focus = focus or [link["asset_id"] for link in incident.get("asset_links", [])]
        steps.append(
            {
                "step": "load_incident",
                "detail": f"incident '{incident['title']}' with severity {incident['severity']} and {len(incident.get('timeline', []))} journal entries",
                "evidence": [f"incident:{incident['id']}"],
            }
        )
        citations.append(f"incident:{incident['id']}")

    graph = DependencyGraph(snapshot)
    insights = graph.insights().to_payload()
    spofs = {entry["asset_id"] for entry in insights.get("single_points_of_failure", [])}
    steps.append(
        {
            "step": "twin_insight",
            "detail": f"{len(snapshot.nodes)} assets, {len(snapshot.edges)} relationships, {len(spofs)} single points of failure",
            "evidence": [f"asset:{asset}" for asset in list(spofs)[:6]],
        }
    )

    degraded = [n for n in snapshot.nodes.values() if status_rank(n.status) >= status_rank(AssetStatus.DEGRADED)]
    horizon_minutes = int(payload.get("horizon_minutes") or 180)
    reach = []
    for asset_id in (focus or [n.id for n, _ in sorted(((n, n.criticality) for n in degraded), key=lambda item: item[1], reverse=True)[:3]]):
        node = snapshot.nodes.get(asset_id)
        if node is None:
            continue
        reach.append(
            {
                "asset_id": node.id,
                "name": node.name,
                "status": node.status.value,
                "depth_m": round(node.flood_depth_m, 3),
                "single_point_of_failure": node.id in spofs,
                "dependents": len(graph.dependents(node.id)) if hasattr(graph, "dependents") else len(snapshot.neighbours(node.id, hops=2)),
                "evidence": [f"asset:{node.id}"],
            }
        )
    steps.append(
        {
            "step": "dependency_reach",
            "detail": f"examined {len(reach)} asset(s) and their dependency neighbourhoods",
            "findings": reach,
            "evidence": [f"asset:{item['asset_id']}" for item in reach],
        }
    )

    exposed = 0
    for item in reach:
        zone = snapshot.nodes[item["asset_id"]].zone
        exposed += snapshot.zone_population(zone)
    steps.append({"step": "exposure", "detail": f"{exposed} people live in the wards of the examined assets", "evidence": [f"zone-of:{item['asset_id']}" for item in reach]})

    facts = [f"{item['name']} is {item['status']} with {item['depth_m']} m of water for {horizon_minutes} minutes" for item in reach][:8]
    question = str(payload.get("question") or "What should the duty officer do first, and what would change the answer?")
    narrative = (
        f"Investigation over {len(reach)} focus assets: "
        + ("; ".join(facts) if facts else "no degraded focus asset was supplied")
        + f". {exposed} residents live in their wards. Question: {question}"
    )
    assessment = await REASONER.choose_corridor(
        target=question,
        options=[
            {
                "name": fact,
                "eta_min": float(index + 1),
                "max_depth_m": float(item["depth_m"]),
                "status": item["status"],
            }
            for index, (fact, item) in enumerate(zip(facts, reach))
        ],
    )
    steps.append(
        {
            "step": "synthesis",
            "detail": assessment.text,
            "source": "language_model" if not assessment.fallback_used else "deterministic",
            "grounded": not assessment.ungrounded_numbers,
            "evidence": [f"asset:{item['asset_id']}" for item in reach],
        }
    )
    citations.extend(f"asset:{item['asset_id']}" for item in reach)
    runtime_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return {
        "thread_id": str(payload.get("thread_id") or f"thread-{incident_id or 'adhoc'}"),
        "stages": steps,
        "output": {
            "answer": assessment.text,
            "summary": narrative,
            "examined_assets": [item["asset_id"] for item in reach],
            "population_in_wards": exposed,
        },
        "citations": citations,
        "grounded": not assessment.ungrounded_numbers,
        "degraded": ORCHESTRATOR.status()["last_cycle"] is None if ORCHESTRATOR.status().get("last_cycle") is None else False,
        "runtime_ms": runtime_ms,
        "checkpoints": len(steps),
    }


@router.get("/anomalies/explain", summary="Explain the most recent anomalies")
async def explain_anomalies(identity: ReaderDep, snapshot: SnapshotDep, limit: Annotated[int, Query(ge=1, le=20)] = 5) -> dict[str, Any]:
    async with store.session_scope() as session:
        rows = await store.list_anomalies(session, limit=limit)
    explained: list[dict[str, Any]] = []
    for row in rows:
        result = await REASONER.assess_anomaly(
            metric=row["metric"],
            value=float(row["value"]),
            baseline=float(row["expected"]),
            spread=float((row.get("evidence") or {}).get("sigma") or 0.0),
            z=float(row["z_score"]),
            context=f"sensor {row['sensor_id']} in {snapshot.city_name or 'the city'}",
        )
        explained.append(
            {
                "anomaly_id": row["id"],
                "sensor_id": row["sensor_id"],
                "metric": row["metric"],
                "z_score": row["z_score"],
                "explanation": result.text,
                "source": "language_model" if not result.fallback_used else "deterministic",
                "grounded": not result.ungrounded_numbers,
            }
        )
    return {"count": len(explained), "anomalies": explained}
