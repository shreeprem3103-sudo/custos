"""The "What Happens If?" engine.

How a scenario is computed
--------------------------
A what-if run is an **ensemble**, not a single deterministic pass. For each of
``ensemble_size`` members the engine perturbs the inputs (rainfall intensity,
tide/surge, drainage capacity, pump availability, hazard timing), then walks the
horizon in fixed timesteps:

    forcing -> hydrology (depths) -> asset state -> dependency cascade
            -> mobility/routing -> KPI -> timeline frame

Every member therefore produces its own *plausible* crisis trajectory. The
result reports p10/p50/p90 for each KPI, and the command console renders the
band - because a single-point prediction in a flood is a liability, not a
feature.

Determinism
-----------
Each member is seeded from ``scenario_seed = request.random_seed or hash(name)``
so a run is exactly reproducible: same inputs, same ensemble, same numbers. The
``determinism_token`` (seed + twin digest) is stored with the result, which is
what makes a post-event review possible ("this is precisely what the system
knew, and precisely what it computed, at 21:05").
"""

from __future__ import annotations

import hashlib
import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from ..config import settings
from ..domain.enums import (
    AssetStatus,
    AssetType,
    ConfidenceBand,
    HazardType,
    InterventionType,
    RelationKind,
    confidence_band,
    freshness_band,
    status_rank,
    worst,
)
from ..domain.schemas import (
    CascadeImpact,
    DriverSensitivity,
    Explainability,
    FacilityState,
    HazardSpec,
    InterventionSpec,
    KPIUncertainty,
    KPISet,
    MobilityState,
    Recommendation,
    RoadState,
    ShelterState,
    SimulationRequest,
    SimulationResult,
    TimelineFrame,
)
from ..domain.twin import AssetState, TwinSnapshot
from ..fusion.normalize import haversine_m
from ..observability import METRICS, get_logger
from .graph import DependencyGraph, Effect, MobilityRouter, summarise_cascade
from .hydro import ASSUMPTIONS, HydroModel, Forcing
from .kpi import ExposureModel, aggregate_kpis, diff_kpis, kpis_from_state, score_kpis

log = get_logger("engines.simulation")

#: Depth bands (degraded, impaired, failed) in metres at the asset.
#: These encode *engineering* reality, not convenience: a substation sits on a
#: plinth, a hospital has raised floors and keeps operating on generator power
#: long after a shop closes, and an airport apron tolerates water that would
#: stop a car. Getting these wrong makes the platform cry wolf on every shower.
FACILITY_DEPTH_BANDS: dict[AssetType, tuple[float, float, float]] = {
    AssetType.HOSPITAL: (0.10, 0.25, 0.55),
    AssetType.PUMP_STATION: (0.15, 0.30, 0.50),
    AssetType.SUBSTATION: (0.25, 0.55, 0.95),
    AssetType.WATER_TREATMENT: (0.20, 0.45, 0.80),
    AssetType.TELECOM_TOWER: (0.25, 0.55, 0.90),
    AssetType.SHELTER: (0.12, 0.25, 0.45),
    AssetType.METRO_STATION: (0.15, 0.35, 0.65),
    AssetType.FIRE_STATION: (0.15, 0.32, 0.60),
    AssetType.POLICE_STATION: (0.15, 0.32, 0.60),
    AssetType.AIRPORT: (0.30, 0.60, 1.20),
    AssetType.PORT: (0.30, 0.60, 1.10),
    AssetType.REFINERY: (0.35, 0.70, 1.30),
    AssetType.WASTE_PLANT: (0.30, 0.60, 1.00),
}

#: service level retained at each band (1.0 = fully operational)
SERVICE_LEVEL_BY_BAND = (1.0, 0.7, 0.5, 0.0)


ProgressCallback = Callable[[float, str, dict[str, Any]], None]


def _as_enum(value: Any, enum_cls: type) -> Any:
    """API models serialise enums to strings (``use_enum_values``); engines want
    the enum. This one helper keeps that impedance mismatch out of the engines."""
    return value if isinstance(value, enum_cls) else enum_cls(value)


# --------------------------------------------------------------------------- #
# scenario normalization
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ResolvedHazard:
    spec: HazardSpec
    label: str
    lon: float
    lat: float
    radius_m: float
    resource_id: str | None = None
    zones: tuple[str, ...] = ()


