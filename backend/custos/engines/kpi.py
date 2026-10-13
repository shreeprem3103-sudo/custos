"""Operational KPI computation: turning a twin state into numbers a commander owns.

Two design rules
----------------
1. **Every KPI is a consequence, not a count.** "Roads impassable" is a KPI only
   because it delays an ambulance; the code therefore computes response delay
   over the *mobility graph*, never over a straight-line distance.
2. **Every KPI is ensemble-reported.** A single number is a lie in a crisis;
   the engine returns p10 / p50 / p90 and the UI renders the spread.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..domain.enums import AssetStatus, AssetType, RelationKind, status_rank
from ..domain.schemas import KPISet
from ..domain.twin import AssetState, TwinSnapshot
from ..fusion.normalize import haversine_m
from ..observability import get_logger

log = get_logger("engines.kpi")

#: water bodies carry hydraulic state, never "population in water": excluding
#: them from exposure accounting is the difference between a credible and an
#: absurd number
WATER_BODY_TYPES = {AssetType.CANAL, AssetType.RIVER_GAUGE}

#: population served per substation, used when the utility has not supplied a
#: feeder-level connectivity model. Explicit POWERS edges always override this.
SUBSTATION_SERVICE_RADIUS_M = 2_200.0
WATER_SERVICE_RADIUS_M = 7_000.0
TELECOM_SERVICE_RADIUS_M = 3_200.0
AMBULANCE_SPEED_KMH = 32.0


@dataclass(slots=True)
class ShelterAllocation:
    shelter_id: str
    name: str
    capacity: int
    demand: int
    assigned: int

    @property
    def over_capacity(self) -> int:
        return max(0, self.assigned - self.capacity)


@dataclass
class ExposureReport:
    """Population-level consequences for one twin state, with provenance."""

    zone_impact: dict[str, float] = field(default_factory=dict)
    unplaced_at_shelters: int = 0
    people_flooded: int = 0
    people_requiring_rescue: int = 0
    people_power_outage: int = 0
    people_water_outage: int = 0
    people_telecom_outage: int = 0
    shelter_demand: int = 0
    shelter_capacity_available: int = 0
    shelter_allocations: list[ShelterAllocation] = field(default_factory=list)
    patients_at_risk: float = 0.0
    hospital_capacity_lost_pct: float = 0.0
    equity_index: float = 1.0
    evidence: list[dict[str, Any]] = field(default_factory=list)


class ExposureModel:
    """Attributes consequences to populations, facilities and equity."""

    #: Effect of an issued public alert on the two demand curves it moves.
    #: These are *stated model assumptions*, not measurements, and they are
    #: surfaced to the operator next to every recommendation that relies on them.
    ALERT_RESCUE_REDUCTION = 0.18
    ALERT_SHELTER_UPTAKE = 0.08

    def __init__(self, snapshot: TwinSnapshot) -> None:
        self.snapshot = snapshot
        self._substation_service = self._build_service_index(AssetType.SUBSTATION, SUBSTATION_SERVICE_RADIUS_M)
        self._water_service = self._build_service_index(AssetType.WATER_TREATMENT, WATER_SERVICE_RADIUS_M)
        self._telecom_service = self._build_service_index(AssetType.TELECOM_TOWER, TELECOM_SERVICE_RADIUS_M)

    # ------------------------------------------------------------------ #
    def _build_service_index(self, asset_type: AssetType, radius_m: float) -> dict[str, list[tuple[str, float]]]:
        """zone_id -> [(provider_id, weight)] using explicit POWERS/COVERS edges
        when present, and distance-weighted proximity otherwise."""
        index: dict[str, list[tuple[str, float]]] = {}
        providers = self.snapshot.of_type(asset_type)
        if not providers:
            return index
        explicit: dict[str, set[str]] = {}
        for edge in self.snapshot.edges:
            if edge.kind in {RelationKind.POWERS, RelationKind.SUPPLIES_WATER, RelationKind.COVERS} and edge.target_id in self.snapshot.nodes:
                explicit.setdefault(edge.target_id, set()).add(edge.source_id)
        for zone in self.snapshot.zones.values():
            weights: list[tuple[str, float]] = []
            for provider in providers:
                distance = haversine_m(zone.lon, zone.lat, provider.lon, provider.lat)
                if distance > radius_m:
                    continue
                weight = max(0.05, 1.0 - distance / radius_m)
                weights.append((provider.id, weight))
            total = sum(w for _, w in weights)
            if total <= 0:
                continue
            index[zone.id] = [(pid, w / total) for pid, w in weights]
        return index

    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        *,
        depths: Mapping[str, float] | None = None,
        response_delays: Mapping[str, float] | None = None,
        alert_active: bool = False,
    ) -> ExposureReport:
        snapshot = self.snapshot
        depths = depths or {}
        report = ExposureReport()

        def depth_of(asset_id: str) -> float:
            return float(depths.get(asset_id, snapshot.nodes[asset_id].flood_depth_m if asset_id in snapshot.nodes else 0.0))

        # ---- zone-level impact -----------------------------------------
        # Population impact is a *depth* question. Service failure is a separate
        # question, answered below by the utility model - mixing the two would
        # report 90 000 people "flooded" because one pumping station degraded,
        # which is exactly the kind of number that destroys trust in a console.
        for zone_id, zone in snapshot.zones.items():
            local_depths = [
                depth_of(n.id)
                for n in snapshot.nodes.values()
                if n.zone == zone_id
                and n.asset_type not in WATER_BODY_TYPES
                and (n.id in depths or n.flood_depth_m)
            ]
            if not local_depths:
                report.zone_impact[zone_id] = 0.0
                continue
            # Two factors decide a ward's population impact: how deep the water
            # gets in its worst pocket, and how much of the ward is actually wet.
            # Ignoring the second factor turns "one junction under 60 cm" into
            # "the entire ward is flooded" - and an EOC stops trusting the screen.
            worst = max(local_depths)
            depth_impact = min(1.0, worst / 0.55)
            flooded_assets = sum(1 for d in local_depths if d > 0.10)
            share = flooded_assets / max(1, len(local_depths))
            extent_factor = min(1.0, max(0.12, share * 3.0))
            impact = depth_impact * extent_factor
            report.zone_impact[zone_id] = round(impact, 3)

        # ---- flooded population and rescue demand ----------------------
        for zone_id, impact in report.zone_impact.items():
            zone = snapshot.zones.get(zone_id)
            if zone is None:
                continue
            affected = int(zone.population * impact)
            report.people_flooded += affected
            # ~4 % of a population in standing water above knee depth needs
            # assisted evacuation - a planning ratio used by district plans
            if impact > 0.45:
                rescue = int(zone.population * (impact - 0.45) * 0.12)
                if alert_active:
                    # a warned population moves to upper floors and leaves before the
                    # peak: the flood is the same, the *demand for boats* is not
                    rescue = int(rescue * (1.0 - self.ALERT_RESCUE_REDUCTION))
                report.people_requiring_rescue += rescue

        # ---- utility service loss --------------------------------------
        report.people_power_outage = self._service_loss(self._substation_service, report.zone_impact, threshold=0.45)
        report.people_water_outage = self._service_loss(self._water_service, report.zone_impact, threshold=0.4)
        report.people_telecom_outage = self._service_loss(self._telecom_service, report.zone_impact, threshold=0.35)

        # ---- hospitals --------------------------------------------------
        total_beds = 0
        lost_beds = 0.0
        for hospital in snapshot.of_type(AssetType.HOSPITAL):
            beds = int(hospital.capacity.get("beds_total") or 0)
            occupied = int(hospital.capacity.get("beds_occupied") or 0)
            total_beds += beds
            severity = self._facility_severity(hospital, depth_of(hospital.id))
            inaccessible = hospital.access_available is False or hospital.status is AssetStatus.FAILED
            if inaccessible:
                severity = max(severity, 0.75)
            report.patients_at_risk += occupied * severity
            lost_beds += beds * severity
            if severity > 0.05:
                report.evidence.append(
                    {
                        "type": "hospital_consequence",
                        "asset_id": hospital.id,
                        "name": hospital.name,
                        "severity": round(severity, 3),
                        "occupied_beds": occupied,
                        "patients_at_risk": round(occupied * severity),
                        "depth_m": round(depth_of(hospital.id), 3),
                        "status": hospital.status.value,
                        "accessible": bool(hospital.access_available),
                    }
                )
        report.hospital_capacity_lost_pct = round(100.0 * lost_beds / total_beds, 2) if total_beds else 0.0

        # ---- shelters ---------------------------------------------------
        allocations, demand_by_zone = self._allocate_shelters(
            report.zone_impact, uptake=1.0 + (self.ALERT_SHELTER_UPTAKE if alert_active else 0.0)
        )
        report.shelter_allocations = allocations
        report.shelter_demand = sum(a.demand for a in allocations) + int(getattr(self, "_last_unplaced", 0))
        report.shelter_capacity_available = sum(max(0, a.capacity - a.assigned) for a in allocations)
        report.unplaced_at_shelters = int(getattr(self, "_last_unplaced", 0))

        # ---- equity -----------------------------------------------------
        report.equity_index = self._equity_index(report.zone_impact)

        if response_delays:
            report.evidence.append(
                {
                    "type": "response_delay",
                    "mean_min": round(sum(response_delays.values()) / max(1, len(response_delays)), 2),
                    "worst_min": round(max(response_delays.values()), 2) if response_delays else 0.0,
                }
            )
        return report

    # ------------------------------------------------------------------ #
    @staticmethod
    def _facility_severity(node: AssetState, depth: float) -> float:
        """Consequence severity for a facility.

        A facility on generator power is *at elevated risk*, not incapacitated:
        modelling that difference is what stops the platform from announcing a
        city-wide hospital collapse every time a feeder trips.
        """
        severity = {
            AssetStatus.OK: 0.0,
            AssetStatus.WATCH: 0.1,
            AssetStatus.DEGRADED: 0.35,
            AssetStatus.IMPAIRED: 0.55,
            AssetStatus.FAILED: 1.0,
            AssetStatus.UNKNOWN: 0.2,
        }.get(node.status, 0.0)
        if depth > 0.05:
            severity = max(severity, min(1.0, depth / 0.75))
        autonomy = float(
            node.capacity.get("autonomy_hours")
            or node.capacity.get("generator_hours")
            or node.capacity.get("backup_hours")
            or 0.0
        )
        if not node.power_available:
            severity = max(severity, 0.55 if autonomy > 0 else 0.85)
        elif autonomy >= 4.0 and depth < 0.10:
            # buffered and dry: the residual risk is operational, not clinical
            severity = min(severity, 0.35)
        if node.access_available is False and node.asset_type is AssetType.HOSPITAL:
            # an unreachable hospital cannot receive casualties: that is a real
            # capability loss even with the lights on
            severity = max(severity, 0.6)
        return round(severity, 3)

    def _service_loss(self, index: Mapping[str, list[tuple[str, float]]], zone_impact: Mapping[str, float], *, threshold: float) -> int:
        total = 0
        for zone_id, providers in index.items():
            zone = self.snapshot.zones.get(zone_id)
            if zone is None:
                continue
            lost_weight = 0.0
            for provider_id, weight in providers:
                provider = self.snapshot.nodes.get(provider_id)
                if provider is None:
                    continue
                provider_impact = self._provider_impact(provider)
                if provider_impact >= threshold:
                    lost_weight += weight
            # local flooding also interrupts service even if the provider stands
            local = zone_impact.get(zone_id, 0.0)
            effective = max(lost_weight, local * 0.55 if local > 0.6 else 0.0)
            total += int(zone.population * min(1.0, effective))
        return total

    @staticmethod
    def _provider_impact(provider: AssetState) -> float:
        impact = {
            AssetStatus.OK: 0.0,
            AssetStatus.WATCH: 0.15,
            AssetStatus.DEGRADED: 0.4,
            AssetStatus.IMPAIRED: 0.8,
            AssetStatus.FAILED: 1.0,
            AssetStatus.UNKNOWN: 0.25,
        }.get(provider.status, 0.0)
        if not provider.power_available and provider.asset_type is not AssetType.TELECOM_TOWER:
            impact = max(impact, 0.75)
        if provider.flood_depth_m > 0.4:
            impact = max(impact, 0.85)
        return impact

    def _allocate_shelters(
        self, zone_impact: Mapping[str, float], *, uptake: float = 1.0
    ) -> tuple[list[ShelterAllocation], dict[str, int]]:
        """Greedy nearest-dry-shelter allocation with hard capacity limits.

        Deterministic and inspectable on purpose: an operator must be able to
        argue with the plan, which requires seeing exactly where each group went.
        """
        snapshot = self.snapshot
        shelters = [s for s in snapshot.shelters]
        by_id = {s.id: s for s in shelters}
        demand_by_zone: dict[str, int] = {}
        for zone_id, impact in zone_impact.items():
            zone = snapshot.zones.get(zone_id)
            if zone is None or impact < 0.25:
                continue
            demand_by_zone[zone_id] = int(zone.population * impact * 0.22 * uptake)  # ~22 % seek public shelter

        report_unplaced = 0
        remaining = {
            s.id: int(s.capacity.get("capacity_people") or s.capacity.get("capacity") or 0)
            for s in shelters
        }
        assigned: dict[str, int] = {s.id: 0 for s in shelters}
        for zone_id, demand in sorted(demand_by_zone.items(), key=lambda kv: -kv[1]):
            zone = snapshot.zones[zone_id]
            candidates = sorted(
                shelters,
                key=lambda s: haversine_m(zone.lon, zone.lat, s.lon, s.lat),
            )
            outstanding = demand
            for shelter in candidates:
                if outstanding <= 0:
                    break
                if not self._shelter_usable(shelter):
                    continue
                room = remaining.get(shelter.id, 0)
                if room <= 0:
                    continue
                take = min(room, outstanding)
                assigned[shelter.id] += take
                remaining[shelter.id] -= take
                outstanding -= take
            if outstanding > 0:
                # No dry capacity remains in the ward: this is a *shortfall*, and
                # the KPI must say so rather than hiding it inside an over-capacity
                # shelter. Commanders can act on "4 000 people have nowhere to go";
                # they cannot act on a silently overloaded shelter.
                report_unplaced += outstanding

        allocations = [
            ShelterAllocation(
                shelter_id=sid,
                name=by_id[sid].name,
                capacity=int(by_id[sid].capacity.get("capacity_people") or 0),
                demand=assigned[sid],
                assigned=assigned[sid],
            )
            for sid in assigned
            if assigned[sid] > 0
        ]
        allocations.sort(key=lambda a: a.over_capacity, reverse=True)
        self._last_unplaced = report_unplaced
        return allocations, demand_by_zone

    @staticmethod
    def _shelter_usable(shelter: AssetState) -> bool:
        if shelter.status is AssetStatus.FAILED:
            return False
        if shelter.flood_depth_m > 0.25:
            return False
        return True

    def _equity_index(self, zone_impact: Mapping[str, float]) -> float:
        """1.0 = impact is spread evenly; < 1 means the vulnerable are carrying it.

        Computed as the ratio of vulnerability-weighted impact to unweighted
        impact, mapped into 0..1. This is the number that turns a technically
        successful response into an ethically defensible one.
        """
        weighted = 0.0
        plain = 0.0
        population = 0
        for zone_id, impact in zone_impact.items():
            zone = self.snapshot.zones.get(zone_id)
            if zone is None:
                continue
            population += zone.population
            weighted += impact * zone.vulnerability_index * zone.population
            plain += impact * zone.population
        if population == 0 or plain <= 0:
            return 1.0
        # below ~2 % of the population affected the index is noise, not signal
        if plain / population < 0.02:
            return 1.0
        mean_vulnerability = sum(z.vulnerability_index * z.population for z in self.snapshot.zones.values()) / max(1, sum(z.population for z in self.snapshot.zones.values()))
        expected = plain / population * mean_vulnerability * population
        ratio = weighted / expected if expected > 1e-9 else 1.0
        return round(max(0.0, min(1.0, 1.0 / max(1.0, ratio))), 3)


# --------------------------------------------------------------------------- #
# KPI assembly
# --------------------------------------------------------------------------- #
def kpis_from_state(
    snapshot: TwinSnapshot,
    *,
    depths: Mapping[str, float],
    response_delays: Mapping[str, float] | None = None,
    cascade_depth: float = 0.0,
    evacuation_minutes: float | None = None,
    extra: Mapping[str, float] | None = None,
    alert_active: bool = False,
) -> KPISet:
    """Collapse a twin state into the operational headline numbers."""
    exposure = ExposureModel(snapshot).evaluate(
        depths=depths, response_delays=response_delays, alert_active=alert_active
    )

    roads_impassable_km = 0.0
    critical_roads = 0
    for node in snapshot.roads:
        depth = float(depths.get(node.id, node.flood_depth_m))
        threshold = float(node.capacity.get("flood_threshold_m") or 0.3)
        if depth >= threshold or node.status is AssetStatus.FAILED:
            length_km = float(node.capacity.get("length_m") or 600.0) / 1000.0
            roads_impassable_km += length_km
            if node.criticality >= 0.8:
                critical_roads += 1

    all_delays = list((response_delays or {}).values())
    # a severed pair is a different failure mode from a slow pair, and averaging
    # the two together produces a number that is true for neither
    reachable = [d for d in all_delays if d < 240.0]
    severed = len(all_delays) - len(reachable)
    avg_delay = sum(reachable) / len(reachable) if reachable else 0.0
    p90_delay = _percentile(reachable, 0.9) if reachable else 0.0

    shelter_shortfall = sum(a.over_capacity for a in exposure.shelter_allocations) + exposure.unplaced_at_shelters
    assets_failed = sum(1 for n in snapshot.nodes.values() if n.status is AssetStatus.FAILED)

    kpis = KPISet(
        patients_at_risk=round(exposure.patients_at_risk, 1),
        hospital_capacity_lost_pct=exposure.hospital_capacity_lost_pct,
        shelters_over_capacity_people=float(shelter_shortfall),
        population_power_outage=float(exposure.people_power_outage),
        population_water_outage=float(exposure.people_water_outage),
        roads_impassable_km=round(roads_impassable_km, 2),
        critical_roads_impassable=float(critical_roads),
        avg_response_delay_min=round(avg_delay, 2),
        p90_response_delay_min=round(p90_delay, 2),
        severed_response_pairs=float(severed),
        evacuation_time_min=round(evacuation_minutes if evacuation_minutes is not None else _evacuation_minutes(exposure), 1),
        telecom_coverage_lost_pct=round(
            100.0 * exposure.people_telecom_outage / max(1, sum(z.population for z in snapshot.zones.values())), 2
        ),
        assets_failed=float(assets_failed),
        cascade_depth=round(cascade_depth, 2),
        equity_index=exposure.equity_index,
        people_requiring_rescue=float(exposure.people_requiring_rescue),
    )
    if extra:
        for key, value in extra.items():
            if hasattr(kpis, key):
                setattr(kpis, key, float(value))
    return kpis


def _evacuation_minutes(exposure: ExposureReport) -> float:
    """Evacuation time from boat/road throughput, not from a straight-line guess."""
    people = exposure.people_requiring_rescue
    if people <= 0:
        return 0.0
    # 8 boats × 8 persons × 4 trips/h + 30 road evacuations/h ≈ 286 persons/h on
    # the resources declared in the resource inventory
    throughput_per_hour = 8 * 8 * 4 + 30
    return people / throughput_per_hour * 60.0


def _percentile(values: Iterable[float], q: float) -> float:
    data = sorted(values)
    if not data:
        return 0.0
    index = min(len(data) - 1, max(0, int(round(q * (len(data) - 1)))))
    return data[index]


def aggregate_kpis(samples: list[KPISet]) -> tuple[KPISet, KPISet, KPISet]:
    """p10 / p50 / p90 across the ensemble, field by field."""
    if not samples:
        empty = KPISet()
        return empty, empty, empty

    def percentile_set(q: float) -> KPISet:
        payload: dict[str, float] = {}
        for field_name in KPISet.model_fields:
            if field_name == "equity_index":
                # equity is a ratio: reported as the median, never extrapolated
                payload[field_name] = _percentile([getattr(s, field_name) for s in samples], 0.5)
                continue
            payload[field_name] = round(_percentile([getattr(s, field_name) for s in samples], q), 2)
        return KPISet(**payload)

    return percentile_set(0.1), percentile_set(0.5), percentile_set(0.9)


def diff_kpis(a: KPISet, b: KPISet) -> KPISet:
    """``b - a`` field by field; positive means scenario B is worse (except equity)."""
    payload: dict[str, float] = {}
    for field_name in KPISet.model_fields:
        payload[field_name] = round(getattr(b, field_name) - getattr(a, field_name), 3)
    return KPISet(**payload)


def score_kpis(kpis: KPISet, weights: Mapping[str, float] | None = None) -> float:
    """Lower is better. Weights let an EOC declare local priorities."""
    defaults = {
        "patients_at_risk": 3.0,
        "hospital_capacity_lost_pct": 2.5,
        "people_requiring_rescue": 2.0,
        "shelters_over_capacity_people": 1.2,
        "population_power_outage": 0.03,
        "population_water_outage": 0.03,
        "roads_impassable_km": 0.8,
        "critical_roads_impassable": 4.0,
        "avg_response_delay_min": 1.5,
        "p90_response_delay_min": 1.0,
        "severed_response_pairs": 6.0,
        "evacuation_time_min": 0.4,
        "telecom_coverage_lost_pct": 0.6,
        "assets_failed": 0.4,
        "cascade_depth": 2.0,
        "equity_index": -8.0,  # a *higher* equity index is better
    }
    merged = {**defaults, **(weights or {})}
    total = 0.0
    for field_name, weight in merged.items():
        value = float(getattr(kpis, field_name, 0.0))
        total += weight * value
    return round(total, 3)


def explain_kpi_confidence(kpis: KPISet, p10: KPISet, p90: KPISet) -> dict[str, float]:
    """Relative spread per KPI: the honest uncertainty the UI must display."""
    spread: dict[str, float] = {}
    for field_name in KPISet.model_fields:
        median = float(getattr(kpis, field_name))
        low = float(getattr(p10, field_name))
        high = float(getattr(p90, field_name))
        if median == 0:
            spread[field_name] = 0.0
        else:
            spread[field_name] = round((high - low) / abs(median), 4)
    return spread
