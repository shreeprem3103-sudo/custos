"""Twin, graph and asset read endpoints (the console's map data)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from ...domain.enums import AssetStatus, AssetType, RelationKind, status_rank, worst
from ...domain.schemas import AssetOut, GeoPoint, GraphEdgeOut, GraphNodeOut, GraphOut, Provenance, RelationshipOut
from ...domain.twin import AssetState, TwinSnapshot
from ...engines.graph import DependencyGraph
from ...engines.kpi import ExposureModel
from ...fusion import repository
from ...infrastructure import db
from ...observability import METRICS
from ...services import store
from ..deps import CommitterDep, ReaderDep, SnapshotDep

router = APIRouter(prefix="/twin", tags=["twin"])


def _asset_payload(node: AssetState, *, updated_at: datetime | None = None, provenance: Provenance | None = None) -> AssetOut:
    return AssetOut(
        id=node.id,
        name=node.name,
        asset_type=node.asset_type,
        subtype=node.subtype,
        criticality=node.criticality,
        elevation_m=node.elevation_m,
        geometry=node.geometry or {"type": "Point", "coordinates": [node.lon, node.lat]},
        centroid=GeoPoint(lon=node.lon, lat=node.lat),
        operator=node.operator,
        zone=node.zone,
        capacity=dict(node.capacity),
        attributes=dict(node.attributes),
        status=node.status,
        flood_depth_m=round(float(node.flood_depth_m), 3),
        power_available=node.power_available,
        access_available=node.access_available,
        service_level=round(float(node.service_level), 3),
        exposure_score=round(float(node.exposure_score), 3),
        provenance=provenance,
        updated_at=updated_at or datetime.now(timezone.utc),
    )


@router.get("", summary="Full twin state: assets, zones, sensors, insights")
async def get_twin(
    identity: ReaderDep,
    snapshot: SnapshotDep,
    response: Response,
    include: Annotated[str, Query(description="comma list: assets,zones,sensors,insights,exposure")] = "assets,zones,sensors,insights",
) -> dict[str, Any]:
    wanted = {part.strip() for part in include.split(",") if part.strip()}
    payload: dict[str, Any] = {
        "city": {"id": snapshot.city_id, "name": snapshot.city_name or "Kochi (Ernakulam)"},
        "computed_at": snapshot.computed_at,
        "degraded": snapshot.degraded,
        "sources": list(snapshot.sources),
        "digest": snapshot.digest(),
        "stats": snapshot.stats(),
    }
    if "assets" in wanted:
        payload["assets"] = [_asset_payload(node).model_dump(mode="json") for node in snapshot.nodes.values()]
    if "zones" in wanted:
        payload["zones"] = [
            {
                "id": zone.id,
                "name": zone.name,
                "centroid": {"lon": zone.lon, "lat": zone.lat},
                "population": zone.population,
                "vulnerability_index": zone.vulnerability_index,
                "children_pct": zone.children_pct,
                "elderly_pct": zone.elderly_pct,
                "population_density": zone.population_density,
                "mean_elevation_m": zone.mean_elevation_m,
                "flood_depth_m": round(
                    max((n.flood_depth_m for n in snapshot.nodes.values() if n.zone == zone.id), default=0.0), 3
                ),
                "assets": sum(1 for n in snapshot.nodes.values() if n.zone == zone.id),
            }
            for zone in snapshot.zones.values()
        ]
    if "sensors" in wanted:
        payload["sensors"] = [
            {
                "id": sensor.id,
                "name": sensor.name,
                "metric": sensor.metric,
                "unit": sensor.unit,
                "asset_id": sensor.asset_id,
                "zone": sensor.zone,
                "centroid": {"lon": sensor.lon, "lat": sensor.lat},
                "last_value": sensor.last_value,
                "last_observed_at": sensor.last_observed_at,
                "age_seconds": sensor.age_seconds,
                "is_active": sensor.is_active,
                "baseline_median": sensor.baseline_median,
            }
            for sensor in snapshot.sensors.values()
        ]
    if "insights" in wanted:
        payload["insights"] = DependencyGraph(snapshot).insights().to_payload()
    if "exposure" in wanted:
        exposure = ExposureModel(snapshot).evaluate(depths={})
        payload["exposure"] = {
            "zone_impact": {zone_id: round(value, 3) for zone_id, value in exposure.zone_impact.items()},
            "shelter_demand": exposure.shelter_demand,
            "shelter_capacity_available": exposure.shelter_capacity_available,
            "unplaced_at_shelters": exposure.unplaced_at_shelters,
        }
    payload["fusion"] = snapshot.attributes.get("fusion_quality", {})
    payload["prediction"] = snapshot.attributes.get("prediction", {})
    payload["stage_outputs"] = {
        "fusion_metrics": {k: v for k, v in snapshot.attributes.get("fusion", {}).items() if k.startswith("metric:")},
    }
    METRICS.inc("api.twin.read")
    return payload


@router.get("/graph", response_model=GraphOut, summary="Dependency graph for cascade visualisation")
async def get_graph(
    identity: ReaderDep,
    snapshot: SnapshotDep,
    kinds: Annotated[str | None, Query(description="comma list of relation kinds")] = None,
    min_criticality: Annotated[float, Query(ge=0.0, le=1.0)] = 0.0,
    limit: Annotated[int, Query(ge=10, le=5000)] = 1200,
) -> GraphOut:
    allowed: set[RelationKind] | None = None
    if kinds:
        allowed = set()
        for raw in kinds.split(","):
            try:
                allowed.add(RelationKind(raw.strip()))
            except ValueError:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown relation kind {raw!r}")
    graph = DependencyGraph(snapshot)
    insights = graph.insights().to_payload()
    spofs = {entry["asset_id"] for entry in insights.get("single_points_of_failure", [])}
    degree: dict[str, int] = {}
    for edge in snapshot.edges:
        degree[edge.source_id] = degree.get(edge.source_id, 0) + 1
        degree[edge.target_id] = degree.get(edge.target_id, 0) + 1
    nodes = [
        GraphNodeOut(
            id=node.id,
            name=node.name,
            asset_type=node.asset_type,
            status=node.status,
            criticality=node.criticality,
            centroid=GeoPoint(lon=node.lon, lat=node.lat),
            degree=degree.get(node.id, 0),
            betweenness=float(next((b["score"] for b in insights.get("betweenness", []) if b.get("asset_id") == node.id), 0.0)),
            single_point_of_failure=node.id in spofs,
            zone=node.zone,
        )
        for node in snapshot.nodes.values()
        if node.criticality >= min_criticality
    ][:limit]
    edges = [
        GraphEdgeOut(**{
            "id": edge.id,
            "source_id": edge.source_id,
            "target_id": edge.target_id,
            "kind": edge.kind,
            "criticality": edge.criticality,
            "weight": edge.weight,
            "lead_time_min": edge.lead_time_min,
            "failure_mode": edge.failure_mode,
            "attributes": dict(edge.attributes),
            "geometry": edge.geometry,
        })
        for edge in snapshot.edges
        if (allowed is None or edge.kind in allowed) and edge.source_id in {n.id for n in nodes} and edge.target_id in {n.id for n in nodes}
    ]
    return GraphOut(nodes=nodes, edges=edges, computed_at=snapshot.computed_at, cascade_paths_sample=insights.get("top_paths", [])[:20])


@router.get("/assets", summary="Asset list with filters")
async def list_assets(
    identity: ReaderDep,
    snapshot: SnapshotDep,
    asset_type: Annotated[AssetType | None, Query()] = None,
    min_status: Annotated[AssetStatus | None, Query(description="only assets at or worse than this status")] = None,
    zone: Annotated[str | None, Query()] = None,
    flooded_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> dict[str, Any]:
    nodes = list(snapshot.nodes.values())
    if asset_type is not None:
        nodes = [n for n in nodes if n.asset_type is asset_type]
    if zone:
        nodes = [n for n in nodes if n.zone == zone]
    if flooded_only:
        nodes = [n for n in nodes if n.flood_depth_m > 0.05]
    if min_status is not None:
        nodes = [n for n in nodes if status_rank(n.status) >= status_rank(min_status)]
    nodes.sort(key=lambda n: (status_rank(n.status), n.flood_depth_m, n.criticality), reverse=True)
    return {"count": len(nodes), "assets": [_asset_payload(node).model_dump(mode="json") for node in nodes[:limit]]}


@router.get("/assets/{asset_id}", summary="One asset with its local graph neighbourhood")
async def get_asset(identity: ReaderDep, snapshot: SnapshotDep, asset_id: str) -> dict[str, Any]:
    node = snapshot.nodes.get(asset_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"asset {asset_id} is not in the twin")
    incoming = snapshot.in_edges(asset_id)
    outgoing = snapshot.out_edges(asset_id)
    return {
        "asset": _asset_payload(node).model_dump(mode="json"),
        "depends_on": [
            {
                "id": edge.source_id,
                "name": snapshot.nodes[edge.source_id].name,
                "kind": edge.kind,
                "status": snapshot.nodes[edge.source_id].status,
                "lead_time_min": edge.lead_time_min,
                "failure_mode": edge.failure_mode,
            }
            for edge in incoming
            if edge.source_id in snapshot.nodes
        ],
        "supports": [
            {
                "id": edge.target_id,
                "name": snapshot.nodes[edge.target_id].name,
                "kind": edge.kind,
                "status": snapshot.nodes[edge.target_id].status,
            }
            for edge in outgoing
            if edge.target_id in snapshot.nodes
        ],
        "cascade_reach": sorted(snapshot.neighbours(asset_id, hops=3)),
    }


@router.patch("/assets/{asset_id}/state", summary="Operator override of an asset state (audited)")
async def patch_asset_state(
    identity: CommitterDep,
    snapshot: SnapshotDep,
    asset_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Manual override: the operator sees something the sensors do not.

    Writes both the *state* and the *reason*, so the twin can explain later why
    an asset was marked failed when no model said so. The override is attributed
    to the caller and chained into the audit log.
    """
    node = snapshot.nodes.get(asset_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"asset {asset_id} is not in the twin")
    new_status: AssetStatus | None = None
    if payload.get("status"):
        try:
            new_status = AssetStatus(str(payload["status"]))
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown status {payload['status']!r}")
    depth = payload.get("flood_depth_m")
    note = str(payload.get("note") or "")
    if new_status is None and depth is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "provide status and/or flood_depth_m")
    try:
        async with store.session_scope() as session:
            await repository.touch_asset_state(
                session,
                asset_id,
                status=new_status,
                flood_depth_m=float(depth) if depth is not None else None,
                source=f"operator:{identity.subject}",
                confidence=float(payload.get("confidence") or 0.9),
            )
            await store.write_audit(
                session,
                actor=identity.subject,
                role=identity.role.value,
                action="asset.override",
                resource=asset_id,
                outcome="ok",
                detail={"status": new_status.value if new_status else None, "flood_depth_m": depth, "note": note},
            )
    except Exception as exc:  # noqa: BLE001 - a storage failure must not lose the operator's intent silently
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"override could not be recorded: {str(exc)[:160]}") from exc
    await repository.get_snapshot(force=True)
    return {"asset_id": asset_id, "status": new_status.value if new_status else node.status.value, "note": note, "recorded_by": identity.subject}