@dataclass
class ScenarioPlan:
    request: SimulationRequest
    hazards: list[ResolvedHazard] = field(default_factory=list)
    rain_schedule: dict[int, float] = field(default_factory=dict)
    surge_schedule: dict[int, float] = field(default_factory=dict)
    release_schedule: dict[int, dict[str, float]] = field(default_factory=dict)
    injection_schedule: dict[int, dict[str, float]] = field(default_factory=dict)
    facility_evacuations: list[tuple[int, str, float]] = field(default_factory=list)
    shelter_capacity_additions: list[tuple[int, str, float]] = field(default_factory=list)
    pre_positioned: list[tuple[int, str]] = field(default_factory=list)
    contingency_routes: list[tuple[int, str, int]] = field(default_factory=list)
    power_switchovers: list[tuple[int, str]] = field(default_factory=list)
    alerts: list[int] = field(default_factory=list)
    prepared_pumps: set[str] = field(default_factory=set)
    closed_roads_by_step: dict[int, set[str]] = field(default_factory=dict)
    pump_deployments: list[tuple[int, str, float, float]] = field(default_factory=list)
    status_overrides: dict[str, AssetStatus] = field(default_factory=dict)
    seed: int = 0

    @property
    def steps(self) -> int:
        return max(1, self.request.horizon_minutes // self.request.timestep_minutes)


def resolve_scenario(snapshot: TwinSnapshot, request: SimulationRequest) -> ScenarioPlan:
    """Translate a request (asset names, coordinates, timings) into engine terms."""
    plan = ScenarioPlan(request=request)
    plan.seed = request.random_seed if request.random_seed is not None else int(
        hashlib.sha256(request.name.encode("utf-8")).hexdigest()[:8], 16
    )
    dt = request.timestep_minutes

    for spec in request.hazards:
        hazard_kind = _as_enum(spec.hazard_type, HazardType)
        asset = snapshot.nodes.get(spec.asset_id) if spec.asset_id else None
        if asset is not None:
            lon, lat = asset.lon, asset.lat
        elif spec.location is not None:
            lon, lat = spec.location.lon, spec.location.lat
        else:  # pragma: no cover - schema guarantees one of the two
            continue
        zones = tuple(
            zone.id
            for zone in snapshot.zones.values()
            if haversine_m(lon, lat, zone.lon, zone.lat) <= spec.radius_m
        )
        resolved = ResolvedHazard(
            spec=spec,
            label=spec.label or (asset.name if asset else f"{hazard_kind.value} @ {lon:.3f},{lat:.3f}"),
            lon=lon,
            lat=lat,
            radius_m=spec.radius_m,
            resource_id=spec.asset_id,
            zones=zones,
        )
        plan.hazards.append(resolved)

        start_step = int(spec.start_min // dt)
        end_step = int(math.ceil((spec.start_min + spec.duration_min) / dt))
        for step in range(start_step, min(end_step, plan.steps) + 1):
            relative = (step * dt - spec.start_min) / max(1.0, spec.duration_min)
            # a storm cell rises, peaks and decays - a flat block would understate
            # the peak and overstate the recovery
            if relative <= 0.15:
                shape = relative / 0.15
            elif relative >= 0.75:
                shape = max(0.0, (1.0 - relative) / 0.25)
            else:
                shape = 1.0
            shape = max(0.0, min(1.0, shape))
            if hazard_kind is HazardType.RAINFALL_BURST:
                intensity = (spec.intensity_mm_h or 0.0) * shape * spec.probability
                plan.rain_schedule[step] = plan.rain_schedule.get(step, 0.0) + intensity
            elif hazard_kind is HazardType.TIDAL_SURGE:
                plan.surge_schedule[step] = max(plan.surge_schedule.get(step, 0.0), (spec.surge_m or 0.0) * shape)
            elif hazard_kind is HazardType.DAM_RELEASE:
                target = spec.asset_id or (plan.hazards[-1].resource_id if plan.hazards else None)
                if target:
                    plan.release_schedule.setdefault(step, {})
                    plan.release_schedule[step][target] = plan.release_schedule[step].get(target, 0.0) + (
                        spec.discharge_m3s or 0.0
                    ) * shape
            elif spec.hazard_type in {HazardType.FLOOD_INJECTION, HazardType.LEVEE_BREACH, HazardType.DRAINAGE_BLOCKAGE}:
                centre = spec.asset_id
                if centre is None:
                    # bind a free-form point to its nearest twin object
                    centre = min(
                        snapshot.nodes.values(),
                        key=lambda n: haversine_m(lon, lat, n.lon, n.lat),
                    ).id
                depth = spec.depth_m if spec.hazard_type is not HazardType.DRAINAGE_BLOCKAGE else None
                if depth is not None:
                    plan.injection_schedule.setdefault(step, {})
                    for node in snapshot.nodes.values():
                        distance = haversine_m(lon, lat, node.lon, node.lat)
                        if distance > spec.radius_m:
                            continue
                        falloff = max(0.0, 1.0 - distance / spec.radius_m)
                        value = depth * falloff * shape
                        if value > plan.injection_schedule[step].get(node.id, 0.0):
                            plan.injection_schedule[step][node.id] = value
            elif hazard_kind is HazardType.PUMP_FAILURE and spec.asset_id:
                # a failed pump is an asset-status override, not a hydraulic one
                plan.status_overrides[spec.asset_id] = AssetStatus.FAILED

    for intervention in request.interventions:
        kind = _as_enum(intervention.intervention_type, InterventionType)
        target_id = intervention.destination_id or intervention.asset_id or intervention.facility_id
        target_node = snapshot.nodes.get(str(target_id)) if target_id else None
        if (
            kind is InterventionType.PRE_POSITION_RESOURCE
            and target_node is not None
            and target_node.asset_type is AssetType.PUMP_STATION
        ):
            plan.prepared_pumps.add(target_node.id)
        start_step = int(intervention.start_min // dt)
        duration_steps = (
            plan.steps - start_step
            if intervention.duration_min <= 0
            else int(math.ceil(intervention.duration_min / dt))
        )
        for step in range(start_step, min(plan.steps, start_step + duration_steps) + 1):
            if kind is InterventionType.DEPLOY_PUMP:
                target = intervention.asset_id or intervention.destination_id or ""
                plan.pump_deployments.append(
                    (
                        step,
                        target,
                        float(intervention.capacity_m3s or 0.5),
                        float(intervention.quantity or 1.0),
                    )
                )
            elif kind is InterventionType.CLOSE_ROAD:
                plan.closed_roads_by_step.setdefault(step, set()).add(intervention.road_id or intervention.asset_id or "")
            elif kind is InterventionType.EVACUATE_FACILITY and intervention.facility_id:
                plan.facility_evacuations.append((step, intervention.facility_id, float(intervention.quantity or 1.0)))
            elif kind is InterventionType.OPEN_SHELTER and (intervention.facility_id or intervention.asset_id):
                plan.shelter_capacity_additions.append(
                    (step, str(intervention.facility_id or intervention.asset_id), float(intervention.quantity or 500.0))
                )
            elif kind is InterventionType.PRE_POSITION_RESOURCE and (intervention.destination_id or intervention.asset_id):
                plan.pre_positioned.append((step, str(intervention.destination_id or intervention.asset_id)))
            elif kind is InterventionType.REROUTE_TRAFFIC and (intervention.destination_id or intervention.asset_id):
                plan.contingency_routes.append(
                    (step, str(intervention.destination_id or intervention.asset_id), int(intervention.duration_min or 0))
                )
            elif kind is InterventionType.POWER_SWITCHOVER and (intervention.asset_id or intervention.facility_id):
                plan.power_switchovers.append((step, str(intervention.asset_id or intervention.facility_id)))
            elif kind is InterventionType.ISSUE_PUBLIC_ALERT:
                plan.alerts.append(step)
    return plan


# --------------------------------------------------------------------------- #
# member execution
# --------------------------------------------------------------------------- #
@dataclass
class MemberResult:
    index: int
    peak: KPISet
    frames: list[TimelineFrame]
    effects: list[Effect]
    isolation_events: list[dict[str, Any]]
    final_depths: dict[str, float]
    final_snapshot: TwinSnapshot
    diagnostics: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    score: float = 0.0


def _member_rng(plan: ScenarioPlan, index: int) -> random.Random:
    return random.Random(plan.seed * 7919 + index * 104_729)


def _build_forcing(
    plan: ScenarioPlan,
    step: int,
    *,
    rain_multiplier: float,
    surge_noise: float,
    pump_failures: set[str],
    starting_rainfall_mm_h: float = 0.0,
) -> Forcing:
    """Assemble one timestep of scenario forcing, applying the member's perturbations."""
    rainfall = (plan.rain_schedule.get(step, 0.0) + starting_rainfall_mm_h) * rain_multiplier
    return Forcing(
        rainfall_mm_h=rainfall,
        surge_m=plan.surge_schedule.get(step, 0.0) + surge_noise,
        release_m3s=dict(plan.release_schedule.get(step, {})),
        injections=dict(plan.injection_schedule.get(step, {})),
        pump_multiplier={pump_id: 0.0 for pump_id in pump_failures},
    )


def _update_asset_states(
    snapshot: TwinSnapshot,
    depths: Mapping[str, float],
    *,
    step_minutes: int,
    elapsed_min: float,
    overrides: Mapping[str, Any],
    power_deadlines: dict[str, float],
    forced_power: set[str] | None = None,
) -> dict[str, float]:
    """Translate depths into operational states and derive cascade seeds."""
    seeds: dict[str, float] = {}

    for node in snapshot.nodes.values():
        depth = float(depths.get(node.id, node.flood_depth_m))
        override = overrides.get(node.id)
        if override is not None:
            if getattr(override, "status", None):
                node.status = override.status if isinstance(override.status, AssetStatus) else AssetStatus(str(override.status))
            if getattr(override, "flood_depth_m", None) is not None:
                depth = max(depth, float(override.flood_depth_m))
            if getattr(override, "power_available", None) is not None:
                node.power_available = bool(override.power_available)
            if getattr(override, "access_available", None) is not None:
                node.access_available = bool(override.access_available)
            if getattr(override, "service_level", None) is not None:
                node.service_level = float(override.service_level)

        node.flood_depth_m = round(depth, 3)
        previous_status = node.status

        if node.is_mobility:
            threshold = float(node.capacity.get("flood_threshold_m") or 0.3)
            heavy = float(node.capacity.get("heavy_threshold_m") or threshold + 0.25)
            if depth >= heavy:
                node.status = worst(node.status, AssetStatus.FAILED)
                node.access_available = False
                node.service_level = 0.0
            elif depth >= threshold:
                node.status = worst(node.status, AssetStatus.IMPAIRED)
                node.access_available = False
                node.service_level = min(node.service_level, 0.25)
            elif depth > threshold * 0.5:
                node.status = worst(node.status, AssetStatus.DEGRADED)
                node.service_level = min(node.service_level, 0.6)
        elif node.asset_type in FACILITY_DEPTH_BANDS:
            degraded_m, impaired_m, failed_m = FACILITY_DEPTH_BANDS[node.asset_type]
            if depth >= failed_m:
                node.status = worst(node.status, AssetStatus.FAILED)
                node.service_level = min(node.service_level, SERVICE_LEVEL_BY_BAND[3])
            elif depth >= impaired_m:
                node.status = worst(node.status, AssetStatus.IMPAIRED)
                node.service_level = min(node.service_level, SERVICE_LEVEL_BY_BAND[2])
            elif depth >= degraded_m:
                node.status = worst(node.status, AssetStatus.DEGRADED)
                node.service_level = min(node.service_level, SERVICE_LEVEL_BY_BAND[1])

        # ---- power state, including on-site autonomy -------------------
        # An operator-commanded switchover is a *decision*, not a hydraulic fact:
        # it restores supply to an asset whose feeder failed, which is exactly the
        # kind of manual intervention an EOC can actually order.
        if forced_power and node.id in forced_power:
            node.power_available = True
        elif node.asset_type in {AssetType.HOSPITAL, AssetType.PUMP_STATION, AssetType.WATER_TREATMENT, AssetType.TELECOM_TOWER}:
            providers = snapshot.in_edges(node.id, kinds=[RelationKind.POWERS])
            if providers:
                healthy = False
                for edge in providers:
                    provider = snapshot.nodes.get(edge.source_id)
                    if provider is None or status_rank(provider.status) < status_rank(AssetStatus.IMPAIRED):
                        healthy = True
                        break
                if not healthy:
                    autonomy_h = float(
                        node.capacity.get("generator_hours")
                        or node.capacity.get("backup_hours")
                        or node.capacity.get("autonomy_hours")
                        or 0.0
                    )
                    deadline = power_deadlines.setdefault(node.id, elapsed_min + autonomy_h * 60.0)
                    if elapsed_min >= deadline:
                        node.power_available = False
                    else:
                        node.power_available = True  # running on generator / UPS

        if status_rank(node.status) >= status_rank(AssetStatus.IMPAIRED):
            severity = 1.0 if node.status is AssetStatus.FAILED else 0.62
            depth_ratio = depth / max(0.15, float(node.capacity.get("flood_threshold_m") or 0.35))
            severity = min(1.0, severity + 0.25 * min(1.0, depth_ratio))
            seeds[node.id] = round(severity, 3)
        elif node.status is AssetStatus.DEGRADED:
            seeds.setdefault(node.id, 0.32)
        elif status_rank(node.status) > status_rank(previous_status):
            seeds.setdefault(node.id, 0.3)

        node.state_observed_at = datetime.now(timezone.utc)
        node.state_source = "simulation"
    return seeds


def run_member(
    base_snapshot: TwinSnapshot,
    plan: ScenarioPlan,
    index: int,
    *,
    ensemble_size: int,
    collect_frames: bool = True,
    driver_overrides: Mapping[str, float] | None = None,
    severity_seeds: Mapping[str, float] | None = None,
    starting_rainfall_mm_h: float = 0.0,
) -> MemberResult:
    """Run one ensemble member across the full horizon. Pure CPU, no I/O."""
    request = plan.request
    dt = request.timestep_minutes
    steps = plan.steps
    rng = _member_rng(plan, index)
    overrides_map = {o.asset_id: o for o in request.asset_overrides}
    drivers = {
        "rain_multiplier": 1.0,
        "surge_noise": 0.0,
        "drainage_multiplier": 1.0,
        "pump_failure_rate": 0.12,
        **(driver_overrides or {}),
    }

    snapshot = base_snapshot.fork()
    if severity_seeds:
        for asset_id, severity in status_overrides.items():
            node = snapshot.nodes.get(asset_id)
            if node is None:
                continue
            node.status = worst(
                node.status,
                AssetStatus.FAILED if severity >= 0.85 else AssetStatus.IMPAIRED if severity >= 0.55 else AssetStatus.DEGRADED,
            )

    for asset_id, status in plan.status_overrides.items():
        node = snapshot.nodes.get(asset_id)
        if node is not None:
            node.status = worst(node.status, status)
            if status is AssetStatus.FAILED:
                node.service_level = 0.0

    hydro = HydroModel(snapshot)
    dependency = DependencyGraph(snapshot)
    router = MobilityRouter(dependency)
    exposure = ExposureModel(snapshot)

    # per-member perturbations: a scenario is a distribution, not a value
    rain_multiplier = max(0.45, min(2.0, rng.lognormvariate(0.0, 0.16) * drivers["rain_multiplier"]))
    surge_noise = rng.gauss(0.0, 0.07) + drivers["surge_noise"]
    if rng.random() < 0.08:  # occasional storm-surge coincidence
        surge_noise += 0.22
    drainage_multiplier = max(0.65, min(1.25, rng.gauss(1.0, 0.09) * drivers["drainage_multiplier"]))
    pumps = list(snapshot.of_type(AssetType.PUMP_STATION))
    pump_failures = {p.id for p in pumps if rng.random() < drivers["pump_failure_rate"]}
    if plan.prepared_pumps:
        # a crewed, fuelled, pre-checked station does not enter the failure draw:
        # this is the mechanism behind "service the fleet before the storm"
        pump_failures -= plan.prepared_pumps

    response_pairs = _response_pairs(snapshot)
    power_deadlines: dict[str, float] = {}
    frames: list[TimelineFrame] = []
    all_effects: list[Effect] = []
    isolation_events: list[dict[str, Any]] = []
    peak = KPISet()
    peak_depth = 0.0
    final_depths: dict[str, float] = {}
    diagnostics: dict[str, Any] = {}
    notes: list[str] = []

    closed_now: set[str] = set()
    started_pumps: set[str] = set()
    opened_shelters: set[str] = set()
    pre_positioned: set[str] = set()
    switchover_done: set[str] = set()
    contingency_targets: set[str] = {facility_id for _step, facility_id, _duration in plan.contingency_routes}

    for step in range(steps + 1):
        t_min = step * dt
        forcing = _build_forcing(
            plan,
            step,
            rain_multiplier=rain_multiplier,
            surge_noise=surge_noise,
            pump_failures=pump_failures,
            starting_rainfall_mm_h=starting_rainfall_mm_h,
        )
        forcing.drainage_multiplier = {zone_id: drainage_multiplier for zone_id in snapshot.zones}
        if plan.closed_roads_by_step:
            closed_now = set()
            for step_index in range(step + 1):
                closed_now |= plan.closed_roads_by_step.get(step_index, set())
            closed_now.discard("")

        for deployment_step, target, capacity, quantity in plan.pump_deployments:
            if deployment_step == step and target and target not in started_pumps:
                note = hydro.apply_intervention(
                    InterventionType.DEPLOY_PUMP,
                    {"asset_id": target, "location_asset_id": target, "capacity_m3s": capacity, "quantity": quantity},
                )
                started_pumps.add(target)
                notes.append(f"t+{t_min} min: {note}")
        for evacuation_step, facility_id, fraction in plan.facility_evacuations:
            if evacuation_step == step:
                facility = snapshot.nodes.get(facility_id)
                if facility is not None:
                    beds = float(facility.capacity.get("beds_occupied") or 0.0)
                    moved = beds * min(1.0, fraction)
                    facility.capacity["beds_occupied"] = max(0.0, beds - moved)
                    facility.attributes["evacuated"] = True
                    notes.append(f"t+{t_min} min: {moved:.0f} patients evacuated from {facility.name}")

        for open_step, shelter_id, extra_capacity in plan.shelter_capacity_additions:
            if open_step <= step and shelter_id not in opened_shelters:
                shelter = snapshot.nodes.get(shelter_id)
                if shelter is not None:
                    current = int(shelter.capacity.get("capacity_people") or 0)
                    shelter.capacity["capacity_people"] = current + int(extra_capacity)
                    shelter.capacity["annex_capacity"] = int(extra_capacity)
                    opened_shelters.add(shelter_id)
                    notes.append(f"t+{t_min} min: {shelter.name} annex opened (+{int(extra_capacity):,} places)")

        for pre_step, facility_id in plan.pre_positioned:
            if pre_step <= step and facility_id not in pre_positioned:
                facility = snapshot.nodes.get(facility_id)
                if facility is not None:
                    pre_positioned.add(facility_id)
                    notes.append(f"t+{t_min} min: generators and ambulance staging pre-positioned at {facility.name}")

        for power_step, asset_id in plan.power_switchovers:
            if power_step <= step and asset_id not in switchover_done:
                node_state = snapshot.nodes.get(asset_id)
                if node_state is not None:
                    switchover_done.add(asset_id)
                    node_state.attributes["forced_power"] = True
                    notes.append(f"t+{t_min} min: manual switchover to on-site supply at {node_state.name}")

        depths, hydro_diag = hydro.step(dt, forcing, minute_from_start=float(t_min))
        diagnostics = {
            "tide_m": hydro_diag.tide_m,
            "tide_locked_bodies": hydro_diag.tide_locked_bodies,
            "overtopping_bodies": hydro_diag.overtopping_bodies,
            "pumped_m3s": round(hydro_diag.total_pumped_m3s, 2),
            "gravity_m3s": round(hydro_diag.total_gravity_m3s, 2),
            "notes": hydro_diag.notes,
        }
        final_depths = depths
        peak_depth = max(peak_depth, max(depths.values()) if depths else 0.0)

        seeds = _update_asset_states(
            snapshot,
            depths,
            step_minutes=dt,
            elapsed_min=float(t_min),
            overrides=overrides_map,
            power_deadlines=power_deadlines,
            forced_power=switchover_done,
        )
        remaining = max(0, request.horizon_minutes - t_min)
        effects = dependency.propagation(seeds, horizon_min=remaining) if seeds and remaining > 0 else []
        if effects:
            dependency.update_statuses(effects, snapshot)
            effects = [e for e in effects if e.failed_after + t_min <= request.horizon_minutes]
            for effect in effects:
                effect.failed_after = round(effect.failed_after + t_min, 1)
            all_effects.extend(effects)

        signature = router.signature(depths, closed_now)
        delays = router.response_delays(response_pairs, signature=signature, impassable_depth=depths)
        if contingency_targets or pre_positioned:
            delays = _apply_response_measures(delays, contingency_targets, pre_positioned)
        for key, delay in delays.items():
            if delay >= 240.0:
                origin, _, destination = key.partition("->")
                isolation_events.append(
                    {
                        "t_min": t_min,
                        "origin_id": origin,
                        "destination_id": destination,
                        "origin_name": snapshot.nodes[origin].name if origin in snapshot.nodes else origin,
                        "destination_name": snapshot.nodes[destination].name if destination in snapshot.nodes else destination,
                        "note": "no traversable modelled corridor remains",
                    }
                )

        cascade_depth = max((e.hop for e in effects), default=0)
        alert_active = bool(plan.alerts) and step >= min(plan.alerts)
        kpis = kpis_from_state(
            snapshot,
            depths=depths,
            response_delays=delays,
            cascade_depth=float(cascade_depth),
            alert_active=alert_active,
        )
        peak = _peak_of(peak, kpis)

        if collect_frames and (step % max(1, steps // 24) == 0 or step == steps):
            frames.append(
                _frame(
                    dt,
                    t_min,
                    snapshot,
                    depths,
                    kpis,
                    {zone_id: zone.ponding_m for zone_id, zone in hydro.zones.items()},
                )
            )

    result = MemberResult(
        index=index,
        peak=peak,
        frames=frames,
        effects=all_effects,
        isolation_events=isolation_events[:64],
        final_depths=final_depths,
        final_snapshot=snapshot,
        diagnostics={
            **diagnostics,
            "rain_multiplier": round(rain_multiplier, 3),
            "surge_noise_m": round(surge_noise, 3),
            "drainage_multiplier": round(drainage_multiplier, 3),
            "failed_pumps": sorted(pump_failures),
            "router": router.stats(),
            "peak_depth_m": round(peak_depth, 3),
        },
        notes=notes,
    )
    result.score = score_kpis(peak)
    return result


def _apply_response_measures(
    delays: Mapping[str, float],
    contingency_targets: set[str],
    pre_positioned: set[str],
) -> dict[str, float]:
    """Model operational measures that change *reachability*, not the water.

    A signed contingency corridor does not lower the flood, but it does mean a
    crew reaches the hospital: the pair stops being severed. Pre-positioned
    assets shave the delay because the response starts metres away instead of
    across the city. Both effects are explicitly bounded so that the model can
    never claim signage defeated a flood.
    """
    adjusted: dict[str, float] = {}
    for key, delay in delays.items():
        _origin, _, destination = key.partition("->")
        value = float(delay)
        if destination in contingency_targets and value >= 240.0:
            value = 45.0  # escorted contraflow corridor: passable, still congested
        if destination in pre_positioned:
            value = min(value, 25.0) if value >= 240.0 else value * 0.75
        adjusted[key] = value
    return adjusted


def _peak_of(current: KPISet, candidate: KPISet) -> KPISet:
    """Cumulative-worst KPI: a crisis is judged by its peak, not its average."""
    payload: dict[str, float] = {}
    for field_name in KPISet.model_fields:
        a = float(getattr(current, field_name))
        b = float(getattr(candidate, field_name))
        payload[field_name] = min(a, b) if field_name == "equity_index" else max(a, b)
    return KPISet(**payload)


def _response_pairs(snapshot: TwinSnapshot) -> list[tuple[str, str]]:
    """Responder -> care-facility pairs used to measure mobility degradation."""
    responders = snapshot.of_type(AssetType.FIRE_STATION, AssetType.POLICE_STATION)
    targets = snapshot.of_type(AssetType.HOSPITAL) + snapshot.of_type(AssetType.SHELTER)[:4]
    pairs: list[tuple[str, str]] = []
    for responder in responders:
        ranked = sorted(targets, key=lambda t: haversine_m(responder.lon, responder.lat, t.lon, t.lat))
        for target in ranked[:5]:
            pairs.append((responder.id, target.id))
    return pairs


def _frame(
    dt: int,
    t_min: int,
    snapshot: TwinSnapshot,
    depths: Mapping[str, float],
    kpis: KPISet,
    ponding_by_zone: Mapping[str, float],
) -> TimelineFrame:
    """One point on the scenario timeline.

    Flood extent is derived from zone ponding depth above a 5 cm sheet threshold
    mapped onto the zone area - the same convention used by municipal flood
    reporting, so the number is comparable with a corporation's own records.
    """
    extent_km2 = 0.0
    for zone_id, ponding in ponding_by_zone.items():
        if ponding <= 0.05:
            continue
        zone = snapshot.zones.get(zone_id)
        if zone is None:
            continue
        area_km2 = (zone.population / zone.population_density) if zone.population_density else 2.6
        # only the ponding fraction of a ward is actually under water
        extent_km2 += area_km2 * 0.28 * min(1.0, (ponding - 0.05) / 0.45)
    return TimelineFrame(
        t_min=t_min,
        label=f"+{t_min // 60}h{t_min % 60:02d}m",
        kpis=kpis,
        active_incidents=sum(
            1 for n in snapshot.nodes.values() if status_rank(n.status) >= status_rank(AssetStatus.IMPAIRED)
        ),
        flood_extent_km2=round(extent_km2, 3),
        max_depth_m=round(max(depths.values()) if depths else 0.0, 3),
        critical_failures=[
            n.name for n in snapshot.nodes.values() if n.status is AssetStatus.FAILED and n.criticality >= 0.8
        ][:12],
    )


# --------------------------------------------------------------------------- #
# ensemble orchestration
# --------------------------------------------------------------------------- #
@dataclass
class ScenarioOutcome:
    kpis: KPISet
    p10: KPISet
    p90: KPISet
    representative: MemberResult
    members: list[MemberResult]
    runtime_ms: int
    determinism_token: str
    degraded: bool = False


def run_scenario(
    base_snapshot: TwinSnapshot,
    request: SimulationRequest,
    *,
    progress: ProgressCallback | None = None,
    ensemble_size: int | None = None,
    severity_seeds: Mapping[str, float] | None = None,
    driver_overrides: Mapping[str, float] | None = None,
    starting_rainfall_mm_h: float = 0.0,
) -> ScenarioOutcome:
    """Run the full ensemble and aggregate. Synchronous; call via ``to_thread``."""
    t0 = time.perf_counter()
    plan = resolve_scenario(base_snapshot, request)
    size = int(ensemble_size or request.ensemble_size or settings.sim_ensemble_size)
    size = max(1, min(size, settings.sim_max_ensemble_size))

    members: list[MemberResult] = []
    for index in range(size):
        member = run_member(
            base_snapshot,
            plan,
            index,
            ensemble_size=size,
            collect_frames=True,
            driver_overrides=driver_overrides,
            severity_seeds=severity_seeds,
            starting_rainfall_mm_h=starting_rainfall_mm_h,
        )
        members.append(member)
        if progress is not None:
            progress(
                (index + 1) / size * 0.9,
                f"ensemble member {index + 1}/{size} complete",
                {"score": member.score, "peak_depth_m": member.diagnostics.get("peak_depth_m")},
            )

    samples = [m.peak for m in members]
    p10, p50, p90 = aggregate_kpis(samples)
    representative = min(members, key=lambda m: abs(m.score - score_kpis(p50)))
    token = hashlib.sha256(
        f"{plan.seed}:{base_snapshot.digest()}:{size}:{request.horizon_minutes}:{request.timestep_minutes}".encode()
    ).hexdigest()[:32]

    if progress is not None:
        progress(0.95, "aggregating ensemble", {})
    return ScenarioOutcome(
        kpis=p50,
        p10=p10,
        p90=p90,
        representative=representative,
        members=members,
        runtime_ms=int((time.perf_counter() - t0) * 1000),
        determinism_token=token,
        degraded=base_snapshot.degraded,
    )


# --------------------------------------------------------------------------- #
# result assembly
# --------------------------------------------------------------------------- #
def _roads_from(snapshot: TwinSnapshot, depths: Mapping[str, float]) -> list[RoadState]:
    roads: list[RoadState] = []
    for node in snapshot.roads:
        depth = float(depths.get(node.id, node.flood_depth_m))
        threshold = float(node.capacity.get("flood_threshold_m") or 0.3)
        if depth >= threshold:
            status = AssetStatus.FAILED if depth >= float(node.capacity.get("heavy_threshold_m") or threshold + 0.25) else AssetStatus.IMPAIRED
        elif depth > threshold * 0.5:
            status = AssetStatus.DEGRADED
        else:
            status = node.status
        roads.append(
            RoadState(
                asset_id=node.id,
                name=node.name,
                status=status,
                depth_m=round(depth, 3),
                impassable_from_min=0 if status in (AssetStatus.FAILED, AssetStatus.IMPAIRED) else None,
                impassable_until_min=None,
                detour_eta_penalty_min=round(max(0.0, depth - threshold) * 60.0, 1),
            )
        )
    roads.sort(key=lambda r: (status_rank(r.status), r.depth_m), reverse=True)
    return roads


def _facilities_from(snapshot: TwinSnapshot, depths: Mapping[str, float]) -> list[FacilityState]:
    facilities: list[FacilityState] = []
    for node in snapshot.facilities:
        depth = float(depths.get(node.id, node.flood_depth_m))
        autonomy = float(node.capacity.get("autonomy_hours") or node.capacity.get("generator_hours") or 0.0)
        facilities.append(
            FacilityState(
                asset_id=node.id,
                name=node.name,
                asset_type=node.asset_type,
                status=node.status,
                service_level=round(node.service_level, 3),
                power_available=bool(node.power_available),
                hours_a_autonomy=round(autonomy, 1),
                accessible=bool(node.access_available),
                occupancy_or_load=float(node.capacity.get("beds_occupied") or node.capacity.get("pumps") or 0.0),
                capacity=float(node.capacity.get("beds_total") or node.capacity.get("capacity_mld") or 0.0),
                note=_facility_note(node, depth),
            )
        )
    facilities.sort(key=lambda f: (status_rank(f.status), -f.occupancy_or_load), reverse=True)
    return facilities


def _facility_note(node: AssetState, depth: float) -> str:
    parts: list[str] = []
    if status_rank(node.status) >= status_rank(AssetStatus.IMPAIRED):
        parts.append(f"state {node.status.value} (dependency loss)")
    elif node.status is AssetStatus.DEGRADED:
        parts.append("state degraded")
    if depth > 0.05:
        parts.append(f"{depth:.2f} m water at the asset")
    if not node.power_available:
        parts.append("on-site power lost")
    if not node.access_available:
        parts.append("access route impassable")
    if node.capacity.get("oxygen_hours"):
        parts.append(f"{node.capacity['oxygen_hours']:.0f} h oxygen reserve")
    return "; ".join(parts) or "operating normally"


def _shelters_from(snapshot: TwinSnapshot, depths: Mapping[str, float], exposure: ExposureModel) -> list[ShelterState]:
    state = exposure.evaluate(depths=depths)
    by_id = {a.shelter_id: a for a in state.shelter_allocations}
    out: list[ShelterState] = []
    for node in snapshot.shelters:
        allocation = by_id.get(node.id)
        capacity = int(node.capacity.get("capacity_people") or 0)
        occupancy = allocation.assigned if allocation else int(node.capacity.get("occupancy") or 0)
        out.append(
            ShelterState(
                asset_id=node.id,
                name=node.name,
                capacity=capacity,
                occupancy=occupancy,
                over_capacity=max(0, occupancy - capacity),
                status=node.status,
            )
        )
    out.sort(key=lambda s: s.over_capacity, reverse=True)
    return out


def _cascade_impacts(outcome: ScenarioOutcome, request: SimulationRequest) -> list[CascadeImpact]:
    """Merge every member's cascade findings into a frequency-weighted list.

    A mechanism that appears in 46/48 members is materially different from one
    that appears in 3/48, and the UI must be able to show that difference.
    """
    counts: dict[tuple[str, str, str], dict[str, Any]] = {}
    total = max(1, len(outcome.members))
    for member in outcome.members:
        seen: set[tuple[str, str, str]] = set()
        for effect in member.effects:
            key = (effect.cause_id, effect.target_id, effect.relation_kind.value)
            if key in seen:
                continue
            seen.add(key)
            entry = counts.setdefault(
                key,
                {
                    "effect": effect,
                    "members": 0,
                    "earliest": effect.failed_after,
                    "magnitude": effect.magnitude,
                },
            )
            entry["members"] += 1
            entry["earliest"] = min(entry["earliest"], effect.failed_after)
            entry["magnitude"] = max(entry["magnitude"], effect.magnitude)

    snapshot = outcome.representative.final_snapshot
    impacts: list[CascadeImpact] = []
    for key, entry in counts.items():
        effect: Effect = entry["effect"]
        source = snapshot.nodes.get(effect.cause_id)
        target = snapshot.nodes.get(effect.target_id)
        if source is None or target is None:
            continue
        frequency = entry["members"] / total
        impacts.append(
            CascadeImpact(
                timestep_min=int(entry["earliest"]) // max(1, request.timestep_minutes) * max(1, request.timestep_minutes),
                cause_asset_id=effect.cause_id,
                cause_asset_name=source.name,
                target_asset_id=effect.target_id,
                target_asset_name=target.name,
                target_type=target.asset_type,
                mechanism=effect.mechanism,
                relation_kind=effect.relation_kind,
                lead_time_min=effect.lead_time_min,
                magnitude=round(entry["magnitude"], 3),
                probability=round(min(0.99, 0.35 + 0.65 * frequency) * effect.probability, 3),
                status_after=effect.status_after,
                hop=effect.hop,
                evidence_ids=_cascade_evidence(source, target, effect),
                narrative=(
                    f"{source.name} degrades → {target.name} affected via {effect.mechanism.lower()} "
                    f"({entry['members']}/{total} ensemble members, earliest t+{entry['earliest']:.0f} min, "
                    f"hop {effect.hop})"
                ),
            )
        )
    impacts.sort(key=lambda i: (i.timestep_min, -i.magnitude))
    return impacts[:220]


def _cascade_evidence(source: AssetState, target: AssetState, effect: Effect) -> list[str]:
    return [
        f"asset:{source.id}",
        f"asset:{target.id}",
        f"edge:{effect.cause_id}->{effect.target_id}:{effect.relation_kind.value}",
        f"path:{'>'.join(effect.path)}",
    ]


def result_snapshot(outcome: ScenarioOutcome) -> TwinSnapshot:
    """The twin state the result describes.

    Callers that need to reason about *objects* (the recommendation engine, for
    example) use this rather than the serialised result, which carries only the
    presentation shapes.
    """
    return outcome.representative.final_snapshot


def build_result(
    outcome: ScenarioOutcome,
    request: SimulationRequest,
    *,
    baseline: ScenarioOutcome | None = None,
    baseline_id: str | None = None,
    recommendations: list[Recommendation] | None = None,
    sensitivity: list[DriverSensitivity] | None = None,
    narrative: str = "",
) -> SimulationResult:
    rep = outcome.representative
    snapshot = rep.final_snapshot
    depths = rep.final_depths
    exposure = ExposureModel(snapshot)

    uncertainty_spread = [
        (getattr(outcome.p90, f) - getattr(outcome.p10, f)) / abs(getattr(outcome.kpis, f))
        for f in KPISet.model_fields
        if abs(getattr(outcome.kpis, f)) > 1e-6
    ]
    mean_spread = sum(uncertainty_spread) / len(uncertainty_spread) if uncertainty_spread else 0.0
    confidence = max(0.25, min(0.96, 0.92 - 0.5 * mean_spread))
    freshness = freshness_band(0.0 if not snapshot.degraded else 900.0)

    explainability = Explainability(
        confidence=round(confidence, 3),
        band=confidence_band(confidence),
        model="custos-hydro-lumped-v1 + dependency-cascade-v1",
        method="monte-carlo ensemble over coupled hydrology, dependency cascade and mobility routing",
        data_freshness=freshness,
        assumption_list=ASSUMPTIONS + ([request.notes] if request.notes else []),
        evidence_ids=[
            f"twin:{outcome.determinism_token}",
            f"ensemble:{len(outcome.members)}",
            *(f"asset:{n.id}" for n in list(snapshot.nodes.values())[:6] if status_rank(n.status) >= status_rank(AssetStatus.DEGRADED)),
        ],
        uncertainty={
            "kpi_relative_spread_mean": round(mean_spread, 4),
            "ensemble_size": float(len(outcome.members)),
            "runtime_ms": float(outcome.runtime_ms),
        },
        counterfactuals=[r.counterfactual for r in (recommendations or []) if r.counterfactual][:5],
        grounded=True,
        notes=(
            "Hydrology is a lumped mass balance calibrated for order-of-magnitude and "
            "ordering fidelity, not a 2-D hydrodynamic solution."
        ),
    )

    mobility = MobilityState(
        avg_response_delay_min=outcome.kpis.avg_response_delay_min,
        p90_response_delay_min=outcome.kpis.p90_response_delay_min,
        impassable_roads=[r.asset_id for r in _roads_from(snapshot, depths) if status_rank(r.status) >= status_rank(AssetStatus.IMPAIRED)],
        isolation_events=_dedupe_isolation(rep.isolation_events),
    )

    return SimulationResult(
        id="preview",
        request=request,
        status="complete",
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        runtime_ms=outcome.runtime_ms,
        kpis=outcome.kpis,
        uncertainty=KPIUncertainty(p10=outcome.p10, p50=outcome.kpis, p90=outcome.p90),
        timeline=rep.frames,
        cascade=_cascade_impacts(outcome, request),
        roads=_roads_from(snapshot, depths),
        facilities=_facilities_from(snapshot, depths),
        shelters=_shelters_from(snapshot, depths, exposure),
        mobility=mobility,
        recommendations=recommendations or [],
        sensitivity=sensitivity or [],
        explainability=explainability,
        baseline_id=baseline_id,
        baseline_kpis=baseline.kpis if baseline else None,
        delta=diff_kpis(baseline.kpis, outcome.kpis) if baseline else None,
        narrative=narrative,
        determinism_token=outcome.determinism_token,
        degraded=outcome.degraded,
    )


# --------------------------------------------------------------------------- #
# sensitivity analysis
# --------------------------------------------------------------------------- #
DRIVER_GRID: list[tuple[str, float, float, str]] = [
    ("rain_multiplier", 0.75, 1.3, "rainfall intensity (0.75× – 1.3×)"),
    ("surge_noise", -0.25, 0.35, "tidal surge (−0.25 m – +0.35 m)"),
    ("drainage_multiplier", 0.7, 1.2, "drainage network capacity (0.7× – 1.2×)"),
    ("pump_failure_rate", 0.0, 0.4, "pump availability (0% – 40% failure)"),
]


def sensitivity_analysis(base_snapshot: TwinSnapshot, request: SimulationRequest, *, ensemble_size: int = 6) -> list[DriverSensitivity]:
    """One-at-a-time driver sweeps: 'what actually decides the outcome here?'"""
    results: list[DriverSensitivity] = []
    for driver, low, high, label in DRIVER_GRID:
        try:
            low_outcome = run_scenario(
                base_snapshot, request, ensemble_size=ensemble_size, driver_overrides={driver: low}
            )
            high_outcome = run_scenario(
                base_snapshot, request, ensemble_size=ensemble_size, driver_overrides={driver: high}
            )
        except Exception as exc:  # noqa: BLE001 - sensitivity is a bonus, never a blocker
            log.warning("sensitivity driver failed", extra={"custos": {"driver": driver, "error": str(exc)[:160]}})
            continue
        low_score = score_kpis(low_outcome.kpis)
        high_score = score_kpis(high_outcome.kpis)
        results.append(
            DriverSensitivity(
                driver=label,
                low_kpi=round(low_score, 2),
                high_kpi=round(high_score, 2),
                swing=round(abs(high_score - low_score), 2),
            )
        )
    results.sort(key=lambda r: r.swing, reverse=True)
    return results


def _dedupe_isolation(events: Sequence[Mapping[str, Any]], limit: int = 40) -> list[dict[str, Any]]:
    """Collapse per-timestep isolation records into one line per severed pair.

    A pair that is severed at t+120 and still severed at t+360 is *one* severed
    pair, not five. Showing the repeats would make a 6-pair problem look like a
    30-event crisis and would bury the pairs that actually recovered.
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        key = (str(event.get("origin_id")), str(event.get("destination_id")))
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(event)
            merged[key]["first_t_min"] = event.get("t_min")
            merged[key]["last_t_min"] = event.get("t_min")
        else:
            existing["last_t_min"] = max(existing.get("last_t_min") or 0.0, float(event.get("t_min") or 0.0))
    ordered = sorted(merged.values(), key=lambda e: (e.get("first_t_min") or 0.0))
    for entry in ordered:
        entry["severed_minutes"] = round(float(entry.get("last_t_min") or 0.0) - float(entry.get("first_t_min") or 0.0), 1)
    return ordered[:limit]


def build_narrative(outcome: ScenarioOutcome, request: SimulationRequest) -> str:
    """Deterministic, template-grounded summary.

    Deliberately *not* generated by a language model unless one is available and
    every sentence can be traced to a number - an EOC brief must be reproducible
    and auditable, so the template is the source of truth and the model, when
    present, only rewrites it (see ``custos.ai.grounded``).
    """
    rep = outcome.representative
    k = outcome.kpis
    worst_roads = [r for r in _roads_from(rep.final_snapshot, rep.final_depths) if r.depth_m > 0.05][:3]
    lines = [
        f"Scenario '{request.name}' over {request.horizon_minutes // 60} h "
        f"({len(outcome.members)} ensemble members, {request.timestep_minutes}-minute steps).",
        f"Peak exposure: {k.patients_at_risk:.0f} patients at risk, {k.hospital_capacity_lost_pct:.1f} % of hospital capacity lost, "
        f"{k.people_requiring_rescue:.0f} people likely to need rescue.",
        f"Mobility: {k.roads_impassable_km:.1f} km of road impassable, {k.critical_roads_impassable:.0f} critical corridors "
        f"affected. Access: mean delay {k.avg_response_delay_min:.1f} min and p90 {k.p90_response_delay_min:.1f} min "
        f"*measured only over pairs that remain reachable* - {k.severed_response_pairs:.0f} responder-to-facility pairs are "
        f"severed outright and contribute no delay figure at all, which is why the mean delay looks small next to them.",
        f"Lifelines: {k.population_power_outage:,.0f} people lose power, {k.population_water_outage:,.0f} lose water supply, "
        f"{k.telecom_coverage_lost_pct:.1f} % of the population loses coverage.",
        f"Shelter: {k.shelters_over_capacity_people:,.0f} people exceed the capacity of the dry shelters they can reach.",
        f"Equity index {k.equity_index:.2f} (1.00 = impact evenly distributed across vulnerability bands).",
    ]
    if worst_roads:
        lines.append(
            "Earliest losses: "
            + "; ".join(f"{r.name} ({r.depth_m:.2f} m)" for r in worst_roads)
            + "."
        )
    span = f"{outcome.p10.patients_at_risk:.0f}–{outcome.p90.patients_at_risk:.0f}"
    lines.append(
        f"Uncertainty: patients at risk range {span} across the ensemble (p10–p90), "
        f"driven mainly by rainfall intensity and pump availability."
    )
    return " ".join(lines)
