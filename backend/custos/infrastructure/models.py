"""SQLAlchemy 2.0 ORM models — the Crisis Digital Twin store.

Portability note
----------------
Production is PostgreSQL (+psycopg, async). The same schema must also run on
SQLite so that a responder laptop can hold a local twin when the EOC link is
down. Every column type below is therefore chosen from the portable subset:
``String``, ``Float``, ``Integer``, ``Boolean``, ``DateTime``, ``Text`` and
``JSON``. Geometry is stored as GeoJSON in a JSON column plus separate indexed
``lon``/``lat`` centroid columns, which keeps spatial filtering available with
B-tree indexes on both engines and avoids a hard PostGIS requirement for the
MVP. A PostGIS migration is a drop-in later step (see docs/DEPLOYMENT.md).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# --------------------------------------------------------------------------- #
# 1. COLLECT — raw, immutable-ish ingestion surface
# --------------------------------------------------------------------------- #
class Sensor(Base):
    """A physical or virtual measurement point with a maintained baseline."""

    __tablename__ = "sensors"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    unit: Mapped[str] = mapped_column(String(24), default="")
    provider: Mapped[str] = mapped_column(String(64), default="synthetic")
    source_ref: Mapped[str | None] = mapped_column(String(160))
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), index=True)
    zone: Mapped[str | None] = mapped_column(String(64), index=True)
    lon: Mapped[float] = mapped_column(Float, default=0.0)
    lat: Mapped[float] = mapped_column(Float, default=0.0)
    elevation_m: Mapped[float | None] = mapped_column(Float)
    expected_min: Mapped[float | None] = mapped_column(Float)
    expected_max: Mapped[float | None] = mapped_column(Float)
    #: robust baseline (median + MAD) recomputed by the fusion service
    baseline_median: Mapped[float | None] = mapped_column(Float)
    baseline_mad: Mapped[float | None] = mapped_column(Float)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    readings: Mapped[list["SensorReading"]] = relationship(back_populates="sensor", cascade="all, delete-orphan")


class SensorReading(Base):
    __tablename__ = "sensor_readings"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_reading_dedupe"),
        Index("ix_reading_sensor_time", "sensor_id", "observed_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("rdg"))
    sensor_id: Mapped[str] = mapped_column(ForeignKey("sensors.id", ondelete="CASCADE"), nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(24), default="")
    quality: Mapped[float] = mapped_column(Float, default=1.0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="synthetic")
    #: sha1(sensor|metric|observed_at|value) — idempotent ingest, replay-safe
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    is_outlier: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    sensor: Mapped[Sensor] = relationship(back_populates="readings")


class ConnectorRun(Base):
    """Observability for every integration: proves a gap in data, not a gap in truth."""

    __tablename__ = "connector_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("crun"))
    connector: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    rows_in: Mapped[int] = mapped_column(Integer, default=0)
    rows_new: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    degraded_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------- #
# 2. FUSE — canonical twin objects
# --------------------------------------------------------------------------- #
class Asset(Base, TimestampMixin):
    """A canonical object of the twin: road, substation, hospital, gauge, canal…"""

    __tablename__ = "assets"
    __table_args__ = (
        Index("ix_asset_type_status", "asset_type", "status"),
        Index("ix_asset_zone", "zone"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    subtype: Mapped[str | None] = mapped_column(String(48))
    criticality: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(16), default="ok", index=True)
    lon: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    lat: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    elevation_m: Mapped[float | None] = mapped_column(Float)
    geometry: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    zone: Mapped[str | None] = mapped_column(String(64))
    operator: Mapped[str | None] = mapped_column(String(160))

    # live twin state (the "Understand" stage writes here)
    flood_depth_m: Mapped[float] = mapped_column(Float, default=0.0)
    power_available: Mapped[bool] = mapped_column(Boolean, default=True)
    access_available: Mapped[bool] = mapped_column(Boolean, default=True)
    service_level: Mapped[float] = mapped_column(Float, default=1.0)
    exposure_score: Mapped[float] = mapped_column(Float, default=0.0)
    capacity: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    state_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state_source: Mapped[str | None] = mapped_column(String(64))
    state_confidence: Mapped[float] = mapped_column(Float, default=1.0)

    outgoing: Mapped[list["Relationship"]] = relationship(
        back_populates="source", foreign_keys="Relationship.source_id", cascade="all, delete-orphan"
    )
    incoming: Mapped[list["Relationship"]] = relationship(
        back_populates="target", foreign_keys="Relationship.target_id", cascade="all, delete-orphan"
    )


class Relationship(Base, TimestampMixin):
    """Directed, typed edge. ``road -> hospital`` is literally one row."""

    __tablename__ = "relationships"
    __table_args__ = (
        UniqueConstraint("source_id", "target_id", "kind", name="uq_relationship"),
        Index("ix_rel_source_kind", "source_id", "kind"),
        Index("ix_rel_target_kind", "target_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("rel"))
    source_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    target_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    criticality: Mapped[float] = mapped_column(Float, default=0.5)
    lead_time_min: Mapped[int] = mapped_column(Integer, default=0)
    attenuation: Mapped[float] = mapped_column(Float, default=1.0)
    failure_mode: Mapped[str] = mapped_column(String(16), default="immediate")
    recovery_hours: Mapped[float] = mapped_column(Float, default=6.0)
    redundancy_group: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    source: Mapped[Asset] = relationship(back_populates="outgoing", foreign_keys=[source_id])
    target: Mapped[Asset] = relationship(back_populates="incoming", foreign_keys=[target_id])


class Zone(Base):
    """A census/administrative area: the unit of equity accounting."""

    __tablename__ = "zones"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    lon: Mapped[float] = mapped_column(Float, default=0.0)
    lat: Mapped[float] = mapped_column(Float, default=0.0)
    population: Mapped[int] = mapped_column(Integer, default=0)
    #: 0..1 composite deprivation index (used to weight "who suffers most")
    vulnerability_index: Mapped[float] = mapped_column(Float, default=0.3)
    children_pct: Mapped[float] = mapped_column(Float, default=0.0)
    elderly_pct: Mapped[float] = mapped_column(Float, default=0.0)
    population_density: Mapped[float] = mapped_column(Float, default=0.0)
    mean_elevation_m: Mapped[float] = mapped_column(Float, default=0.0)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class FacilityDetail(Base):
    """Facility-specific attributes kept off the hot `assets` row."""

    __tablename__ = "facility_details"

    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    capacity: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: autonomous hours on generator / stored water
    autonomy_hours: Mapped[float] = mapped_column(Float, default=0.0)
    oxygen_hours: Mapped[float] = mapped_column(Float, default=0.0)
    trauma_level: Mapped[int] = mapped_column(Integer, default=0)
    beds_total: Mapped[int] = mapped_column(Integer, default=0)
    beds_occupied: Mapped[int] = mapped_column(Integer, default=0)
    vulnerable_patients: Mapped[int] = mapped_column(Integer, default=0)
    accepts_evacuees: Mapped[bool] = mapped_column(Boolean, default=True)
    helipad_elevation_m: Mapped[float | None] = mapped_column(Float)


class ShelterDetail(Base):
    __tablename__ = "shelter_details"

    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    capacity_people: Mapped[int] = mapped_column(Integer, default=0)
    occupancy: Mapped[int] = mapped_column(Integer, default=0)
    has_water: Mapped[bool] = mapped_column(Boolean, default=True)
    has_power_backup: Mapped[bool] = mapped_column(Boolean, default=True)
    accessible_for_disability: Mapped[bool] = mapped_column(Boolean, default=False)
    opening_time_min: Mapped[int] = mapped_column(Integer, default=0)


class RoadDetail(Base):
    __tablename__ = "road_details"

    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    length_m: Mapped[float] = mapped_column(Float, default=0.0)
    lanes: Mapped[int] = mapped_column(Integer, default=2)
    free_flow_speed_kmh: Mapped[float] = mapped_column(Float, default=40.0)
    #: depth above which ordinary traffic stops (m)
    flood_threshold_m: Mapped[float] = mapped_column(Float, default=0.3)
    #: depth above which a heavy rescue vehicle stops (m)
    heavy_threshold_m: Mapped[float] = mapped_column(Float, default=0.6)
    is_evacuation_route: Mapped[bool] = mapped_column(Boolean, default=False)
    is_bridge: Mapped[bool] = mapped_column(Boolean, default=False)
    underpass: Mapped[bool] = mapped_column(Boolean, default=False)
    altitude_profile: Mapped[list[float]] = mapped_column(JSON, default=list)


# --------------------------------------------------------------------------- #
# 3. UNDERSTAND — incidents, anomalies, work items
# --------------------------------------------------------------------------- #
class Incident(Base, TimestampMixin):
    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incident_status_sev", "status", "severity"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("inc"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    severity: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(16), default="detected", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.6)
    summary: Mapped[str] = mapped_column(Text, default="")
    lon: Mapped[float] = mapped_column(Float, default=0.0)
    lat: Mapped[float] = mapped_column(Float, default=0.0)
    radius_m: Mapped[float] = mapped_column(Float, default=400.0)
    zone: Mapped[str | None] = mapped_column(String(64))
    population_exposed: Mapped[int] = mapped_column(Integer, default=0)
    reported_by: Mapped[str] = mapped_column(String(64), default="system")
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    explainability: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    timeline: Mapped[list["IncidentEvent"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", order_by="IncidentEvent.at"
    )
    asset_links: Mapped[list["IncidentAsset"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )


class IncidentAsset(Base):
    __tablename__ = "incident_assets"
    __table_args__ = (UniqueConstraint("incident_id", "asset_id", name="uq_incident_asset"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("insa"))
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(24), default="affected")
    impact: Mapped[float] = mapped_column(Float, default=0.0)

    incident: Mapped[Incident] = relationship(back_populates="asset_links")


class IncidentEvent(Base):
    __tablename__ = "incident_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("inse"))
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    stage: Mapped[str] = mapped_column(String(24), default="understand")
    actor: Mapped[str] = mapped_column(String(64), default="system")
    detail: Mapped[str] = mapped_column(Text, default="")
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)

    incident: Mapped[Incident] = relationship(back_populates="timeline")


class Anomaly(Base):
    __tablename__ = "anomalies"
    __table_args__ = (Index("ix_anomaly_time", "observed_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("anom"))
    sensor_id: Mapped[str] = mapped_column(ForeignKey("sensors.id", ondelete="CASCADE"), nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(64), default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    observed_value: Mapped[float] = mapped_column(Float, default=0.0)
    expected_value: Mapped[float] = mapped_column(Float, default=0.0)
    z_score: Mapped[float] = mapped_column(Float, default=0.0)
    severity: Mapped[str] = mapped_column(String(12), default="watch", index=True)
    method: Mapped[str] = mapped_column(String(32), default="robust-zscore")
    explanation: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.6)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    promoted_incident_id: Mapped[str | None] = mapped_column(ForeignKey("incidents.id", ondelete="SET NULL"))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class IncidentUpdate(Base):
    """Append-only operator/agent journal; the audit trail of the incident."""

    __tablename__ = "incident_updates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("inu"))
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    author: Mapped[str] = mapped_column(String(64), default="system")
    kind: Mapped[str] = mapped_column(String(24), default="note")
    body: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------- #
# 4. PREDICT / SIMULATE — the what-if store
# --------------------------------------------------------------------------- #
class Simulation(Base, TimestampMixin):
    __tablename__ = "simulations"
    __table_args__ = (Index("ix_sim_status_created", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sim"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    baseline_id: Mapped[str | None] = mapped_column(String(64))
    horizon_minutes: Mapped[int] = mapped_column(Integer, default=360)
    timestep_minutes: Mapped[int] = mapped_column(Integer, default=15)
    ensemble_size: Mapped[int] = mapped_column(Integer, default=48)
    random_seed: Mapped[int] = mapped_column(Integer, default=0)
    request: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    kpis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    uncertainty: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    timeline: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cascade: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recommendations: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    explainability: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sensitivity: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    narrative: Mapped[str] = mapped_column(Text, default="")
    determinism_token: Mapped[str] = mapped_column(String(64), default="")
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    runtime_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SimulationEvent(Base):
    """Progress stream; also replays the run when a browser reconnects."""

    __tablename__ = "simulation_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sime"))
    simulation_id: Mapped[str] = mapped_column(
        ForeignKey("simulations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, default=0)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    stage: Mapped[str] = mapped_column(String(24), default="simulate")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CascadeEdge(Base):
    """Persisted cascade finding: explainable, scoreable, reviewable."""

    __tablename__ = "cascade_edges"
    __table_args__ = (Index("ix_cascade_sim_ts", "simulation_id", "timestep_min"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("csc"))
    simulation_id: Mapped[str] = mapped_column(
        ForeignKey("simulations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    timestep_min: Mapped[int] = mapped_column(Integer, default=0)
    cause_asset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_asset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mechanism: Mapped[str] = mapped_column(String(64), default="")
    relation_kind: Mapped[str | None] = mapped_column(String(24))
    hop: Mapped[int] = mapped_column(Integer, default=1)
    magnitude: Mapped[float] = mapped_column(Float, default=0.0)
    probability: Mapped[float] = mapped_column(Float, default=0.0)
    status_after: Mapped[str] = mapped_column(String(16), default="degraded")
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)


# --------------------------------------------------------------------------- #
# 5. RECOMMEND — decisions, tasks, resources
# --------------------------------------------------------------------------- #
class RecommendationRecord(Base, TimestampMixin):
    __tablename__ = "recommendations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("rec"))
    simulation_id: Mapped[str | None] = mapped_column(ForeignKey("simulations.id", ondelete="SET NULL"), index=True)
    incident_id: Mapped[str | None] = mapped_column(ForeignKey("incidents.id", ondelete="SET NULL"), index=True)
    rank: Mapped[int] = mapped_column(Integer, default=1)
    action: Mapped[str] = mapped_column(String(200), default="")
    intervention_type: Mapped[str] = mapped_column(String(32), default="pre_position_resource")
    target_id: Mapped[str | None] = mapped_column(String(64))
    window: Mapped[str] = mapped_column(String(64), default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    expected_kpi_delta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    resources_required: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    urgency: Mapped[int] = mapped_column(Integer, default=3)
    confidence: Mapped[float] = mapped_column(Float, default=0.6)
    feasibility: Mapped[float] = mapped_column(Float, default=0.8)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    counterfactual: Mapped[str] = mapped_column(Text, default="")
    generated_by: Mapped[str] = mapped_column(String(48), default="rules+cascade")
    accepted: Mapped[bool | None] = mapped_column(Boolean)
    accepted_by: Mapped[str | None] = mapped_column(String(64))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_task_status_priority", "status", "priority"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("tsk"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="proposed", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=3)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    incident_id: Mapped[str | None] = mapped_column(String(64), index=True)
    simulation_id: Mapped[str | None] = mapped_column(String(64))
    recommendation_id: Mapped[str | None] = mapped_column(String(64))
    assigned_to: Mapped[str | None] = mapped_column(String(64))
    target_asset_id: Mapped[str | None] = mapped_column(String(64))
    resource_requirements: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class Resource(Base, TimestampMixin):
    __tablename__ = "resources"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("res"))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    home_asset_id: Mapped[str | None] = mapped_column(String(64))
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    available: Mapped[float] = mapped_column(Float, default=1.0)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="available")
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ResourceAssignment(Base):
    __tablename__ = "resource_assignments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ras"))
    resource_id: Mapped[str] = mapped_column(ForeignKey("resources.id", ondelete="CASCADE"), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(64))
    simulation_id: Mapped[str | None] = mapped_column(String(64))
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    from_asset_id: Mapped[str | None] = mapped_column(String(64))
    to_asset_id: Mapped[str | None] = mapped_column(String(64))
    depart_min: Mapped[int] = mapped_column(Integer, default=0)
    eta_min: Mapped[int | None] = mapped_column(Integer)
    route_asset_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="planned")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------- #
# cross-cutting
# --------------------------------------------------------------------------- #
class EventLog(Base):
    """Durable event spine. The WebSocket hub and any replay tooling read this."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_event_kind_at", "kind", "at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("evt"))
    kind: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    topic: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    correlation_id: Mapped[str] = mapped_column(String(64), default="-")
    #: producer of the event (component or operator identity), for after-action review
    source: Mapped[str] = mapped_column(String(64), default="system")
    seq: Mapped[int] = mapped_column(Integer, default=0)


