"""Presentation metadata for every KPI the platform reports.

Kept next to the domain rather than in the frontend because *the direction of
good* is not a UI concern: whether "severed_response_pairs" going up is bad news
is part of the model's semantics, and a comparison verdict computed in the
browser could disagree with the one computed by the orchestrator. One table,
served by the API, rendered by the console.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

KpiCategory = Literal["people", "health", "mobility", "lifelines", "shelter", "equity", "severity", "resilience"]


@dataclass(frozen=True, slots=True)
class KpiMeta:
    label: str
    unit: str
    lower_is_better: bool
    category: KpiCategory
    description: str

    @property
    def direction(self) -> str:
        return "lower_is_better" if self.lower_is_better else "higher_is_better"


KPI_META: dict[str, KpiMeta] = {
    "patients_at_risk": KpiMeta("Patients at risk", "people", True, "health", "Inpatients in facilities that are flooded, unreachable or without power."),
    "hospital_capacity_lost_pct": KpiMeta("Hospital capacity lost", "%", True, "health", "Share of occupied hospital capacity that is impaired, isolated or evacuated."),
    "people_flooded": KpiMeta("People in flooded areas", "people", True, "people", "Population inside the flooded fraction of each ward."),
    "people_requiring_rescue": KpiMeta("People needing rescue", "people", True, "people", "Population in water deep enough to need assisted evacuation."),
    "population_power_outage": KpiMeta("People without power", "people", True, "lifelines", "Population served by failed or isolated substations."),
    "population_water_outage": KpiMeta("People without water", "people", True, "lifelines", "Population served by water treatment plants that stopped producing."),
    "telecom_coverage_lost_pct": KpiMeta("Telecom coverage lost", "%", True, "lifelines", "Share of population outside coverage of any healthy tower."),
    "shelters_over_capacity_people": KpiMeta("Shelter demand over capacity", "people", True, "shelter", "People who cannot be placed in a dry, reachable shelter at the declared intake ratio."),
    "roads_impassable_km": KpiMeta("Roads impassable", "km", True, "mobility", "Length of carriageway under more water than a light vehicle can cross."),
    "critical_roads_impassable": KpiMeta("Critical corridors impassable", "count", True, "mobility", "Class-A emergency corridors that are closed or flooded."),
    "avg_response_delay_min": KpiMeta("Mean response delay", "min", True, "mobility", "Travel-time penalty over dry conditions, averaged over reachable responder-to-facility pairs only."),
    "p90_response_delay_min": KpiMeta("P90 response delay", "min", True, "mobility", "90th percentile of the same reachable pairs - the tail the commander plans for."),
    "severed_response_pairs": KpiMeta("Severed response pairs", "pairs", True, "mobility", "Responder-to-facility pairs with no traversable corridor left. A different failure mode from a slow route, so it is never averaged into the delay."),
    "population_affected": KpiMeta("Population affected", "people", True, "people", "Population inside wards with a non-zero flood impact."),
    "equity_index": KpiMeta("Equity index", "index", False, "equity", "1.00 means the burden falls on the population in proportion to its size; lower means it concentrates on the least resilient wards."),
    "cascade_depth": KpiMeta("Cascade depth", "hops", True, "severity", "Longest dependency chain that failed end-to-end during the scenario."),
    "assets_failed": KpiMeta("Assets failed", "count", True, "severity", "Critical assets whose service level reached zero."),
    "assets_impaired": KpiMeta("Assets impaired", "count", True, "severity", "Critical assets degraded below full service without failing."),
    "evacuation_minutes": KpiMeta("Evacuation time", "min", True, "mobility", "Modelled time to clear the exposed population along usable corridors."),
    "max_flood_depth_m": KpiMeta("Peak flood depth", "m", True, "severity", "Deepest modelled standing water anywhere in the city."),
    "flooded_assets": KpiMeta("Assets in water", "count", True, "severity", "Assets with standing water above the reporting threshold."),
    "kpi_score": KpiMeta("Composite severity score", "score", True, "severity", "Weighted composite used to rank scenarios; shown so the ranking is never a black box."),
}


def label_for(kpi: str) -> str:
    meta = KPI_META.get(kpi)
    return meta.label if meta else kpi.replace("_", " ").title()


def unit_for(kpi: str) -> str:
    meta = KPI_META.get(kpi)
    return meta.unit if meta else ""


def lower_is_better(kpi: str) -> bool:
    meta = KPI_META.get(kpi)
    return meta.lower_is_better if meta else True


def as_payload() -> list[dict[str, object]]:
    """Serialisable metadata for the console's KPI dictionary."""
    return [
        {
            "id": key,
            "label": meta.label,
            "unit": meta.unit,
            "direction": meta.direction,
            "lower_is_better": meta.lower_is_better,
            "category": meta.category,
            "description": meta.description,
        }
        for key, meta in KPI_META.items()
    ]
