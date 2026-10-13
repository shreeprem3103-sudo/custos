"""Closed enumerations shared by storage, engines and the API contract.

String-valued enums are used deliberately: they survive a JSON round-trip
unchanged, they are readable in an audit log years later, and adding a member
never renumbers existing data.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return str(self.value)

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]


class Role(StrEnum):
    COMMANDER = "commander"  # may author what-if runs and commit recommendations
    ANALYST = "analyst"      # may run simulations and triage, may not commit resources
    RESPONDER = "responder"  # field view: assigned tasks, route updates
    VIEWER = "viewer"        # read-only observers / liaisons
    INGEST = "ingest"        # machine-to-machine sensor ingestion


class Permission(StrEnum):
    READ = "read"
    INGEST = "ingest"
    SIMULATE = "simulate"
    COMMIT = "commit"
    ADMIN = "admin"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.COMMANDER: frozenset({Permission.READ, Permission.SIMULATE, Permission.COMMIT, Permission.ADMIN}),
    Role.ANALYST: frozenset({Permission.READ, Permission.SIMULATE}),
    Role.RESPONDER: frozenset({Permission.READ}),
    Role.VIEWER: frozenset({Permission.READ}),
    Role.INGEST: frozenset({Permission.INGEST}),
}


class AssetType(StrEnum):
    ROAD = "road"
    BRIDGE = "bridge"
    CANAL = "canal"
    RIVER_GAUGE = "river_gauge"
    RAIN_GAUGE = "rain_gauge"
    PUMP_STATION = "pump_station"
    SUBSTATION = "substation"
    HOSPITAL = "hospital"
    SHELTER = "shelter"
    TELECOM_TOWER = "telecom_tower"
    WATER_TREATMENT = "water_treatment"
    FIRE_STATION = "fire_station"
    POLICE_STATION = "police_station"
    METRO_STATION = "metro_station"
    AIRPORT = "airport"
    PORT = "port"
    REFINERY = "refinery"
    WASTE_PLANT = "waste_plant"
    SCHOOL = "school"
    COMMUNITY_HALL = "community_hall"


#: asset classes whose failure removes service from a population
LIFELINE_TYPES = frozenset(
    {
        AssetType.HOSPITAL,
        AssetType.SUBSTATION,
        AssetType.TELECOM_TOWER,
        AssetType.WATER_TREATMENT,
        AssetType.PUMP_STATION,
        AssetType.FIRE_STATION,
        AssetType.POLICE_STATION,
        AssetType.METRO_STATION,
        AssetType.AIRPORT,
        AssetType.PORT,
        AssetType.REFINERY,
        AssetType.WASTE_PLANT,
    }
)

#: asset classes that carry protection capacity for people
SHELTER_TYPES = frozenset({AssetType.SHELTER, AssetType.SCHOOL, AssetType.COMMUNITY_HALL})

MOBILITY_TYPES = frozenset({AssetType.ROAD, AssetType.BRIDGE})


class AssetStatus(StrEnum):
    OK = "ok"
    WATCH = "watch"
    DEGRADED = "degraded"
    IMPAIRED = "impaired"
    FAILED = "failed"
    UNKNOWN = "unknown"


#: severity ladder used by the cascade engine to decide whether an effect is an
#: escalation. Higher index == worse.
STATUS_LADDER: tuple[AssetStatus, ...] = (
    AssetStatus.UNKNOWN,
    AssetStatus.OK,
    AssetStatus.WATCH,
    AssetStatus.DEGRADED,
    AssetStatus.IMPAIRED,
    AssetStatus.FAILED,
)


def status_rank(status: AssetStatus) -> int:
    try:
        return STATUS_LADDER.index(status)
    except ValueError:  # pragma: no cover - defensive
        return 0


def worst(a: AssetStatus, b: AssetStatus) -> AssetStatus:
    return a if status_rank(a) >= status_rank(b) else b


class RelationKind(StrEnum):
    CONNECTS = "connects"            # road <-> road continuity
    CROSSES = "crosses"              # road -> bridge
    ACCESSES = "accesses"            # road -> facility (the ambulance route)
    POWERS = "powers"                # substation -> consumer
    SUPPLIES_WATER = "supplies_water"  # treatment plant -> zone/facility
    COVERS = "covers"                # telecom tower -> area/facility
    DRAINS = "drains"                # pump/canal -> area
    MONITORS = "monitors"            # gauge -> water body
    SHELTERS = "shelters"            # shelter -> population zone
    RELEASES = "releases"            # reservoir -> river gauge
    FEEDS = "feeds"                  # canal -> canal / river -> canal
    DEPENDS_ON = "depends_on"        # generic hard dependency
    ROUTES_TO = "routes_to"          # station -> coverage area


class FailureMode(StrEnum):
    IMMEDIATE = "immediate"  # effect lands within one timestep
    DEGRADED = "degraded"    # partial capability loss
    DELAYED = "delayed"      # effect lands after a lead time / buffer exhaustion


class IncidentCategory(StrEnum):
    URBAN_FLOOD = "urban_flood"
    CANAL_OVERFLOW = "canal_overflow"
    ROAD_INUNDATION = "road_inundation"
    POWER_OUTAGE = "power_outage"
    WATER_OUTAGE = "water_outage"
    TELECOM_OUTAGE = "telecom_outage"
    STRUCTURAL_DAMAGE = "structural_damage"
    LANDSLIDE = "landslide"
    DAM_RELEASE = "dam_release"
    PUBLIC_HEALTH = "public_health"
    CITIZEN_REPORT = "citizen_report"
    SENSOR_ANOMALY = "sensor_anomaly"


class IncidentStatus(StrEnum):
    DETECTED = "detected"
    TRIAGED = "triaged"
    ACTIVE = "active"
    CONTAINED = "contained"
    RESOLVED = "resolved"
    CLOSED = "closed"


class HazardType(StrEnum):
    FLOOD_INJECTION = "flood_injection"   # a depth is imposed at a location
    RAINFALL_BURST = "rainfall_burst"     # mm/h applied over a polygon
    DAM_RELEASE = "dam_release"           # upstream discharge in m3/s
    TIDAL_SURGE = "tidal_surge"           # downstream boundary condition
    PUMP_FAILURE = "pump_failure"         # drainage capacity removed
    DRAINAGE_BLOCKAGE = "drainage_blockage"
    LEVEE_BREACH = "levee_breach"


class InterventionType(StrEnum):
    DEPLOY_PUMP = "deploy_pump"
    CLOSE_ROAD = "close_road"
    REOPEN_ROAD = "reopen_road"
    EVACUATE_FACILITY = "evacuate_facility"
    PRE_POSITION_RESOURCE = "pre_position_resource"
    REROUTE_TRAFFIC = "reroute_traffic"
    SANDBAG_LINE = "sandbag_line"
    POWER_SWITCHOVER = "power_switchover"
    OPEN_SHELTER = "open_shelter"
    ISSUE_PUBLIC_ALERT = "issue_public_alert"
    HOLD_WATER_RELEASE = "hold_water_release"


class ResourceType(StrEnum):
    PUMP = "pump"
    BOAT = "boat"
    AMBULANCE = "ambulance"
    RESCUE_TEAM = "rescue_team"
    FIRE_UNIT = "fire_unit"
    SANDBAG_UNIT = "sandbag_unit"
    GENERATOR = "generator"
    DRONE = "drone"
    MOBILE_TOWER = "mobile_tower"


class EventKind(StrEnum):
    TELEMETRY = "telemetry"
    INCIDENT = "incident"
    ASSET_STATE = "asset_state"
    SIMULATION = "simulation"
    RECOMMENDATION = "recommendation"
    TASK = "task"
    SYSTEM = "system"


class AIStage(StrEnum):
    CLASSIFY = "classify"
    ANOMALY = "anomaly"
    CASCADE = "cascade"
    PREDICT = "predict"
    RECOMMEND = "recommend"
    BRIEF = "brief"


class ConfidenceBand(StrEnum):
    VERIFIED = "verified"   # >= 0.9, corroborated by >= 2 independent sources
    HIGH = "high"           # >= 0.75
    MEDIUM = "medium"       # >= 0.5
    LOW = "low"             # >= 0.3
    SPECULATIVE = "speculative"


def confidence_band(score: float) -> ConfidenceBand:
    if score >= 0.9:
        return ConfidenceBand.VERIFIED
    if score >= 0.75:
        return ConfidenceBand.HIGH
    if score >= 0.5:
        return ConfidenceBand.MEDIUM
    if score >= 0.3:
        return ConfidenceBand.LOW
    return ConfidenceBand.SPECULATIVE


class FreshnessBand(StrEnum):
    LIVE = "live"          # < 2 min
    RECENT = "recent"      # < 15 min
    STALE = "stale"        # < 2 h
    DARK = "dark"          # >= 2 h or never


def freshness_band(age_seconds: float | None) -> FreshnessBand:
    if age_seconds is None:
        return FreshnessBand.DARK
    if age_seconds < 120:
        return FreshnessBand.LIVE
    if age_seconds < 900:
        return FreshnessBand.RECENT
    if age_seconds < 7200:
        return FreshnessBand.STALE
    return FreshnessBand.DARK


class OperatingMode(StrEnum):
    """Whole-system resilience state, surfaced to every operator console."""

    NORMAL = "normal"
    DEGRADED = "degraded"      # one or more upstream feeds lost, core alive
    ISOLATED = "isolated"      # upstream lost; operating from local state only
    READ_ONLY = "read_only"    # storage unhealthy; commands are queued, not committed


class TaskStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    DISPATCHED = "dispatched"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class AuditAction(StrEnum):
    BOOT = "boot"
    LOGIN = "login"
    READ = "read"
    INGEST = "ingest"
    SIMULATE = "simulate"
    COMMIT = "commit"
    CONFIG = "config"
    DENIED = "denied"
