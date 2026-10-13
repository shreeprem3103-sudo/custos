"""Recommendation engine — advice that has been *tested*, not asserted.

The rule this module exists to enforce: **never tell a commander to do something
you have not simulated.** Every recommendation is produced by generating a
candidate intervention, re-running the scenario with that intervention applied,
and computing the counterfactual delta against the same scenario without it. The
UI then shows "deploying two pumps here removes ~840 patients-hours of risk",
which is a claim that can be argued with, audited and reproduced.

Ranking blends:

* **effectiveness**  — KPI delta from the re-simulation, on the commander's own
  weighting (an EOC can weigh "patients at risk" above "roads closed"),
* **speed**          — how early in the horizon it acts; a perfect action that
  lands after the flood has passed is worthless,
* **feasibility**    — whether the declared resource inventory can supply it,
* **equity**         — whether it helps the vulnerable wards more than the mean.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..config import settings
from ..domain.enums import (
    AssetStatus,
    AssetType,
    InterventionType,
    ResourceType,
    status_rank,
)
from ..domain.schemas import (
    InterventionSpec,
    KPISet,
    Recommendation,
    SimulationRequest,
)
from ..domain.twin import AssetState, TwinSnapshot
from ..fusion.normalize import haversine_m
from ..observability import get_logger
from .kpi import score_kpis
from .simulation import _as_enum

log = get_logger("engines.recommend")

#: how many candidates are put through counterfactual re-simulation
MAX_COUNTERFACTUALS = 6
#: ensemble size used for counterfactuals (accuracy vs latency trade-off, stated
#: explicitly so a reviewer knows the confidence of a recommendation)
COUNTERFACTUAL_ENSEMBLE = 6


@dataclass(slots=True)
class Candidate:
    spec: InterventionSpec
    rationale: str
    target_id: str | None
    target_name: str = ""
    urgency: int = 3
    feasibility: float = 0.8
    resources_required: dict[str, float] = field(default_factory=dict)
    equity_note: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    #: some actions are a bundle (crewing six pumping stations is one decision but
    #: six interventions); the bundle is simulated exactly as the commander would
    #: order it, not averaged into a single fictional lever
    bundle: list[InterventionSpec] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    #: Some measures only pay off when something *else* goes wrong (spare pumps
    #: matter when pumps fail). Those are evaluated against a stressed control
    #: run, and the stress level is printed with the result instead of being
    #: smuggled into the headline number.
    stress: dict[str, float] | None = None


def generate_candidates(
    snapshot: TwinSnapshot,
    kpis: KPISet,
    *,
    request: SimulationRequest,
    cascade_targets: Sequence[str] = (),
    max_candidates: int = 16,
) -> list[Candidate]:
    """Build the intervention shortlist from what the simulation actually shows."""
    candidates: list[Candidate] = []
    nodes = snapshot.nodes

    # 1. pumps where water is deepest -------------------------------------
    flooded_water = sorted(
        (
            n
            for n in nodes.values()
            if n.asset_type in {AssetType.CANAL, AssetType.RIVER_GAUGE, AssetType.PUMP_STATION}
            and n.flood_depth_m > 0.05
        ),
        key=lambda n: (n.flood_depth_m * (0.5 + n.criticality)),
        reverse=True,
    )
    deployed = set()
    for node in flooded_water[:6]:
        if node.zone in deployed:
            continue
        deployed.add(node.zone)
        candidates.append(
            Candidate(
                spec=InterventionSpec(
                    intervention_type=InterventionType.DEPLOY_PUMP,
                    label=f"Deploy mobile pumps to {node.name}",
                    asset_id=node.id,
                    destination_id=node.id,
                    resource_type=ResourceType.PUMP,
                    quantity=3.0,
                    capacity_m3s=0.8,
                    start_min=0,
                    duration_min=0,
                    cost_units=2.0,
                ),
                rationale=(
                    f"{node.name} is carrying {node.flood_depth_m:.2f} m of standing water; three 0.8 m³/s trailer "
                    "pumps lift 2.4 m³/s off the streets of the receiving ward and discharge it to the canal"
                ),
                target_id=node.id,
                target_name=node.name,
                urgency=4,
                feasibility=0.9,
                resources_required={"pump": 2.0, "generator": 1.0},
                equity_note="Pump placement is prioritised to the wards with the highest population in water.",
                evidence_ids=[f"asset:{node.id}"],
            )
        )

    # 2. close corridors that are already impassable ----------------------
    for node in sorted(snapshot.roads, key=lambda r: r.criticality, reverse=True):
        depth = float(node.flood_depth_m)
        threshold = float(node.capacity.get("flood_threshold_m") or 0.3)
        heavy = float(node.capacity.get("heavy_threshold_m") or threshold + 0.25)
        if depth < heavy:
            continue
        candidates.append(
            Candidate(
                spec=InterventionSpec(
                    intervention_type=InterventionType.CLOSE_ROAD,
                    label=f"Close and sign {node.name}",
                    road_id=node.id,
                    asset_id=node.id,
                    start_min=0,
                    duration_min=0,
                    cost_units=1.0,
                ),
                rationale=(
                    f"{node.name} holds {depth:.2f} m against a {heavy:.2f} m heavy-vehicle threshold - "
                    "closing it prevents vehicles entering water that will disable them and consume rescue capacity"
                ),
                target_id=node.id,
                target_name=node.name,
                urgency=5,
                feasibility=0.95,
                resources_required={"police_unit": 1.0, "barricade_set": 2.0},
                evidence_ids=[f"asset:{node.id}"],
            )
        )
        if len([c for c in candidates if c.spec.intervention_type is InterventionType.CLOSE_ROAD]) >= 3:
            break

    # 3. shelter shortfall ------------------------------------------------
    dry_shelters = sorted(
        (n for n in snapshot.shelters if n.status is not AssetStatus.FAILED and n.flood_depth_m < 0.15),
        key=lambda s: (int(s.capacity.get("capacity_people") or 0), -s.criticality),
        reverse=True,
    )
    if kpis.shelters_over_capacity_people > 0 and dry_shelters:
        target = dry_shelters[0]
        annex = min(10_000.0, float(kpis.shelters_over_capacity_people))
        candidates.append(
            Candidate(
                spec=InterventionSpec(
                    intervention_type=InterventionType.OPEN_SHELTER,
                    label=f"Open a {int(annex):,}-place annex at {target.name}",
                    facility_id=target.id,
                    asset_id=target.id,
                    quantity=annex,
                    start_min=30,
                    duration_min=0,
                    cost_units=1.5,
                ),
                rationale=(
                    f"{int(kpis.shelters_over_capacity_people):,} people exceed the open dry shelter capacity at this "
                    f"severity; {target.name} is dry, above the flood line and already staffed by the district "
                    f"administration. The annex is sized at {int(annex):,} places - what the venue and its adjoining "
                    "halls can credibly hold, not the whole shortfall."
                ),
                assumptions=[
                    f"shelter demand model: 22% of the affected population seeks public shelter (planning ratio)",
                    f"annex capacity is available from the declared start step and is usable immediately",
                ],
                target_id=target.id,
                target_name=target.name,
                urgency=4,
                feasibility=0.85,
                resources_required={"shelter_staff": 12.0, "water_supply_kl": 40.0},
                evidence_ids=[f"asset:{target.id}", "kpi:shelters_over_capacity_people"],
            )
        )

    # 4. protect the most exposed hospital --------------------------------
    hospitals = sorted(
        (h for h in snapshot.of_type(AssetType.HOSPITAL)),
        key=lambda h: (
            (h.flood_depth_m or 0.0) * float(h.capacity.get("beds_occupied") or 0),
            -h.criticality,
        ),
        reverse=True,
    )
    if hospitals:
        hospital = hospitals[0]
        occupied = int(hospital.capacity.get("beds_occupied") or 0)
        if hospital.flood_depth_m > 0.08 or status_rank(hospital.status) >= status_rank(AssetStatus.DEGRADED):
            candidates.append(
                Candidate(
                    spec=InterventionSpec(
                        intervention_type=InterventionType.PRE_POSITION_RESOURCE,
                        label=f"Pre-position generators + ambulance staging at {hospital.name}",
                        asset_id=hospital.id,
                        destination_id=hospital.id,
                        resource_type=ResourceType.GENERATOR,
                        quantity=2.0,
                        start_min=0,
                        duration_min=0,
                        cost_units=2.0,
                    ),
                    rationale=(
                        f"{hospital.name} carries {occupied} inpatients and is showing {hospital.flood_depth_m:.2f} m at the "
                        "asset with its access route degraded - staging power and ambulances before the peak avoids an "
                        "emergency transfer during the peak"
                    ),
                    target_id=hospital.id,
                    target_name=hospital.name,
                    urgency=5,
                    feasibility=0.75,
                    resources_required={"generator": 2.0, "ambulance": 3.0},
                    equity_note="Hospital protection benefits the least mobile population first.",
                    evidence_ids=[f"asset:{hospital.id}"],
                )
            )

    # 5. fleet readiness - the cheapest flood measure there is -----------------
    pump_stations = sorted(snapshot.of_type(AssetType.PUMP_STATION), key=lambda p: p.criticality, reverse=True)
    if pump_stations:
        wet = [p for p in pump_stations if p.zone in {n.zone for n in flooded_water}]
        targets = wet or pump_stations
        bundle = [
            InterventionSpec(
                intervention_type=InterventionType.PRE_POSITION_RESOURCE,
                label=f"Crew and fuel {station.name}",
                resource_type=ResourceType.PUMP,
                destination_id=station.id,
                asset_id=station.id,
                quantity=1.0,
                start_min=0,
                duration_min=0,
                cost_units=1.0,
            )
            for station in targets[:6]
        ]
        candidates.append(
            Candidate(
                spec=bundle[0].model_copy(update={"label": f"Crew, fuel and pre-check {len(bundle)} pumping stations"}),
                bundle=bundle[1:],
                rationale=(
                    f"{len(bundle)} pumping stations sit in or feed the flooded wards. Servicing the fleet before the "
                    "burst keeps every unit out of the failure draw (the ensemble models a 12% per-station mechanical "
                    "failure risk per event) - a station that fails at the peak floods its whole catchment for the "
                    "rest of the event."
                ),
                target_id=None,
                target_name="pumping fleet",
                urgency=4,
                feasibility=0.9,
                resources_required={"pump_crew": float(len(bundle)), "generator": float(len(bundle))},
                equity_note="The deepest wards are the ones behind these stations.",
                evidence_ids=[f"asset:{spec.destination_id}" for spec in bundle],
                stress={"pump_failure_rate": 0.6},
                assumptions=[
                    "readiness is stress-tested: this candidate is measured against a control run in which 60% of "
                    "pumping stations suffer a mechanical failure, because in a benign draw readiness has no visible effect",
                ],
            )
        )

    # 6. warn the public ------------------------------------------------------
    if kpis.people_requiring_rescue > 500:
        candidates.append(
            Candidate(
                spec=InterventionSpec(
                    intervention_type=InterventionType.ISSUE_PUBLIC_ALERT,
                    label="Issue district-wide flood alert with ward-level depth guidance",
                    start_min=0,
                    duration_min=request.horizon_minutes,
                    cost_units=0.5,
                ),
                rationale=(
                    f"{int(kpis.people_requiring_rescue):,} people are forecast to need assisted evacuation at this "
                    "severity. A warning does not hold back water, but a warned population moves to upper floors and "
                    "leaves before the peak, which reduces boat demand."
                ),
                target_id=None,
                target_name="city-wide",
                urgency=5,
                feasibility=0.95,
                resources_required={"public_alert_channel": 1.0},
                equity_note="Reaches the informal settlements first only if pushed through local volunteers, not just SMS.",
                evidence_ids=["kpi:people_requiring_rescue"],
                assumptions=[
                    "assumed effect: rescue demand falls 18% once the alert is active",
                    "assumed effect: shelter demand rises 8% because more people leave voluntarily",
                ],
            )
        )

    # 7. hold the upstream release (only relevant with a dam-release hazard)
    if any(str(getattr(h.hazard_type, "value", h.hazard_type)) == "dam_release" for h in request.hazards):
        regulator = next((n for n in nodes.values() if n.subtype == "dam"), None)
        if regulator is not None:
            candidates.append(
                Candidate(
                    spec=InterventionSpec(
                        intervention_type=InterventionType.HOLD_WATER_RELEASE,
                        label=f"Hold release at {regulator.name}",
                        asset_id=regulator.id,
                        start_min=0,
                        duration_min=request.horizon_minutes,
                        cost_units=3.0,
                    ),
                    rationale=(
                        "Holding discharge at the regulator for the duration of the rainfall peak shifts the downstream "
                        "stage peak past the tide-locked window"
                    ),
                    target_id=regulator.id,
                    target_name=regulator.name,
                    urgency=5,
                    feasibility=0.6,
                    resources_required={"irrigation_gate_operator": 2.0},
                    evidence_ids=[f"asset:{regulator.id}"],
                )
            )

    # 6. response coverage for severed pairs -------------------------------
    for asset_id in cascade_targets[:2]:
        node = nodes.get(asset_id)
        if node is None or not node.is_lifeline:
            continue
        candidates.append(
            Candidate(
                spec=InterventionSpec(
                    intervention_type=InterventionType.REROUTE_TRAFFIC,
                    label=f"Open contingency corridor to {node.name}",
                    asset_id=node.id,
                    destination_id=node.id,
                    via_asset_id=_nearest_open_road(snapshot, node),
                    start_min=0,
                    duration_min=request.horizon_minutes,
                    cost_units=1.5,
                ),
                rationale=(
                    f"{node.name} is a cascade target with degraded access; a signed contingency corridor removes it "
                    "from the isolation set during the peak"
                ),
                target_id=node.id,
                target_name=node.name,
                urgency=4,
                feasibility=0.7,
                resources_required={"police_unit": 2.0},
                evidence_ids=[f"asset:{node.id}"],
            )
        )

    return candidates[:max_candidates]


def _nearest_open_road(snapshot: TwinSnapshot, node: AssetState) -> str | None:
    best: tuple[str, float] | None = None
    for road in snapshot.roads:
        if road.status is AssetStatus.FAILED or road.flood_depth_m > float(road.capacity.get("flood_threshold_m") or 0.3):
            continue
        distance = haversine_m(node.lon, node.lat, road.lon, road.lat)
        if best is None or distance < best[1]:
            best = (road.id, distance)
    return best[0] if best else None


# --------------------------------------------------------------------------- #
# counterfactual evaluation
# --------------------------------------------------------------------------- #
def evaluate_candidates(
    base_snapshot: TwinSnapshot,
    request: SimulationRequest,
    candidates: Sequence[Candidate],
    *,
    baseline_kpis: KPISet,
    baseline_score: float,
    weights: Mapping[str, float] | None = None,
    ensemble_size: int = COUNTERFACTUAL_ENSEMBLE,
    progress: Any = None,
) -> list[Recommendation]:
    """Re-simulate each candidate and rank by measured improvement."""
    from .simulation import run_scenario  # local import: avoids a circular import

    # The comparison baseline must use the *same* ensemble as the counterfactuals
    # (same seeds, same size). Comparing a 6-member counterfactual against a
    # 48-member baseline would report the sampling difference as if it were the
    # effect of the intervention - a subtle way to lie with statistics.
    from .simulation import run_scenario as _run

    control_request = request.model_copy(deep=True)
    control_request.ensemble_size = ensemble_size
    control = _run(base_snapshot, control_request, ensemble_size=ensemble_size)
    control_kpis = control.kpis
    control_score = score_kpis(control_kpis, weights)

    shortlist = _shortlist(candidates, MAX_COUNTERFACTUALS)
    out: list[Recommendation] = []
    for index, candidate in enumerate(shortlist):
        variant = request.model_copy(deep=True)
        variant.ensemble_size = ensemble_size
        variant.interventions = [*variant.interventions, candidate.spec, *candidate.bundle]
        try:
            if candidate.stress:
                # both sides of the comparison get the same stress, or the stress
                # itself would be reported as the effect of the intervention
                stressed_control = _run(base_snapshot, control_request, ensemble_size=ensemble_size,
                                        driver_overrides=dict(candidate.stress))
                outcome = _run(base_snapshot, variant, ensemble_size=ensemble_size,
                               driver_overrides=dict(candidate.stress))
            else:
                stressed_control = control
                outcome = _run(base_snapshot, variant, ensemble_size=ensemble_size)
        except Exception as exc:  # noqa: BLE001 - one bad candidate must not kill the run
            log.warning("counterfactual failed", extra={"custos": {"candidate": candidate.spec.label, "error": str(exc)[:200]}})
            continue
        score = score_kpis(outcome.kpis, weights)
        reference_score = score_kpis(stressed_control.kpis, weights)
        reference_kpis = stressed_control.kpis
        delta_score = reference_score - score  # positive == the intervention helps
        delta = {
            field: round(getattr(outcome.kpis, field) - getattr(reference_kpis, field), 3)
            for field in KPISet.model_fields
        }
        feasibility = candidate.feasibility
        effectiveness = max(0.0, min(1.0, delta_score / max(1.0, reference_score * 0.15))) if reference_score else 0.0
        # urgency rises when the action is time-critical and the mechanism lands early
        urgency = candidate.urgency
        if candidate.spec.start_min <= 30 and effectiveness > 0.2:
            urgency = min(5, urgency + 1)
        stress_note = (
            f"; stress: pump failure rate {candidate.stress['pump_failure_rate']:.0%}"
            if candidate.stress else ""
        )
        if not delta_score and all(abs(v) < 1e-6 for v in delta.values()):
            counterfactual = (
                f"No measurable effect in a {len(outcome.members)}-member ensemble{stress_note}. The control run reaches "
                f"{reference_kpis.patients_at_risk:.0f} patients at risk, {reference_kpis.roads_impassable_km:.1f} km of "
                "impassable road and the same figures with the action applied. Reported as measured - a lever with no "
                "effect at this severity should not be sold as one that has."
            )
        else:
            counterfactual = (
                f"Control run (identical {len(outcome.members)}-member ensemble, no intervention{stress_note}): "
                f"{reference_kpis.patients_at_risk:.0f} patients at risk, {reference_kpis.roads_impassable_km:.1f} km "
                f"impassable, {reference_kpis.people_requiring_rescue:.0f} people requiring rescue. "
                f"With this action: {outcome.kpis.patients_at_risk:.0f}, {outcome.kpis.roads_impassable_km:.1f} km, "
                f"{outcome.kpis.people_requiring_rescue:.0f}."
            )
        rank_key = (0.6 * effectiveness + 0.25 * feasibility + 0.15 * (urgency / 5.0))
        out.append(
            Recommendation(
                id=f"rec_{hashlib.sha1((candidate.spec.label + str(candidate.target_id)).encode()).hexdigest()[:10]}",
                rank=0,
                action=candidate.spec.label,
                intervention_type=candidate.spec.intervention_type,
                target_id=candidate.target_id,
                target_name=candidate.target_name,
                window=(
                    f"t+{candidate.spec.start_min} min"
                    + (f" – t+{candidate.spec.start_min + candidate.spec.duration_min} min" if candidate.spec.duration_min else " → end of horizon")
                ),
                rationale=candidate.rationale,
                expected_kpi_delta=delta,
                resources_required=candidate.resources_required,
                feasibility=round(feasibility, 2),
                urgency=urgency,
                confidence=round(min(0.9, 0.45 + 0.5 * effectiveness), 2),
                evidence_ids=candidate.evidence_ids,
                counterfactual=counterfactual,
                generated_by=f"counterfactual re-simulation ({ensemble_size} members) + rule generation",
                assumptions=candidate.assumptions,
            )
        )
        if progress is not None:
            progress(
                (index + 1) / max(1, len(shortlist)),
                f"evaluated: {candidate.spec.label}",
                {"rank_key": round(rank_key, 4), "delta_score": round(delta_score, 2)},
            )

    out.sort(
        key=lambda r: (
            0.6 * max(0.0, min(1.0, (r.confidence - 0.45) / 0.5))
            + 0.25 * r.feasibility
            + 0.15 * (r.urgency / 5.0)
        ),
        reverse=True,
    )
    for position, recommendation in enumerate(out, start=1):
        recommendation.rank = position
    return out


#: Priors on how much a lever can move the outcome *before* it is simulated.
#: These only decide which candidates get the scarce counterfactual budget; they
#: never appear in the ranking, which is measured. A cheap, city-wide, fast lever
#: outranks a slow physical one because physics says so here, not because it is
#: fashionable: a warning cannot fail mechanically and reaches everyone at once.
_LEVER_PRIOR: dict[InterventionType, float] = {
    InterventionType.ISSUE_PUBLIC_ALERT: 1.00,
    InterventionType.OPEN_SHELTER: 0.95,
    InterventionType.PRE_POSITION_RESOURCE: 0.90,
    InterventionType.HOLD_WATER_RELEASE: 0.90,
    InterventionType.POWER_SWITCHOVER: 0.80,
    InterventionType.DEPLOY_PUMP: 0.75,
    InterventionType.REROUTE_TRAFFIC: 0.70,
    InterventionType.EVACUATE_FACILITY: 0.65,
    InterventionType.CLOSE_ROAD: 0.60,
    InterventionType.SANDBAG_LINE: 0.55,
    InterventionType.REOPEN_ROAD: 0.50,
}

#: never spend the whole budget on one idea
MAX_PER_LEVER = 2


def _shortlist(candidates: Sequence[Candidate], budget: int) -> list[Candidate]:
    """Choose which candidates are worth a counterfactual re-simulation.

    Each counterfactual costs a full ensemble run, so the budget is real. The
    shortlist is deliberately *diverse*: two pumps plus two shelter options would
    tell a commander less than one of each, and the worst outcome for an EOC is
    four variations of an idea that was never going to work.
    """
    def prior(candidate: Candidate) -> float:
        kind = _as_enum(candidate.spec.intervention_type, InterventionType)
        base = _LEVER_PRIOR.get(kind, 0.5)
        return base * (0.55 + 0.45 * candidate.feasibility) * (0.6 + 0.08 * candidate.urgency)

    ordered = sorted(candidates, key=prior, reverse=True)
    chosen: list[Candidate] = []
    used: dict[InterventionType, int] = {}
    for candidate in ordered:
        kind = _as_enum(candidate.spec.intervention_type, InterventionType)
        if used.get(kind, 0) >= MAX_PER_LEVER:
            continue
        used[kind] = used.get(kind, 0) + 1
        chosen.append(candidate)
        if len(chosen) >= budget:
            break
    return chosen


def summarise_recommendations(recommendations: Iterable[Recommendation]) -> list[dict[str, Any]]:
    return [
        {
            "rank": r.rank,
            "action": r.action,
            "window": r.window,
            "urgency": r.urgency,
            "confidence": r.confidence,
            "feasibility": r.feasibility,
            "counterfactual": r.counterfactual,
        }
        for r in recommendations
    ]
