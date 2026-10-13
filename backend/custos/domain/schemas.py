"""Pydantic v2 contract objects.

These models are the *only* shapes that cross the API boundary. Storage rows and
engine internals never leak outward, which keeps the "explainability" promises
(every number carries provenance) enforceable in one place.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import (
    AssetStatus,
    AssetType,
    ConfidenceBand,
    EventKind,
    FailureMode,
    FreshnessBand,
    HazardType,
    IncidentCategory,
    IncidentStatus,
    InterventionType,
    OperatingMode,
    RelationKind,
    ResourceType,
    Role,
    TaskStatus,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, use_enum_values=True, extra="forbid")


# --------------------------------------------------------------------------- #
# geometry & provenance
# --------------------------------------------------------------------------- #
class GeoPoint(ApiModel):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)

    def as_xy(self) -> tuple[float, float]:
        return (self.lon, self.lat)


class Provenance(ApiModel):
    """Answers the four questions an EOC always asks about a number."""

    source: str = Field(description="Connector or operator that produced the value")
    source_id: str | None = None
    observed_at: datetime
    ingested_at: datetime | None = None
    method: str = Field(default="direct", description="direct | derived | inferred | fused | operator")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)

    @property
    def age_seconds(self) -> float:  # pragma: no cover - helper
        import datetime as _dt

        now = _dt.datetime.now(_dt.timezone.utc)
        obs = self.observed_at
        if obs.tzinfo is None:
            obs = obs.replace(tzinfo=_dt.timezone.utc)
        return max(0.0, (now - obs).total_seconds())


class EvidenceItem(ApiModel):
    """A single, addressable, machine-checkable fact.

    Anything an AI stage says must point at one of these; the grounding
    verifier rejects statements that do not.
    """

    id: str
    kind: Literal[
        "sensor_reading",
        "asset_state",
        "asset_attribute",
        "relationship",
        "incident",
        "weather",
        "utility",
        "citizen_report",
        "model_output",
        "operator_input",
        "graph_path",
        "historical",
    ]
    source: str
    label: str
    observed_at: datetime
    value: float | int | str | bool | None = None
    unit: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    staleness_s: float | None = None
    location: GeoPoint | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def freshness(self) -> FreshnessBand:
        from .enums import freshness_band

        return freshness_band(self.staleness_s)


class Explainability(ApiModel):
    confidence: float = Field(ge=0.0, le=1.0)
    band: ConfidenceBand
    model: str
    method: str
    data_freshness: FreshnessBand
    assumption_list: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    uncertainty: dict[str, float] = Field(default_factory=dict)
    counterfactuals: list[str] = Field(default_factory=list)
    grounded: bool = True
    notes: str | None = None


# --------------------------------------------------------------------------- #
# assets & relationships
# --------------------------------------------------------------------------- #
class AssetBase(ApiModel):
    name: str
    asset_type: AssetType
    subtype: str | None = None
    criticality: float = Field(default=0.5, ge=0.0, le=1.0, description="0..1 consequence weight")
    elevation_m: float | None = Field(default=None, description="ground elevation AMSL, metres")
    geometry: dict[str, Any] = Field(description="GeoJSON geometry (Point/LineString/Polygon)")
    centroid: GeoPoint
    operator: str | None = None
    zone: str | None = None
    capacity: dict[str, Any] = Field(default_factory=dict)
    attributes: dict[str, Any] = Field(default_factory=dict)


class AssetCreate(AssetBase):
    id: str | None = None


class AssetOut(AssetBase):
    id: str
    status: AssetStatus
    flood_depth_m: float = 0.0
    power_available: bool = True
    access_available: bool = True
    service_level: float = Field(default=1.0, ge=0.0, le=1.0)
    exposure_score: float = Field(default=0.0, ge=0.0, le=1.0)
    provenance: Provenance | None = None
    updated_at: datetime


class RelationshipCreate(ApiModel):
    source_id: str
    target_id: str
    kind: RelationKind
    criticality: float = Field(default=0.5, ge=0.0, le=1.0)
    lead_time_min: int = Field(default=0, ge=0, le=2880)
    attenuation: float = Field(default=1.0, ge=0.0, le=1.0, description="fraction of parent impact transmitted")
    failure_mode: FailureMode = FailureMode.IMMEDIATE
    recovery_hours: float = Field(default=6.0, ge=0.0)
    redundancy_group: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class RelationshipOut(RelationshipCreate):
    id: str
    source_name: str | None = None
    target_name: str | None = None
    source_type: AssetType | None = None
    target_type: AssetType | None = None
    active: bool = True


class SensorReadingIn(ApiModel):
    sensor_id: str
    metric: str
    value: float
    unit: str = ""
    observed_at: datetime
    quality: float = Field(default=1.0, ge=0.0, le=1.0)
    raw: dict[str, Any] = Field(default_factory=dict)


class SensorReadingOut(SensorReadingIn):
    id: str
    ingested_at: datetime
    dedupe_key: str
    source: str


# --------------------------------------------------------------------------- #
# incidents
# --------------------------------------------------------------------------- #
class IncidentTimelineEntry(ApiModel):
    at: datetime
    stage: str
    actor: str
    detail: str
    evidence_ids: list[str] = Field(default_factory=list)


class IncidentBase(ApiModel):
    title: str
    category: IncidentCategory
    severity: int = Field(ge=1, le=5, description="1 = minor, 5 = catastrophic")
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    summary: str = ""
    location: GeoPoint
    zone: str | None = None
    affected_asset_ids: list[str] = Field(default_factory=list)
    population_exposed: int = Field(default=0, ge=0)
    reported_by: str = "system"


class IncidentCreate(IncidentBase):
    detected_at: datetime | None = None


class IncidentOut(IncidentBase):
    id: str
    status: IncidentStatus
    detected_at: datetime
    updated_at: datetime
    timeline: list[IncidentTimelineEntry] = Field(default_factory=list)
    explainability: Explainability | None = None
    simulation_ids: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# what-if simulation
# --------------------------------------------------------------------------- #
class HazardSpec(ApiModel):
    hazard_type: HazardType
    label: str = ""
    asset_id: str | None = None
    location: GeoPoint | None = None
    radius_m: float = Field(default=600.0, gt=0, le=20_000)
    depth_m: float | None = Field(default=None, ge=0, le=15)
    intensity_mm_h: float | None = Field(default=None, ge=0, le=500)
    discharge_m3s: float | None = Field(default=None, ge=0, le=10_000)
    surge_m: float | None = Field(default=None, ge=0, le=10)
    start_min: int = Field(default=0, ge=0, le=4320)
    duration_min: int = Field(default=240, gt=0, le=4320)
    probability: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _needs_target(self) -> "HazardSpec":
        # a tidal surge is a city-wide boundary condition and needs no target;
        # every other hazard must be anchored to an asset or a coordinate
        hazard_kind = str(getattr(self.hazard_type, "value", self.hazard_type))
        if hazard_kind != HazardType.TIDAL_SURGE.value and self.asset_id is None and self.location is None:
            raise ValueError("a hazard needs either asset_id or location")
        if self.start_min + self.duration_min > 4320:
            raise ValueError("hazard window extends beyond the 72 h simulation limit")
        return self


class AssetOverride(ApiModel):
    asset_id: str
    status: AssetStatus | None = None
    power_available: bool | None = None
    access_available: bool | None = None
    service_level: float | None = Field(default=None, ge=0.0, le=1.0)
    flood_depth_m: float | None = Field(default=None, ge=0, le=15)
    note: str = "operator override"


class InterventionSpec(ApiModel):
    intervention_type: InterventionType
    label: str = ""
    asset_id: str | None = None
    facility_id: str | None = None
    road_id: str | None = None
    resource_type: ResourceType | None = None
    quantity: float = Field(default=1.0, gt=0)
    capacity_m3s: float | None = Field(default=None, ge=0, le=100)
    start_min: int = Field(default=0, ge=0, le=4320)
    duration_min: int = Field(default=0, ge=0, le=4320, description="0 = permanent for the horizon")
    destination_id: str | None = None
    via_asset_id: str | None = None
    cost_units: float = Field(default=1.0, ge=0)


class SimulationRequest(ApiModel):
    name: str = Field(min_length=3, max_length=160)
    description: str = ""
    horizon_minutes: int = Field(default=360, ge=15, le=4320)
    timestep_minutes: int = Field(default=15, ge=1, le=120)
    ensemble_size: int = Field(default=48, ge=1, le=400)
    random_seed: int | None = None
    hazards: list[HazardSpec] = Field(default_factory=list, max_length=24)
    asset_overrides: list[AssetOverride] = Field(default_factory=list, max_length=128)
    interventions: list[InterventionSpec] = Field(default_factory=list, max_length=64)
    baseline_simulation_id: str | None = Field(
        default=None, description="set to compare against a stored run (or the live baseline)"
    )
    tags: list[str] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _bounds(self) -> "SimulationRequest":
        from ..config import settings as _s

        if self.ensemble_size > _s.sim_max_ensemble_size:
            raise ValueError(f"ensemble_size exceeds server maximum {_s.sim_max_ensemble_size}")
        if self.timestep_minutes > self.horizon_minutes:
            raise ValueError("timestep_minutes cannot exceed horizon_minutes")
        if not self.hazards and not self.asset_overrides and not self.interventions:
            raise ValueError("a scenario must contain at least one hazard, override or intervention")
        return self

    @property
    def step_count(self) -> int:
        return max(1, self.horizon_minutes // self.timestep_minutes)


class KPISet(ApiModel):
    """Operational headline numbers. Every field is a *mean over the ensemble*."""

    patients_at_risk: float = 0.0
    hospital_capacity_lost_pct: float = 0.0
    shelters_over_capacity_people: float = 0.0
    population_power_outage: float = 0.0
    population_water_outage: float = 0.0
    roads_impassable_km: float = 0.0
    critical_roads_impassable: float = 0.0
    avg_response_delay_min: float = 0.0
    p90_response_delay_min: float = 0.0
    severed_response_pairs: float = 0.0
    evacuation_time_min: float = 0.0
    telecom_coverage_lost_pct: float = 0.0
    assets_failed: float = 0.0
    cascade_depth: float = 0.0
    equity_index: float = Field(default=1.0, description="1.0 = impact evenly spread across zones")
    people_requiring_rescue: float = 0.0


class KPIUncertainty(ApiModel):
    p10: KPISet
    p50: KPISet
    p90: KPISet


class CascadeImpact(ApiModel):
    timestep_min: int
    cause_asset_id: str
    cause_asset_name: str
    target_asset_id: str
    target_asset_name: str
    target_type: AssetType
    mechanism: str
    relation_kind: RelationKind | None = None
    lead_time_min: int = 0
    magnitude: float = Field(ge=0.0, le=1.0)
    probability: float = Field(ge=0.0, le=1.0)
    status_after: AssetStatus
    hop: int = 1
    evidence_ids: list[str] = Field(default_factory=list)
    narrative: str = ""


class RoadState(ApiModel):
    asset_id: str
    name: str
    status: AssetStatus
    depth_m: float = 0.0
    impassable_from_min: int | None = None
    impassable_until_min: int | None = None
    detour_eta_penalty_min: float = 0.0


class FacilityState(ApiModel):
    asset_id: str
    name: str
    asset_type: AssetType
    status: AssetStatus
    service_level: float
    power_available: bool
    hours_a_autonomy: float = 0.0
    accessible: bool = True
    occupancy_or_load: float = 0.0
    capacity: float = 0.0
    note: str = ""


class ShelterState(ApiModel):
    asset_id: str
    name: str
    capacity: int
    occupancy: int
    over_capacity: int = 0
    status: AssetStatus = AssetStatus.OK


class MobilityState(ApiModel):
    avg_response_delay_min: float = 0.0
    p90_response_delay_min: float = 0.0
    severed_response_pairs: float = 0.0
    impassable_roads: list[str] = Field(default_factory=list)
    isolation_events: list[dict[str, Any]] = Field(default_factory=list)


class TimelineFrame(ApiModel):
    t_min: int
    label: str
    kpis: KPISet
    active_incidents: int = 0
    flood_extent_km2: float = 0.0
    max_depth_m: float = 0.0
    critical_failures: list[str] = Field(default_factory=list)


class Recommendation(ApiModel):
    id: str
    rank: int
    action: str
    intervention_type: InterventionType
    target_id: str | None = None
    target_name: str = ""
    window: str = ""
    rationale: str = ""
    expected_kpi_delta: dict[str, float] = Field(default_factory=dict)
    resources_required: dict[str, float] = Field(default_factory=dict)
    feasibility: float = Field(default=0.8, ge=0.0, le=1.0)
    urgency: int = Field(default=3, ge=1, le=5)
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)
    counterfactual: str = ""
    generated_by: str = "rules+cascade"
    assumptions: list[str] = Field(
        default_factory=list,
        description="stated model assumptions this recommendation depends on - shown to the operator, never hidden",
    )


class DriverSensitivity(ApiModel):
    driver: str
    low_kpi: float
    high_kpi: float
    swing: float


class SimulationSummary(ApiModel):
    id: str
    name: str
    status: str
    created_at: datetime
    completed_at: datetime | None = None
    horizon_minutes: int
    timestep_minutes: int
    ensemble_size: int
    created_by: str = "system"
    severity_delta: float = 0.0
    headline: str = ""
    progress: float = Field(default=0.0, ge=0.0, le=1.0)


class SimulationResult(ApiModel):
    id: str
    request: SimulationRequest
    status: str
    created_at: datetime
    completed_at: datetime | None = None
    runtime_ms: int = 0
    kpis: KPISet
    uncertainty: KPIUncertainty
    timeline: list[TimelineFrame]
    cascade: list[CascadeImpact]
    roads: list[RoadState]
    facilities: list[FacilityState]
    shelters: list[ShelterState]
    mobility: MobilityState
    recommendations: list[Recommendation]
    sensitivity: list[DriverSensitivity]
    explainability: Explainability
    baseline_id: str | None = None
    baseline_kpis: KPISet | None = None
    delta: KPISet | None = None
    narrative: str = ""
    determinism_token: str = ""
    degraded: bool = False


class SimulationRunAccepted(ApiModel):
    id: str
    status: str
    poll_url: str
    stream_url: str


# --------------------------------------------------------------------------- #
# scenario comparison
# --------------------------------------------------------------------------- #
class ComparisonRequest(ApiModel):
    scenario_a_id: str
    scenario_b_id: str
    weights: dict[str, float] = Field(default_factory=dict, description="KPI id -> relative weight")


class ComparisonRow(ApiModel):
    kpi: str
    label: str
    unit: str
    a: float
    b: float
    delta: float
    delta_pct: float
    winner: Literal["a", "b", "tie"]
    lower_is_better: bool
    band_a: tuple[float, float] | None = None
    band_b: tuple[float, float] | None = None
    material: bool = Field(default=False, description="delta is larger than the ensemble spread")


class ComparisonResult(ApiModel):
    scenario_a: SimulationSummary
    scenario_b: SimulationSummary
    rows: list[ComparisonRow]
    score_a: float
    score_b: float
    verdict: str
    decisive_factors: list[str]
    tradeoffs: list[str]
    explainability: Explainability


# --------------------------------------------------------------------------- #
# tasks / resources / events
# --------------------------------------------------------------------------- #
class TaskCreate(ApiModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = ""
    priority: int = Field(default=3, ge=1, le=5)
    due_at: datetime | None = None
    incident_id: str | None = None
    simulation_id: str | None = None
    recommendation_id: str | None = None
    assigned_to: str | None = None
    target_asset_id: str | None = None
    resource_requirements: dict[str, float] = Field(default_factory=dict)


class TaskOut(TaskCreate):
    id: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    created_by: str
    history: list[dict[str, Any]] = Field(default_factory=list)


class ResourceOut(ApiModel):
    id: str
    name: str
    resource_type: ResourceType
    home_asset_id: str | None = None
    quantity: float
    available: float
    capabilities: dict[str, Any] = Field(default_factory=dict)
    status: str = "available"


class EventOut(ApiModel):
    id: str
    kind: EventKind
    topic: str
    at: datetime
    payload: dict[str, Any]
    correlation_id: str = "-"


class StageStatus(ApiModel):
    stage: str
    state: Literal["idle", "active", "degraded", "error"]
    last_run_at: datetime | None = None
    detail: str = ""
    throughput: int = 0


class HealthOut(ApiModel):
    status: Literal["ok", "degraded", "critical"]
    mode: OperatingMode
    version: str
    uptime_s: float
    city: str
    checks: dict[str, dict[str, Any]]
    stages: list[StageStatus]
    ai: dict[str, Any]
    metrics: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class AuthToken(ApiModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int
    subject: str
    role: Role
    permissions: list[str]


class LoginRequest(ApiModel):
    subject: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=4, max_length=256)
    role: Role = Role.VIEWER


class AuditEntryOut(ApiModel):
    seq: int
    at: datetime
    actor: str
    role: str
    action: str
    resource: str
    outcome: str
    detail: dict[str, Any] = Field(default_factory=dict)
    hash: str
    prev_hash: str
    correlation_id: str = "-"


class GraphNodeOut(ApiModel):
    id: str
    name: str
    asset_type: AssetType
    status: AssetStatus
    criticality: float
    centroid: GeoPoint
    degree: int = 0
    betweenness: float = 0.0
    single_point_of_failure: bool = False
    zone: str | None = None


class GraphEdgeOut(RelationshipOut):
    pass


class GraphOut(ApiModel):
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    computed_at: datetime
    cascade_paths_sample: list[list[str]] = Field(default_factory=list)


class AnomalyOut(ApiModel):
    id: str
    sensor_id: str
    sensor_name: str
    metric: str
    observed_at: datetime
    observed_value: float
    expected_value: float
    z_score: float
    severity: Literal["info", "watch", "warn", "alert"]
    method: str
    explanation: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.6


class AIWorkflowRequest(ApiModel):
    thread_id: str | None = None
    incident_id: str | None = None
    focus_asset_ids: list[str] = Field(default_factory=list)
    question: str = ""
    horizon_minutes: int = Field(default=180, ge=15, le=1440)


class AIWorkflowResult(ApiModel):
    thread_id: str
    stages: list[dict[str, Any]]
    output: dict[str, Any]
    citations: list[str]
    grounded: bool
    degraded: bool
    runtime_ms: int
    checkpoints: int = 0


class IngestBatch(ApiModel):
    """Store-and-forward envelope used by field gateways when the link is down."""

    gateway_id: str = Field(min_length=2, max_length=64)
    queued_at: datetime | None = None
    readings: list[SensorReadingIn] = Field(default_factory=list, max_length=5000)
    events: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)


class IngestResult(ApiModel):
    accepted: int
    duplicates: int
    rejected: int
    errors: list[str] = Field(default_factory=list)
    server_time: datetime