@router.get("/zones/{zone_id}/risk", summary="Ward-level risk breakdown used by the equity panel")
async def zone_risk(identity: ReaderDep, snapshot: SnapshotDep, zone_id: str) -> dict[str, Any]:
    zone = snapshot.zones.get(zone_id)
    if zone is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"zone {zone_id} is not in the twin")
    assets = [n for n in snapshot.nodes.values() if n.zone == zone_id]
    flooded = [n for n in assets if n.flood_depth_m > 0.05]
    exposure = ExposureModel(snapshot).evaluate(depths={})
    return {
        "zone": {"id": zone.id, "name": zone.name, "population": zone.population, "vulnerability_index": zone.vulnerability_index},
        "impact": round(exposure.zone_impact.get(zone_id, 0.0), 3),
        "assets": len(assets),
        "flooded_assets": len(flooded),
        "worst_depth_m": round(max((n.flood_depth_m for n in assets), default=0.0), 3),
        "lifelines_at_risk": [
            {"id": n.id, "name": n.name, "type": n.asset_type.value, "status": n.status.value, "depth_m": round(n.flood_depth_m, 3)}
            for n in assets
            if n.is_lifeline and (n.flood_depth_m > 0.05 or status_rank(n.status) >= status_rank(AssetStatus.DEGRADED))
        ],
    }
