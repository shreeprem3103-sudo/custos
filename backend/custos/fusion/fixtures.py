"""In-memory twin construction straight from the city dataset.

Used by the seed writer, by unit tests and by the offline "war game" CLI, so
that the engines can be exercised without a database in the loop.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from ..config import settings
from ..domain.enums import AssetStatus, AssetType, FailureMode, RelationKind
from ..domain.twin import AssetState, Edge, SensorState, TwinSnapshot, ZoneState
from . import city_kochi as city


def snapshot_from_city_data(
    *,
    statuses: Mapping[str, AssetStatus] | None = None,
    depths: Mapping[str, float] | None = None,
    now: datetime | None = None,
) -> TwinSnapshot:
    statuses = statuses or {}
    depths = depths or {}
    now = now or datetime.now(timezone.utc)

    nodes: dict[str, AssetState] = {}
    for asset in city.ASSETS:
        asset_id = asset["id"]
        nodes[asset_id] = AssetState(
            id=asset_id,
            name=asset["name"],
            asset_type=asset["asset_type"],
            criticality=float(asset["criticality"]),
            lon=float(asset["lon"]),
            lat=float(asset["lat"]),
            elevation_m=asset.get("elevation_m"),
            design_rainfall=(asset.get("attributes") or {}).get("design_rainfall_mm_h"),
            status=statuses.get(asset_id, AssetStatus.OK),
            flood_depth_m=float(depths.get(asset_id, 0.0)),
            zone=asset.get("zone"),
            operator=asset.get("operator"),
            subtype=asset.get("subtype"),
            geometry=asset.get("geometry") or {},
            capacity=dict(asset.get("capacity") or {}),
            attributes=dict(asset.get("attributes") or {}),
            state_observed_at=now - timedelta(minutes=3),
            state_source="seed",
            state_confidence=0.95,
        )

    edges = [
        Edge(
            id=f"seed_{index:04d}",
            source_id=row["source_id"],
            target_id=row["target_id"],
            kind=RelationKind(row["kind"]),
            criticality=float(row["criticality"]),
            lead_time_min=int(row["lead_time_min"]),
            attenuation=float(row["attenuation"]),
            failure_mode=FailureMode(row["failure_mode"]),
            recovery_hours=float(row["recovery_hours"]),
            redundancy_group=row.get("redundancy_group"),
            attributes=dict(row.get("attributes") or {}),
        )
        for index, row in enumerate(city.RELATIONSHIPS)
    ]

    zones = {
        z["id"]: ZoneState(
            id=z["id"],
            name=z["name"],
            lon=float(z["lon"]),
            lat=float(z["lat"]),
            population=int(z["population"]),
            vulnerability_index=float(z["vulnerability_index"]),
            children_pct=float(z["children_pct"]),
            elderly_pct=float(z["elderly_pct"]),
            population_density=float(z.get("population_density") or 0.0),
            mean_elevation_m=float(z["mean_elevation_m"]),
        )
        for z in city.ZONES
    }

    sensors = {
        s["id"]: SensorState(
            id=s["id"],
            name=s["name"],
            metric=s["metric"],
            unit=s["unit"],
            lon=float(s["lon"]),
            lat=float(s["lat"]),
            asset_id=s.get("asset_id"),
            zone=s.get("zone"),
            expected_min=s.get("expected_min"),
            expected_max=s.get("expected_max"),
            last_value=None,
            last_observed_at=None,
        )
        for s in city.SENSORS
    }

    return TwinSnapshot(
        nodes=nodes,
        edges=edges,
        zones=zones,
        sensors=sensors,
        city_id=settings.city_id,
        city_name=settings.city_name,
        sources=("fixture",),
    )


def severity_seeds_from_snapshot(snapshot: TwinSnapshot) -> dict[str, float]:
    """Derive cascade seeds from the *current* twin state.

    Only genuinely degraded objects seed a propagation: a healthy asset is never
    a source, otherwise every simulation would produce phantom cascades.
    """
    seeds: dict[str, float] = {}
    for state in snapshot.nodes.values():
        if state.status in (AssetStatus.OK, AssetStatus.WATCH):
            continue
        base = {
            AssetStatus.DEGRADED: 0.35,
            AssetStatus.IMPAIRED: 0.65,
            AssetStatus.FAILED: 1.0,
            AssetStatus.UNKNOWN: 0.25,
        }.get(state.status, 0.0)
        depth = state.flood_depth_m or 0.0
        threshold = float(state.capacity.get("flood_threshold_m") or 0.3)
        if depth > 0:
            base = min(1.0, base + min(0.35, depth / max(0.15, threshold) * 0.2))
        seeds[state.id] = round(base, 3)
    return seeds