class AuditEntry(Base):
    """Hash-chained audit trail: tamper-evident, exportable, replayable.

    ``hash = sha256(prev_hash | seq | at | actor | action | resource | outcome | detail)``
    so silently editing history breaks the chain at an exact sequence number.
    """

    __tablename__ = "audit_log"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(64), default=lambda: new_id("aud"), unique=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(64), default="anonymous")
    role: Mapped[str] = mapped_column(String(24), default="viewer")
    action: Mapped[str] = mapped_column(String(32), default="read", index=True)
    resource: Mapped[str] = mapped_column(String(200), default="")
    outcome: Mapped[str] = mapped_column(String(16), default="ok")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    hash: Mapped[str] = mapped_column(String(64), default="")
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    correlation_id: Mapped[str] = mapped_column(String(64), default="-")


class AIInvocation(Base):
    """Every model call, with prompt digest, latency, fallback and grounding result."""

    __tablename__ = "ai_invocations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ai"))
    thread_id: Mapped[str | None] = mapped_column(String(64), index=True)
    stage: Mapped[str] = mapped_column(String(24), default="classify")
    model: Mapped[str] = mapped_column(String(96), default="")
    prompt_digest: Mapped[str] = mapped_column(String(64), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    fell_back: Mapped[bool] = mapped_column(Boolean, default=False)
    grounded: Mapped[bool] = mapped_column(Boolean, default=True)
    rejected_claims: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CheckpointMirror(Base):
    """Human-readable mirror of LangGraph async checkpoints.

    LangGraph owns its own checkpoint tables (created by ``AsyncPostgresSaver``
    or ``AsyncSqliteSaver``). This mirror exists so that operators can *see* the
    reasoning state in the UI without reading a library-internal schema, and so
    an air-gapped review can be exported to CSV.
    """

    __tablename__ = "checkpoint_mirror"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ckpt"))
    thread_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    checkpoint_id: Mapped[str] = mapped_column(String(64), default="")
    step: Mapped[int] = mapped_column(Integer, default=0)
    node: Mapped[str] = mapped_column(String(64), default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    state_digest: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SystemState(Base):
    """Single-row key/value table for mode, degraded flags and boot markers."""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
