"""Kochi (Ernakulam) crisis-twin dataset.

Scope of the MVP: **one city**. Everything below is a hand-built, deterministic
digital twin of the Kochi urban region - the tidal-backwater city where the
Periyar and Muvattupuzha river systems meet the Vembanad backwaters, and where
the flood mechanism is *not* a single river crest but a compound event:

    intense monsoon rainfall  ->  canal network surcharge
    + high tide / backwater level  ->  gravity drains stop draining
    + pump station failure / power loss  ->  inundation persists for days

That physical chain is the reason CUSTOS models canals, pumps, tides and the
power graph as first-class objects rather than as annotations.

Coordinates are real-world approximations of the actual facilities, laid out to
satisfy the city bounding box. The dataset is intentionally *fixtured*: a
production deployment replaces it through the ingestion API (docs/INTEGRATION.md)
without touching a single line of engine code.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..domain.enums import (
    AssetStatus,
    AssetType,
    FailureMode,
    IncidentCategory,
    IncidentStatus,
    RelationKind,
    ResourceType,
)
from ..fusion.normalize import haversine_m

CITY = {
    "id": "kochi",
    "name": "Kochi (Ernakulam)",
    "timezone": "Asia/Kolkata",
    "population": 3_120_000,
    "bbox": (76.16, 9.83, 76.45, 10.20),
    "mean_elevation_m": 3.4,
    "tide_range_m": 1.2,
    "notes": "Tidal backwater city; drainage is gravity-dependent on backwater stage.",
}

NOW = datetime(2026, 10, 13, 20, 0, tzinfo=timezone.utc)  # seed anchor (deterministic)


# --------------------------------------------------------------------------- #
# zones - population units used for equity and exposure accounting
# --------------------------------------------------------------------------- #
ZONES: list[dict[str, Any]] = [
    # id,             name,                lon,     lat,   pop,   vuln, kids%, eld%,  elev
    {"id": "z_edappally", "name": "Edappally", "lon": 76.3050, "lat": 10.0250, "population": 96_000, "vulnerability_index": 0.42, "children_pct": 0.11, "elderly_pct": 0.09, "area_km2": 6.5, "population_density": 14769.2, "design_rainfall_mm_h": 40, "mean_elevation_m": 7.2},
    {"id": "z_kakkanad", "name": "Kakkanad", "lon": 76.3400, "lat": 10.0100, "population": 128_000, "vulnerability_index": 0.38, "children_pct": 0.12, "elderly_pct": 0.07, "area_km2": 14.0, "population_density": 9142.9, "design_rainfall_mm_h": 48, "mean_elevation_m": 8.6},
    {"id": "z_vyttila", "name": "Vyttila", "lon": 76.3200, "lat": 9.9700, "population": 88_000, "vulnerability_index": 0.55, "children_pct": 0.10, "elderly_pct": 0.11, "area_km2": 7.0, "population_density": 12571.4, "design_rainfall_mm_h": 34, "mean_elevation_m": 3.1},
    {"id": "z_kaloor", "name": "Kaloor", "lon": 76.2900, "lat": 9.9950, "population": 104_000, "vulnerability_index": 0.52, "children_pct": 0.09, "elderly_pct": 0.12, "area_km2": 6.0, "population_density": 17333.3, "design_rainfall_mm_h": 34, "mean_elevation_m": 4.0},
    {"id": "z_palarivattom", "name": "Palarivattom", "lon": 76.3080, "lat": 10.0020, "population": 76_000, "vulnerability_index": 0.45, "children_pct": 0.10, "elderly_pct": 0.10, "area_km2": 5.2, "population_density": 14615.4, "design_rainfall_mm_h": 40, "mean_elevation_m": 5.2},
    {"id": "z_elamkulam", "name": "Elamkulam", "lon": 76.2950, "lat": 9.9720, "population": 82_000, "vulnerability_index": 0.61, "children_pct": 0.11, "elderly_pct": 0.13, "area_km2": 4.2, "population_density": 19523.8, "design_rainfall_mm_h": 30, "mean_elevation_m": 2.6},
    {"id": "z_thevara", "name": "Thevara", "lon": 76.2890, "lat": 9.9510, "population": 58_000, "vulnerability_index": 0.58, "children_pct": 0.10, "elderly_pct": 0.12, "area_km2": 3.6, "population_density": 16111.1, "design_rainfall_mm_h": 30, "mean_elevation_m": 2.2},
    {"id": "z_fortkochi", "name": "Fort Kochi", "lon": 76.2420, "lat": 9.9660, "population": 34_000, "vulnerability_index": 0.66, "children_pct": 0.09, "elderly_pct": 0.16, "area_km2": 2.1, "population_density": 16190.5, "design_rainfall_mm_h": 28, "mean_elevation_m": 1.4},
    {"id": "z_mattancherry", "name": "Mattancherry", "lon": 76.2510, "lat": 9.9570, "population": 41_000, "vulnerability_index": 0.71, "children_pct": 0.12, "elderly_pct": 0.14, "area_km2": 2.4, "population_density": 17083.3, "design_rainfall_mm_h": 28, "mean_elevation_m": 1.1},
    {"id": "z_vypin", "name": "Vypin - Njarakkal", "lon": 76.2300, "lat": 10.0050, "population": 112_000, "vulnerability_index": 0.68, "children_pct": 0.11, "elderly_pct": 0.15, "area_km2": 16.0, "population_density": 7000.0, "design_rainfall_mm_h": 26, "mean_elevation_m": 1.0},
    {"id": "z_kalamassery", "name": "Kalamassery", "lon": 76.3200, "lat": 10.0500, "population": 74_000, "vulnerability_index": 0.40, "children_pct": 0.11, "elderly_pct": 0.08, "area_km2": 12.0, "population_density": 6166.7, "design_rainfall_mm_h": 46, "mean_elevation_m": 11.8},
    {"id": "z_aluva", "name": "Aluva", "lon": 76.3500, "lat": 10.1000, "population": 68_000, "vulnerability_index": 0.54, "children_pct": 0.10, "elderly_pct": 0.12, "area_km2": 10.0, "population_density": 6800.0, "design_rainfall_mm_h": 44, "mean_elevation_m": 9.4},
    {"id": "z_kumbalam", "name": "Kumbalam", "lon": 76.3050, "lat": 9.9050, "population": 46_000, "vulnerability_index": 0.69, "children_pct": 0.10, "elderly_pct": 0.14, "area_km2": 7.5, "population_density": 6133.3, "design_rainfall_mm_h": 28, "mean_elevation_m": 1.6},
    {"id": "z_maradu", "name": "Maradu", "lon": 76.2930, "lat": 9.9380, "population": 52_000, "vulnerability_index": 0.57, "children_pct": 0.11, "elderly_pct": 0.11, "area_km2": 5.4, "population_density": 9629.6, "design_rainfall_mm_h": 32, "mean_elevation_m": 2.4},
    {"id": "z_kadavanthra", "name": "Kadavanthra", "lon": 76.2990, "lat": 9.9680, "population": 61_000, "vulnerability_index": 0.48, "children_pct": 0.09, "elderly_pct": 0.11, "area_km2": 4.6, "population_density": 13260.9, "design_rainfall_mm_h": 34, "mean_elevation_m": 3.6},
    {"id": "z_ambalamugal", "name": "Ambalamugal", "lon": 76.3620, "lat": 9.9450, "population": 39_000, "vulnerability_index": 0.62, "children_pct": 0.13, "elderly_pct": 0.09, "area_km2": 9.0, "population_density": 4333.3, "design_rainfall_mm_h": 44, "mean_elevation_m": 12.0},
    {"id": "z_thrippunithura", "name": "Thrippunithura", "lon": 76.3400, "lat": 9.9400, "population": 72_000, "vulnerability_index": 0.50, "children_pct": 0.10, "elderly_pct": 0.12, "area_km2": 8.5, "population_density": 8470.6, "design_rainfall_mm_h": 40, "mean_elevation_m": 5.4},
    {"id": "z_cheranalloor", "name": "Cheranalloor", "lon": 76.2570, "lat": 10.0290, "population": 44_000, "vulnerability_index": 0.63, "children_pct": 0.11, "elderly_pct": 0.13, "area_km2": 6.2, "population_density": 7096.8, "design_rainfall_mm_h": 28, "mean_elevation_m": 2.0},
    {"id": "z_island", "name": "Willingdon Island", "lon": 76.2600, "lat": 9.9400, "population": 12_000, "vulnerability_index": 0.44, "children_pct": 0.07, "elderly_pct": 0.10, "area_km2": 3.4, "population_density": 3529.4, "design_rainfall_mm_h": 40, "mean_elevation_m": 2.8},
    {"id": "z_mgroad", "name": "M.G. Road - Central", "lon": 76.2880, "lat": 9.9780, "population": 97_000, "vulnerability_index": 0.46, "children_pct": 0.08, "elderly_pct": 0.14, "area_km2": 3.8, "population_density": 25526.3, "design_rainfall_mm_h": 32, "mean_elevation_m": 3.2},
]


def _point(lon: float, lat: float) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [lon, lat]}


def _line(points: list[tuple[float, float]]) -> dict[str, Any]:
    return {"type": "LineString", "coordinates": [[lon, lat] for lon, lat in points]}


# --------------------------------------------------------------------------- #
# assets
# --------------------------------------------------------------------------- #
# elevation in metres AMSL; criticality 0..1 = consequence weight used by the
# cascade engine and by the equity-weighted recommendation scorer.
ASSETS: list[dict[str, Any]] = []


def _add(
    asset_id: str,
    name: str,
    asset_type: AssetType,
    lon: float,
    lat: float,
    elevation: float | None = None,
    *,
    criticality: float,
    zone: str | None = None,
    subtype: str | None = None,
    operator: str | None = None,
    capacity: dict[str, Any] | None = None,
    geometry: dict[str, Any] | None = None,
    attributes: dict[str, Any] | None = None,
) -> None:
    ASSETS.append(
        {
            "id": asset_id,
            "name": name,
            "asset_type": asset_type,
            "subtype": subtype,
            "criticality": criticality,
            "lon": lon,
            "lat": lat,
            "elevation_m": elevation,
            "zone": zone,
            "operator": operator,
            "capacity": capacity or {},
            "geometry": geometry or _point(lon, lat),
            "attributes": attributes or {},
        }
    )


# ---- water bodies / gauges -------------------------------------------------
_add("a_periyar_aluva", "Periyar River (Aluva reach)", AssetType.RIVER_GAUGE, 76.3540, 10.1060, elevation=8.9, criticality=0.95, zone="z_aluva", subtype="river_reach", operator="Irrigation Dept", capacity={"bankfull_level_m": 5.4, "flood_warning_level_m": 4.6, "danger_level_m": 5.6, "design_capacity_m3s": 320.0, "width_m": 110.0})
_add("a_periyar_eloor", "Periyar River (Eloor reach)", AssetType.RIVER_GAUGE, 76.2900, 10.0800, elevation=4.2, criticality=0.9, zone="z_kalamassery", subtype="river_reach", operator="Irrigation Dept", capacity={"bankfull_level_m": 4.2, "design_capacity_m3s": 360.0, "width_m": 120.0})
_add("a_muvattupuzha_river", "Muvattupuzha River (downstream)", AssetType.RIVER_GAUGE, 76.3700, 9.9700, elevation=6.0, criticality=0.72, zone="z_ambalamugal", subtype="river_reach", operator="Irrigation Dept")
_add("a_kaniyampuzha", "Kaniyampuzha River", AssetType.CANAL, 76.3350, 9.9550, elevation=5.2, criticality=0.8, zone="z_thrippunithura", subtype="tributary", capacity={"bankfull_level_m": 3.2, "design_capacity_m3s": 60.0, "width_m": 30.0}, geometry=_line([(76.3650, 9.9800), (76.3500, 9.9650), (76.3350, 9.9550), (76.3200, 9.9450)]))
_add("a_backwater_kumbalam", "Vembanad Backwater (Kumbalam)", AssetType.RIVER_GAUGE, 76.3120, 9.9000, elevation=0.3, criticality=0.88, zone="z_kumbalam", subtype="backwater", operator="Port Dept", capacity={"mean_sea_level_m": 0.6, "design_capacity_m3s": 500.0, "width_m": 900.0})

_add("a_canal_thevara_perandoor", "Thevara - Perandoor Canal", AssetType.CANAL, 76.2920, 9.9600, elevation=2.1, criticality=0.92, zone="z_elamkulam", subtype="primary_canal", operator="Kochi Corporation", capacity={"bankfull_level_m": 2.8, "design_capacity_m3s": 32.0}, geometry=_line([(76.2990, 9.9680), (76.2960, 9.9620), (76.2920, 9.9580), (76.2890, 9.9520)]))
_add("a_canal_chilavannoor", "Chilavannoor Canal", AssetType.CANAL, 76.2980, 9.9690, elevation=2.8, criticality=0.78, zone="z_kadavanthra", subtype="primary_canal", capacity={"bankfull_level_m": 2.4, "design_capacity_m3s": 22.0, "width_m": 12.0}, geometry=_line([(76.3060, 9.9740), (76.3010, 9.9710), (76.2980, 9.9690)]))
_add("a_canal_edappally", "Edappally Canal", AssetType.CANAL, 76.3080, 10.0140, elevation=5.6, criticality=0.74, zone="z_edappally", subtype="primary_canal", capacity={"bankfull_level_m": 3.0, "design_capacity_m3s": 26.0, "width_m": 14.0}, geometry=_line([(76.3220, 10.0280), (76.3120, 10.0200), (76.3080, 10.0140), (76.3020, 10.0060)]))
_add("a_canal_perandoor", "Perandoor Canal (Kaloor)", AssetType.CANAL, 76.2890, 9.9930, elevation=3.4, criticality=0.7, zone="z_kaloor", subtype="primary_canal", capacity={"bankfull_level_m": 2.6, "design_capacity_m3s": 24.0, "width_m": 13.0}, geometry=_line([(76.2960, 10.0010), (76.2900, 9.9950), (76.2860, 9.9880)]))
_add("a_canal_kadamakudy", "Kadamakudy Canal Network", AssetType.CANAL, 76.2400, 10.0200, elevation=0.6, criticality=0.75, zone="z_vypin", subtype="backwater_cluster", geometry=_line([(76.2500, 10.0300), (76.2400, 10.0200), (76.2350, 10.0100)]))
_add("a_chitrapuzha", "Chitrapuzha Canal", AssetType.CANAL, 76.2850, 10.0600, elevation=2.6, criticality=0.68, zone="z_kalamassery", subtype="industrial_canal", geometry=_line([(76.3000, 10.0700), (76.2880, 10.0640), (76.2830, 10.0560)]))

# water-body hydraulic parameters (surveyed sections land here in production)
_CAPACITY_PATCHES: dict[str, dict[str, Any]] = {
    "a_muvattupuzha_river": {"design_capacity_m3s": 120.0, "width_m": 45.0, "bankfull_level_m": 4.0},
    "a_canal_kadamakudy": {"design_capacity_m3s": 18.0, "width_m": 20.0, "bankfull_level_m": 1.8},
    "a_chitrapuzha": {"design_capacity_m3s": 20.0, "width_m": 16.0, "bankfull_level_m": 2.4},
    "a_regulator_bhoothathankettu": {"design_capacity_m3s": 1100.0, "width_m": 60.0},
    "a_regulator_kaniyampuzha": {"design_capacity_m3s": 60.0, "width_m": 30.0},
    "a_regulator_manjummel": {"design_capacity_m3s": 90.0, "width_m": 34.0},
    "a_backwater_kumbalam": {"bankfull_level_m": 1.9, "initial_stage_m": 0.95},
    "a_canal_thevara_perandoor": {"initial_stage_m": 2.05},
    "a_canal_chilavannoor": {"initial_stage_m": 1.7},
    "a_canal_perandoor": {"initial_stage_m": 1.9},
    "a_canal_edappally": {"initial_stage_m": 2.1},
    "a_periyar_aluva": {"initial_stage_m": 3.6},
    "a_periyar_eloor": {"initial_stage_m": 2.9},
    "a_muvattupuzha_river": {"initial_stage_m": 3.1},
    "a_kaniyampuzha": {"initial_stage_m": 2.2},
    "a_canal_kadamakudy": {"initial_stage_m": 1.1},
    "a_chitrapuzha": {"initial_stage_m": 1.8},
}
for _asset in ASSETS:
    if _asset["id"] in _CAPACITY_PATCHES:
        _asset["capacity"].update(_CAPACITY_PATCHES[_asset["id"]])

# ---- pumping stations ------------------------------------------------------
_add("a_pump_elamkulam", "Elamkulam Pumping Station", AssetType.PUMP_STATION, 76.2970, 9.9700, elevation=2.4, criticality=0.94, zone="z_elamkulam", operator="Kochi Corporation", capacity={"pump_rate_m3s": 6.0, "pumps": 3, "generator_hours": 6.0, "power_dependency": "a_ss_kaloor"}, attributes={"commissioned": 2019})
_add("a_pump_thevara", "Thevara Pumping Station", AssetType.PUMP_STATION, 76.2900, 9.9530, elevation=2.0, criticality=0.9, zone="z_thevara", operator="Kochi Corporation", capacity={"pump_rate_m3s": 4.5, "pumps": 2, "generator_hours": 4.0})
_add("a_pump_kaloor", "Kaloor Pump House", AssetType.PUMP_STATION, 76.2870, 9.9970, elevation=3.2, criticality=0.82, zone="z_kaloor", operator="Kochi Corporation", capacity={"pump_rate_m3s": 3.0, "pumps": 2, "generator_hours": 0.0})
_add("a_pump_vennala", "Vennala Pumping Station", AssetType.PUMP_STATION, 76.3130, 9.9860, elevation=3.8, criticality=0.85, zone="z_palarivattom", operator="Kochi Corporation", capacity={"pump_rate_m3s": 5.0, "pumps": 3, "generator_hours": 8.0})
_add("a_pump_kumbalam", "Kumbalam Pumping Station", AssetType.PUMP_STATION, 76.3080, 9.9080, elevation=1.4, criticality=0.83, zone="z_kumbalam", operator="Kochi Corporation", capacity={"pump_rate_m3s": 3.5, "pumps": 2, "generator_hours": 2.0})
_add("a_pump_vyttila", "Vyttila Pumping Station", AssetType.PUMP_STATION, 76.3190, 9.9750, elevation=3.0, criticality=0.87, zone="z_vyttila", operator="Kochi Corporation", capacity={"pump_rate_m3s": 4.0, "pumps": 2, "generator_hours": 5.0})

# ---- power -----------------------------------------------------------------
_add("a_ss_kaloor", "KSEB 220 kV Substation Kaloor", AssetType.SUBSTATION, 76.2870, 9.9910, elevation=4.1, criticality=0.98, zone="z_kaloor", operator="KSEB", capacity={"capacity_mva": 200.0, "voltage_kv": 220, "feeders": 14, "flood_wall_m": 0.4})
_add("a_ss_kalamassery", "KSEB 110 kV Substation Kalamassery", AssetType.SUBSTATION, 76.3230, 10.0470, elevation=12.4, criticality=0.85, zone="z_kalamassery", operator="KSEB", capacity={"capacity_mva": 80.0, "voltage_kv": 110, "feeders": 9})
_add("a_ss_thevara", "KSEB 110 kV Substation Thevara", AssetType.SUBSTATION, 76.2870, 9.9480, elevation=2.3, criticality=0.9, zone="z_thevara", operator="KSEB", capacity={"capacity_mva": 63.0, "voltage_kv": 110, "feeders": 8, "flood_wall_m": 0.4})
_add("a_ss_kakkanad", "KSEB 110 kV Substation Kakkanad", AssetType.SUBSTATION, 76.3390, 10.0080, elevation=9.1, criticality=0.88, zone="z_kakkanad", operator="KSEB", capacity={"capacity_mva": 100.0, "voltage_kv": 110, "feeders": 11})
_add("a_ss_edappally", "KSEB 110 kV Substation Edappally", AssetType.SUBSTATION, 76.3070, 10.0270, elevation=7.6, criticality=0.86, zone="z_edappally", operator="KSEB", capacity={"capacity_mva": 75.0, "voltage_kv": 110, "feeders": 10})
_add("a_ss_aluva", "KSEB 110 kV Substation Aluva", AssetType.SUBSTATION, 76.3540, 10.1080, elevation=9.8, criticality=0.8, zone="z_aluva", operator="KSEB", capacity={"capacity_mva": 63.0, "voltage_kv": 110, "feeders": 7})
_add("a_ss_kumbalam", "KSEB 66 kV Substation Kumbalam", AssetType.SUBSTATION, 76.3020, 9.9010, elevation=1.8, criticality=0.72, zone="z_kumbalam", operator="KSEB", capacity={"capacity_mva": 40.0, "voltage_kv": 66, "feeders": 5})
_add("a_refinery_ss", "BPCL Kochi Refinery Captive Substation", AssetType.SUBSTATION, 76.3600, 9.9480, elevation=12.2, criticality=0.96, zone="z_ambalamugal", operator="BPCL", capacity={"capacity_mva": 150.0, "voltage_kv": 110, "feeders": 12})

# ---- water supply ----------------------------------------------------------
_add("a_wtp_aluva", "KWA Aluva Water Treatment Plant", AssetType.WATER_TREATMENT, 76.3480, 10.1040, elevation=8.6, criticality=0.97, zone="z_aluva", operator="KWA", capacity={"capacity_mld": 320.0, "intake_level_m": 6.2, "serves_population": 1_050_000})
_add("a_wtp_thevara", "KWA Thevara Booster Station", AssetType.WATER_TREATMENT, 76.2910, 9.9545, elevation=2.2, criticality=0.88, zone="z_thevara", operator="KWA", capacity={"capacity_mld": 90.0, "serves_population": 280_000})

# ---- telecom ---------------------------------------------------------------
_add("a_tel_kaloor", "Jio Tower Kaloor", AssetType.TELECOM_TOWER, 76.2890, 9.9930, elevation=4.0, criticality=0.8, zone="z_kaloor", operator="Reliance Jio", capacity={"coverage_km": 4.0, "backup_hours": 4.0})
_add("a_tel_vyttila", "Airtel Tower Vyttila", AssetType.TELECOM_TOWER, 76.3180, 9.9710, elevation=3.2, criticality=0.78, zone="z_vyttila", operator="Bharti Airtel", capacity={"coverage_km": 3.5, "backup_hours": 3.0})
_add("a_tel_elamkulam", "BSNL Exchange Elamkulam", AssetType.TELECOM_TOWER, 76.2960, 9.9730, elevation=2.7, criticality=0.82, zone="z_elamkulam", operator="BSNL", capacity={"coverage_km": 4.5, "backup_hours": 8.0, "type": "exchange"})
_add("a_tel_fortkochi", "Airtel Tower Fort Kochi", AssetType.TELECOM_TOWER, 76.2430, 9.9640, elevation=2.0, criticality=0.7, zone="z_fortkochi", operator="Bharti Airtel", capacity={"coverage_km": 3.0, "backup_hours": 2.0})
_add("a_tel_kakkanad", "Jio Tower Infopark Kakkanad", AssetType.TELECOM_TOWER, 76.3430, 10.0060, elevation=9.0, criticality=0.76, zone="z_kakkanad", operator="Reliance Jio", capacity={"coverage_km": 4.0, "backup_hours": 6.0})

# ---- hospitals -------------------------------------------------------------
HOSPITALS = [
    ("a_hosp_amrita", "Amrita Institute of Medical Sciences", 76.2960, 10.0260, 10.4, 0.97, "z_edappally", 1_300, 1_050, 240, 18.0, 12.0, 3, "a_ss_edappally"),
    ("a_hosp_general", "Ernakulam General Hospital", 76.2840, 9.9770, 3.4, 0.95, "z_mgroad", 640, 520, 110, 10.0, 8.0, 2, "a_ss_kaloor"),
    ("a_hosp_lisie", "Lisie Hospital Kaloor", 76.2880, 9.9890, 4.2, 0.92, "z_kaloor", 720, 610, 95, 12.0, 8.0, 2, "a_ss_kaloor"),
    ("a_hosp_aster", "Aster Medcity Cheranalloor", 76.2590, 10.0290, 3.0, 0.9, "z_cheranalloor", 670, 480, 130, 24.0, 16.0, 3, "a_ss_kaloor"),
    ("a_hosp_rajagiri", "Rajagiri Hospital Aluva", 76.3390, 10.0900, 9.2, 0.85, "z_aluva", 450, 320, 70, 16.0, 10.0, 2, "a_ss_aluva"),
    ("a_hosp_ig", "Indira Gandhi Co-operative Hospital", 76.3010, 9.9660, 3.4, 0.82, "z_kadavanthra", 380, 300, 60, 8.0, 6.0, 1, "a_ss_thevara"),
    ("a_hosp_lakeshore", "Lakeshore Hospital Nettoor", 76.3050, 9.9260, 2.4, 0.84, "z_maradu", 460, 350, 80, 14.0, 8.0, 2, "a_ss_thevara"),
    ("a_hosp_kims", "KIMS Hospital Kadavanthra", 76.2970, 9.9700, 3.6, 0.83, "z_kadavanthra", 420, 330, 65, 10.0, 6.0, 2, "a_ss_kaloor"),
    ("a_hosp_pvs", "PVS Memorial Hospital Kaloor", 76.2910, 9.9960, 4.4, 0.8, "z_kaloor", 320, 250, 55, 8.0, 6.0, 1, "a_ss_kaloor"),
    ("a_hosp_sam", "Samaritan Hospital Thrippunithura", 76.3380, 9.9430, 5.6, 0.72, "z_thrippunithura", 240, 160, 40, 6.0, 4.0, 1, "a_ss_kakkanad"),
]
for (
    hid, name, lon, lat, elev, crit, zone, beds, occ, vulnerable, oxygen_h, autonomy_h, trauma, power_from,
) in HOSPITALS:
    _add(
        hid, name, AssetType.HOSPITAL, lon, lat, elevation=elev, criticality=crit, zone=zone,
        operator="Private" if not name.startswith("Ernakulam") else "Government",
        capacity={
            "beds_total": beds, "beds_occupied": occ, "vulnerable_patients": vulnerable,
            "oxygen_hours": oxygen_h, "autonomy_hours": autonomy_h, "trauma_level": trauma, "helipad": trauma >= 3,
        },
        attributes={"power_from": power_from},
    )

# ---- shelters --------------------------------------------------------------
SHELTERS = [
    ("a_shl_jln", "Jawaharlal Nehru International Stadium", 76.2900, 9.9990, 4.6, "z_kaloor", 6_500, "a_ss_kaloor", True, True),
    ("a_shl_maharajas", "Maharaja's College Ernakulam", 76.2870, 9.9730, 3.1, "z_mgroad", 1_400, "a_ss_kaloor", True, True),
    ("a_shl_stteresas", "St. Teresa's College Ernakulam", 76.2860, 9.9770, 3.2, "z_mgroad", 900, "a_ss_kaloor", True, True),
    ("a_shl_cusat", "CUSAT Campus Kalamassery", 76.3260, 10.0450, 12.0, "z_kalamassery", 3_200, "a_ss_kalamassery", True, True),
    ("a_shl_sacred_heart", "Sacred Heart College Thevara", 76.2900, 9.9550, 2.3, "z_thevara", 1_100, "a_ss_thevara", True, True),
    ("a_shl_rajarajeshwari", "Rajagiri College of Social Sciences", 76.3080, 9.9160, 2.6, "z_kumbalam", 1_600, "a_ss_kumbalam", True, False),
    ("a_shl_ghss_kakkanad", "Govt. HSS Kakkanad", 76.3360, 10.0130, 8.8, "z_kakkanad", 800, "a_ss_kakkanad", True, True),
    ("a_shl_vyttila_hall", "Vyttila Community Hall", 76.3170, 9.9720, 3.1, "z_vyttila", 600, "a_ss_kaloor", False, False),
    ("a_shl_vypin_school", "Govt. HSS Njarakkal", 76.2280, 10.0080, 1.2, "z_vypin", 700, "a_ss_kaloor", False, False),
    ("a_shl_fortkochi_townhall", "Fort Kochi Town Hall", 76.2440, 9.9670, 1.8, "z_fortkochi", 450, "a_ss_thevara", True, False),
    ("a_shl_thrippunithura_govt", "Govt. Boys HSS Thrippunithura", 76.3370, 9.9460, 5.9, "z_thrippunithura", 650, "a_ss_kakkanad", True, True),
    ("a_shl_edappally", "Edappally Govt. LP School", 76.3040, 10.0230, 7.4, "z_edappally", 500, "a_ss_edappally", True, False),
]
for sid, name, lon, lat, elev, zone, cap, power_from, water, accessible in SHELTERS:
    _add(
        sid, name, AssetType.SHELTER, lon, lat, elevation=elev, criticality=0.6, zone=zone,
        operator="District Administration",
        capacity={"capacity_people": cap, "has_water": water, "accessible_for_disability": accessible, "opening_time_min": 45},
        attributes={"power_from": power_from},
    )

# ---- emergency services ----------------------------------------------------
_add("a_fire_club", "Kochi Fire & Rescue Station (Club Road)", AssetType.FIRE_STATION, 76.2870, 9.9800, elevation=3.3, criticality=0.9, zone="z_mgroad", operator="Kerala Fire & Rescue", capacity={"units": 5, "coverage_km": 6.0})
_add("a_fire_gandhinagar", "Gandhinagar Fire Station", AssetType.FIRE_STATION, 76.2850, 9.9600, elevation=2.8, criticality=0.78, zone="z_thevara", operator="Kerala Fire & Rescue", capacity={"units": 3, "coverage_km": 5.0})
_add("a_fire_kalamassery", "Kalamassery Fire Station", AssetType.FIRE_STATION, 76.3220, 10.0490, elevation=12.2, criticality=0.75, zone="z_kalamassery", operator="Kerala Fire & Rescue", capacity={"units": 2, "coverage_km": 7.0})
_add("a_police_central", "Central Police Station Ernakulam", AssetType.POLICE_STATION, 76.2840, 9.9760, elevation=3.4, criticality=0.76, zone="z_mgroad", operator="Kerala Police", capacity={"units": 4, "coverage_km": 5.0})
_add("a_police_kakkanad", "Kakkanad Police Station", AssetType.POLICE_STATION, 76.3420, 10.0090, elevation=9.0, criticality=0.7, zone="z_kakkanad", operator="Kerala Police", capacity={"units": 3, "coverage_km": 6.0})
_add("a_police_mattancherry", "Mattancherry Coastal Police Station", AssetType.POLICE_STATION, 76.2500, 9.9560, elevation=1.2, criticality=0.68, zone="z_mattancherry", operator="Kerala Coastal Police", capacity={"units": 2, "coverage_km": 4.0, "boats": 3})

# ---- transport / economy ---------------------------------------------------
_add("a_airport_cial", "Cochin International Airport (CIAL)", AssetType.AIRPORT, 76.4000, 10.1520, elevation=13.0, criticality=0.95, zone="z_aluva", operator="CIAL", capacity={"runway_elevation_m": 13.0, "annual_pax_m": 10.2})
_add("a_port_kochi", "Cochin Port (Willingdon Island)", AssetType.PORT, 76.2620, 9.9410, elevation=2.6, criticality=0.9, zone="z_island", operator="Cochin Port Authority", capacity={"berths": 9, "draft_m": 12.5, "storm_surge_threshold_m": 1.4})
_add("a_port_ictt", "Vallarpadam International Container Transshipment Terminal", AssetType.PORT, 76.2480, 9.9930, elevation=2.4, criticality=0.92, zone="z_vypin", operator="DP World", capacity={"berths": 3, "teu_capacity": 1_000_000})
_add("a_refinery", "BPCL Kochi Refinery", AssetType.REFINERY, 76.3620, 9.9460, elevation=12.4, criticality=0.97, zone="z_ambalamugal", operator="BPCL", capacity={"capacity_mmtpa": 15.5, "effluent_ponds": 4})
_add("a_waste_plant", "Brahmapuram MSW Treatment Plant", AssetType.WASTE_PLANT, 76.3750, 10.0180, elevation=18.0, criticality=0.7, zone="z_kakkanad", operator="Kochi Corporation", capacity={"capacity_tpd": 400})
_add("a_metro_aluva", "Kochi Metro - Aluva", AssetType.METRO_STATION, 76.3510, 10.1090, elevation=9.6, criticality=0.72, zone="z_aluva", operator="KMRL", capacity={"platforms": 2, "entrance_elevation_m": 9.6})
_add("a_metro_kalamassery", "Kochi Metro - Kalamassery", AssetType.METRO_STATION, 76.3210, 10.0520, elevation=11.9, criticality=0.7, zone="z_kalamassery", operator="KMRL", capacity={"platforms": 2, "entrance_elevation_m": 11.9})
_add("a_metro_cusat", "Kochi Metro - CUSAT", AssetType.METRO_STATION, 76.3250, 10.0460, elevation=12.1, criticality=0.68, zone="z_kalamassery", operator="KMRL", capacity={"platforms": 2})
_add("a_metro_edappally", "Kochi Metro - Edappally", AssetType.METRO_STATION, 76.3060, 10.0260, elevation=7.5, criticality=0.74, zone="z_edappally", operator="KMRL", capacity={"platforms": 2, "interchange": "bike_taxi_hub"})
_add("a_metro_palarivattom", "Kochi Metro - Palarivattom", AssetType.METRO_STATION, 76.3090, 10.0040, elevation=5.4, criticality=0.7, zone="z_palarivattom", operator="KMRL", capacity={"platforms": 2})
_add("a_metro_kaloor", "Kochi Metro - Kaloor", AssetType.METRO_STATION, 76.2910, 9.9930, elevation=4.2, criticality=0.72, zone="z_kaloor", operator="KMRL", capacity={"platforms": 2, "nearby_gateway": "district_collectorate"})
_add("a_metro_mgroad", "Kochi Metro - M.G. Road", AssetType.METRO_STATION, 76.2890, 9.9790, elevation=3.4, criticality=0.7, zone="z_mgroad", operator="KMRL", capacity={"platforms": 2})
_add("a_metro_vyttila", "Kochi Metro - Vyttila", AssetType.METRO_STATION, 76.3180, 9.9680, elevation=3.2, criticality=0.76, zone="z_vyttila", operator="KMRL", capacity={"platforms": 2, "interchange": "ksrtc_mobility_hub"})
_add("a_rail_south", "Ernakulam Junction Railway Station", AssetType.METRO_STATION, 76.2890, 9.9680, elevation=3.0, criticality=0.85, zone="z_elamkulam", subtype="railway", operator="Southern Railway", capacity={"platforms": 6, "daily_pax": 60_000})
_add("a_rail_north", "Ernakulam Town Railway Station", AssetType.METRO_STATION, 76.2870, 10.0000, elevation=4.3, criticality=0.8, zone="z_kaloor", subtype="railway", operator="Southern Railway", capacity={"platforms": 3, "daily_pax": 32_000})

# ---- roads & bridges -------------------------------------------------------
# (traffic-capacity roads are the mobility skeleton; free-flow speed drives ETA)
ROADS = [
    # id, name, points, elevation, criticality, lanes, length_km, flood_threshold, evacuation, underpass
    ("a_road_nh544", "NH 544 Salem-Kochi (Aluva to Edappally)", [(76.3540, 10.1080), (76.3400, 10.0800), (76.3300, 10.0600), (76.3200, 10.0450), (76.3090, 10.0270), (76.3050, 10.0250)], 9.8, 0.97, 6, 11.2, 0.35, True, False),
    ("a_road_nh66", "NH 66 Coastal (Edappally to Kumbalam)", [(76.3050, 10.0250), (76.3060, 10.0060), (76.3120, 9.9880), (76.3180, 9.9700), (76.3160, 9.9500), (76.3050, 9.9260), (76.3040, 9.9050)], 3.6, 0.96, 6, 14.8, 0.35, True, False),
    ("a_road_bypass_vyttila", "Kochi Bypass - Vyttila Interchange", [(76.3180, 9.9700), (76.3200, 9.9600), (76.3140, 9.9450), (76.3050, 9.9260)], 3.1, 0.94, 4, 5.6, 0.4, True, False),
    ("a_road_spap", "Seaport-Airport Road (SPAP)", [(76.3050, 10.0250), (76.3180, 10.0060), (76.3300, 9.9950), (76.3430, 9.9880), (76.3550, 9.9930), (76.3520, 10.0600), (76.3540, 10.1080)], 6.4, 0.9, 4, 18.4, 0.4, True, False),
    ("a_road_kk", "Kaloor - Kadavanthra Road", [(76.2900, 9.9930), (76.2940, 9.9850), (76.2990, 9.9720), (76.3010, 9.9680)], 3.6, 0.88, 4, 4.2, 0.3, True, False),
    ("a_road_sa", "S.A. Road (Vyttila to Kadavanthra)", [(76.3180, 9.9700), (76.3080, 9.9680), (76.3010, 9.9680)], 3.2, 0.85, 4, 2.4, 0.3, True, False),
    ("a_road_banerjee", "Banerjee Road", [(76.2860, 9.9760), (76.2890, 9.9790), (76.2900, 9.9830)], 3.2, 0.72, 4, 1.1, 0.25, False, False),
    ("a_road_mg", "M.G. Road", [(76.2840, 9.9720), (76.2870, 9.9760), (76.2890, 9.9800), (76.2910, 9.9850), (76.2920, 9.9890)], 3.3, 0.8, 4, 2.6, 0.28, False, True),
    ("a_road_chittoor", "Chittoor Road", [(76.2830, 9.9700), (76.2860, 9.9760), (76.2890, 9.9830), (76.2920, 9.9900)], 3.4, 0.74, 4, 2.5, 0.3, False, False),
    ("a_road_market", "Market Road", [(76.2820, 9.9740), (76.2860, 9.9780), (76.2880, 9.9820)], 3.2, 0.66, 2, 0.9, 0.25, False, False),
    ("a_road_thevara_ferry", "Thevara Ferry Road", [(76.2890, 9.9520), (76.2890, 9.9600), (76.2910, 9.9680)], 2.4, 0.8, 2, 1.8, 0.25, True, False),
    ("a_road_container", "Container Road (Kalamassery - Vallarpadam)", [(76.3200, 10.0450), (76.3050, 10.0300), (76.2850, 10.0200), (76.2600, 10.0100), (76.2480, 9.9930)], 5.2, 0.93, 4, 14.2, 0.4, False, False),
    ("a_road_goshree", "Goshree Road (Vypin link)", [(76.2480, 9.9930), (76.2450, 9.9750), (76.2420, 9.9660)], 2.2, 0.86, 4, 5.4, 0.35, True, False),
    ("a_road_fortkochi", "Fort Kochi - Mattancherry Road", [(76.2420, 9.9670), (76.2460, 9.9620), (76.2510, 9.9570)], 1.8, 0.8, 2, 1.9, 0.2, True, False),
    ("a_road_mattancherry_palace", "Mattancherry Palace Road", [(76.2510, 9.9570), (76.2540, 9.9540)], 1.4, 0.6, 2, 0.8, 0.2, False, False),
    ("a_road_kumbalam", "Kumbalam - Aroor Road", [(76.3040, 9.9050), (76.3100, 9.8980)], 1.6, 0.78, 4, 1.4, 0.3, True, False),
    ("a_road_aluva_main", "Aluva Main Road (Marthanda Varma)", [(76.3510, 10.1090), (76.3540, 10.1060), (76.3560, 10.1000)], 9.5, 0.82, 4, 1.6, 0.35, False, False),
    ("a_road_kakkanad_infopark", "Infopark Expressway", [(76.3300, 9.9950), (76.3390, 10.0060), (76.3450, 10.0140)], 9.2, 0.7, 4, 2.2, 0.4, False, False),
]
for rid, name, pts, elev, crit, lanes, length_km, thr, evac, under in ROADS:
    _add(
        rid, name, AssetType.ROAD, pts[0][0], pts[0][1], elevation=elev, criticality=crit,
        zone=None, subtype="highway" if "NH" in name else "arterial",
        operator="NHAI" if "NH" in name else "Kochi Corporation",
        capacity={
            "lanes": lanes, "length_m": length_km * 1000, "free_flow_speed_kmh": 55.0 if "NH" in name else 35.0,
            "flood_threshold_m": thr, "heavy_threshold_m": round(thr + 0.25, 2),
            "is_evacuation_route": evac, "underpass": under,
        },
        geometry=_line(pts),
    )

BRIDGES = [
    ("a_bridge_venduruthy", "Venduruthy Bridge", 76.2760, 9.9450, 2.9, 0.93, "a_road_bypass_vyttila", 0.5),
    ("a_bridge_goshree", "Goshree Bridge (Vypin)", 76.2440, 9.9800, 1.9, 0.88, "a_road_goshree", 0.6),
    ("a_bridge_mattancherry", "Mattancherry Bridge", 76.2510, 9.9530, 1.5, 0.7, "a_road_fortkochi", 0.4),
    ("a_bridge_kundannoor", "Kundannoor Bridge", 76.3140, 9.9440, 2.6, 0.9, "a_road_bypass_vyttila", 0.5),
    ("a_bridge_palarivattom", "Palarivattom Bridge", 76.3080, 10.0020, 5.2, 0.8, "a_road_nh66", 0.45),
    ("a_bridge_marthanda_varma", "Marthanda Varma Bridge (Aluva)", 76.3545, 10.1065, 9.6, 0.86, "a_road_aluva_main", 0.6),
    ("a_bridge_varapuzha", "Varapuzha Bridge", 76.2790, 10.0710, 3.4, 0.74, "a_road_container", 0.5),
    ("a_bridge_kaniyampuzha", "Kaniyampuzha Bridge", 76.3360, 9.9540, 5.3, 0.72, "a_road_bypass_vyttila", 0.45),
    ("a_bridge_eloor", "Eloor Bridge", 76.2900, 10.0770, 4.4, 0.7, "a_road_container", 0.5),
]
for bid, name, lon, lat, elev, crit, parent, thr in BRIDGES:
    _add(
        bid, name, AssetType.BRIDGE, lon, lat, elevation=elev, criticality=crit, zone=None,
        subtype="bridge", operator="PWD",
        capacity={"free_flow_speed_kmh": 30.0, "flood_threshold_m": thr, "heavy_threshold_m": thr + 0.3},
        attributes={"replaces_road": parent},
    )

# ---- irrigation / regulator structures ------------------------------------
_add("a_regulator_bhoothathankettu", "Bhoothathankettu Dam Regulator", AssetType.RIVER_GAUGE, 76.3960, 10.1300, elevation=24.0, criticality=0.94, zone="z_aluva", subtype="dam", operator="KSEB", capacity={"reservoir_capacity_mcm": 26.0, "spillway_m3s": 1_100.0, "release_notice_min": 120})
_add("a_regulator_kaniyampuzha", "Kaniyampuzha Regulator", AssetType.CANAL, 76.3400, 9.9600, elevation=5.0, criticality=0.8, zone="z_thrippunithura", subtype="regulator", operator="Irrigation Dept", capacity={"gate_count": 4})
_add("a_regulator_manjummel", "Manjummel Regulator (Periyar)", AssetType.CANAL, 76.2950, 10.0700, elevation=4.0, criticality=0.78, zone="z_kalamassery", subtype="regulator", operator="Irrigation Dept", capacity={"gate_count": 6})

# ---- street-level flood sensors (depth) -----------------------------------
FLOOD_SENSORS = [
    ("a_fs_elamkulam", "Elamkulam Low-Lying Pocket", 76.2962, 9.9710, 2.3, 0.9, "z_elamkulam"),
    ("a_fs_thevara", "Thevara Ferry Junction", 76.2892, 9.9540, 2.1, 0.88, "z_thevara"),
    ("a_fs_kadavanthra", "Kadavanthra Underpass", 76.3000, 9.9670, 3.0, 0.86, "z_kadavanthra"),
    ("a_fs_mg_road", "M.G. Road Underpass", 76.2885, 9.9810, 3.1, 0.84, "z_mgroad"),
    ("a_fs_vyttila", "Vyttila Junction", 76.3180, 9.9710, 3.0, 0.88, "z_vyttila"),
    ("a_fs_kumbalam", "Kumbalam Causeway", 76.3060, 9.9070, 1.5, 0.8, "z_kumbalam"),
    ("a_fs_fortkochi", "Fort Kochi Beach Road", 76.2425, 9.9665, 1.5, 0.78, "z_fortkochi"),
    ("a_fs_mattancherry", "Mattancherry Bazaar Road", 76.2505, 9.9575, 1.2, 0.8, "z_mattancherry"),
    ("a_fs_cheranalloor", "Cheranalloor Market", 76.2580, 10.0280, 2.0, 0.72, "z_cheranalloor"),
    ("a_fs_edappally", "Edappally Toll Approach", 76.3060, 10.0230, 7.3, 0.7, "z_edappally"),
    ("a_fs_kaloor", "Kaloor Bus Stand", 76.2905, 9.9945, 4.0, 0.82, "z_kaloor"),
    ("a_fs_aluva", "Aluva Manappuram", 76.3535, 10.1075, 9.0, 0.76, "z_aluva"),
]
for sid, name, lon, lat, elev, crit, zone in FLOOD_SENSORS:
    _add(
        sid, name, AssetType.RAIN_GAUGE, lon, lat, elevation=elev, criticality=crit, zone=zone,
        subtype="street_flood_sensor", operator="Kochi Corporation",
        capacity={"instrument_max_depth_m": 3.0, "trigger_watch_m": 0.15, "trigger_warn_m": 0.3, "trigger_alert_m": 0.5},
    )


# --------------------------------------------------------------------------- #
# sensors
# --------------------------------------------------------------------------- #
def _sensors() -> list[dict[str, Any]]:
    sensors: list[dict[str, Any]] = []
    rain_gauges = [
        ("s_rain_kalamassery", "Kalamassery Rain Gauge", 76.3210, 10.0490, "z_kalamassery"),
        ("s_rain_aluva", "Aluva Rain Gauge", 76.3520, 10.1050, "z_aluva"),
        ("s_rain_edappally", "Edappally Rain Gauge", 76.3060, 10.0260, "z_edappally"),
        ("s_rain_kakkanad", "Kakkanad Rain Gauge", 76.3400, 10.0070, "z_kakkanad"),
        ("s_rain_thevara", "Thevara Rain Gauge", 76.2900, 9.9540, "z_thevara"),
        ("s_rain_kochi_port", "Kochi Port Rain Gauge", 76.2620, 9.9420, "z_island"),
        ("s_rain_vypin", "Vypin Rain Gauge", 76.2320, 10.0050, "z_vypin"),
        ("s_rain_vyttila", "Vyttila Rain Gauge", 76.3170, 9.9710, "z_vyttila"),
        ("s_rain_mgroad", "M.G. Road Rain Gauge", 76.2880, 9.9790, "z_mgroad"),
        ("s_rain_ambalamugal", "Ambalamugal Rain Gauge", 76.3620, 9.9460, "z_ambalamugal"),
    ]
    for sid, name, lon, lat, zone in rain_gauges:
        sensors.append(
            {
                "id": sid, "name": name, "metric": "rainfall_mm_h", "unit": "mm/h",
                "lon": lon, "lat": lat, "zone": zone, "provider": "IMD",
                "asset_id": None, "expected_min": 0.0, "expected_max": 180.0,
            }
        )
    levels = [
        ("s_lvl_periyar_aluva", "Periyar Level @ Marthanda Varma Bridge", "a_periyar_aluva", "river_level_m", 4.6, 5.6),
        ("s_lvl_periyar_eloor", "Periyar Level @ Eloor", "a_periyar_eloor", "river_level_m", 3.6, 4.4),
        ("s_lvl_backwater", "Vembanad Backwater Level @ Kumbalam", "a_backwater_kumbalam", "water_level_m", 1.0, 1.6),
        ("s_lvl_thevara_canal", "Thevara Canal Level", "a_canal_thevara_perandoor", "water_level_m", 2.4, 2.9),
        ("s_lvl_perandoor_canal", "Perandoor Canal Level", "a_canal_perandoor", "water_level_m", 2.2, 2.7),
        ("s_lvl_kaniyampuzha", "Kaniyampuzha River Level", "a_kaniyampuzha", "river_level_m", 2.8, 3.4),
        ("s_lvl_edappally_canal", "Edappally Canal Level", "a_canal_edappally", "water_level_m", 2.6, 3.1),
        ("s_lvl_chilavannoor", "Chilavannoor Canal Level", "a_canal_chilavannoor", "water_level_m", 2.1, 2.6),
        ("s_lvl_muvattupuzha", "Muvattupuzha River Level", "a_muvattupuzha_river", "river_level_m", 4.5, 5.5),
        ("s_lvl_mattancherry", "Mattancherry Channel Level", "a_canal_kadamakudy", "water_level_m", 0.9, 1.5),
    ]
    for sid, name, asset_id, metric, warn, danger in levels:
        asset = next(a for a in ASSETS if a["id"] == asset_id)
        sensors.append(
            {
                "id": sid, "name": name, "metric": metric, "unit": "m",
                "lon": asset["lon"], "lat": asset["lat"], "zone": asset["zone"],
                "provider": "Irrigation Dept" if "river" in metric else "Kochi Corporation",
                "asset_id": asset_id,
                "expected_min": 0.0,
                "expected_max": danger + 1.5,
                "attributes": {"warn_level_m": warn, "danger_level_m": danger},
            }
        )
    sensors.append(
        {
            "id": "s_tide_kochi", "name": "Kochi Port Tide Gauge", "metric": "tide_m", "unit": "m",
            "lon": 76.2625, "lat": 9.9415, "zone": "z_island", "provider": "Cochin Port Authority",
            "asset_id": "a_port_kochi", "expected_min": -0.5, "expected_max": 2.2,
        }
    )
    sensors.append(
        {
            "id": "s_tide_vypin", "name": "Vypin Tidal Station", "metric": "tide_m", "unit": "m",
            "lon": 76.2290, "lat": 10.0020, "zone": "z_vypin", "provider": "Port Dept",
            "asset_id": None, "expected_min": -0.5, "expected_max": 2.3,
        }
    )
    for sid, name, lon, lat, elev, crit, zone in FLOOD_SENSORS:
        sensors.append(
            {
                "id": f"s_depth_{sid[5:]}", "name": f"{name} Depth Sensor", "metric": "flood_depth_m", "unit": "m",
                "lon": lon, "lat": lat, "zone": zone, "provider": "Kochi Corporation IoT",
                "asset_id": sid, "expected_min": 0.0, "expected_max": 2.5,
            }
        )
    sensors.extend(
        [
            {"id": "s_power_kaloor_ss", "name": "Kaloor Substation Load", "metric": "power_kw", "unit": "kW", "lon": 76.2870, "lat": 9.9910, "zone": "z_kaloor", "provider": "KSEB SCADA", "asset_id": "a_ss_kaloor", "expected_min": 20_000.0, "expected_max": 180_000.0},
            {"id": "s_power_thevara_ss", "name": "Thevara Substation Load", "metric": "power_kw", "unit": "kW", "lon": 76.2870, "lat": 9.9480, "zone": "z_thevara", "provider": "KSEB SCADA", "asset_id": "a_ss_thevara", "expected_min": 10_000.0, "expected_max": 60_000.0},
            {"id": "s_pump_elamkulam_flow", "name": "Elamkulam Pump Discharge", "metric": "pump_rate_m3s", "unit": "m3/s", "lon": 76.2970, "lat": 9.9700, "zone": "z_elamkulam", "provider": "Kochi Corporation", "asset_id": "a_pump_elamkulam", "expected_min": 0.0, "expected_max": 6.5},
            {"id": "s_pump_thevara_flow", "name": "Thevara Pump Discharge", "metric": "pump_rate_m3s", "unit": "m3/s", "lon": 76.2900, "lat": 9.9530, "zone": "z_thevara", "provider": "Kochi Corporation", "asset_id": "a_pump_thevara", "expected_min": 0.0, "expected_max": 5.0},
            {"id": "s_traffic_vyttila", "name": "Vyttila Average Corridor Speed", "metric": "traffic_speed_kmh", "unit": "km/h", "lon": 76.3180, "lat": 9.9705, "zone": "z_vyttila", "provider": "Kochi Traffic Police", "asset_id": "a_road_nh66", "expected_min": 4.0, "expected_max": 60.0},
            {"id": "s_traffic_sa", "name": "S.A. Road Average Corridor Speed", "metric": "traffic_speed_kmh", "unit": "km/h", "lon": 76.3080, "lat": 9.9680, "zone": "z_kadavanthra", "provider": "Kochi Traffic Police", "asset_id": "a_road_sa", "expected_min": 3.0, "expected_max": 45.0},
        ]
    )
    return sensors


SENSORS: list[dict[str, Any]] = _sensors()


# --------------------------------------------------------------------------- #
# resources
# --------------------------------------------------------------------------- #
def _resources() -> list[dict[str, Any]]:
    spec = [
        ("res_pump_elamkulam", "Elamkulam Mobile Pump Set", ResourceType.PUMP, "a_pump_elamkulam", 6, {"rate_m3s": 0.6}, 0.9),
        ("res_pump_thevara", "Thevara Mobile Pump Set", ResourceType.PUMP, "a_pump_thevara", 4, {"rate_m3s": 0.5}, 1.2),
        ("res_pump_north", "North Zone Dewatering Pumps", ResourceType.PUMP, "a_fire_kalamassery", 5, {"rate_m3s": 0.4}, 2.4),
        ("res_boat_fortkochi", "Fort Kochi Rescue Boats", ResourceType.BOAT, "a_police_mattancherry", 8, {"capacity_pax": 8}, 1.0),
        ("res_boat_vypin", "Vypin Fishing Boat Volunteers", ResourceType.BOAT, "a_shl_vypin_school", 12, {"capacity_pax": 10}, 1.8),
        ("res_boat_kumbalam", "Kumbalam Rescue Boats", ResourceType.BOAT, "a_pump_kumbalam", 6, {"capacity_pax": 8}, 1.4),
        ("res_amb_central", "Central Ambulance Fleet", ResourceType.AMBULANCE, "a_hosp_general", 14, {"als": 6, "bls": 8}, 0.8),
        ("res_amb_amrita", "Amrita Ambulance Fleet", ResourceType.AMBULANCE, "a_hosp_amrita", 10, {"als": 4, "bls": 6}, 1.0),
        ("res_amb_south", "South Ambulance Fleet", ResourceType.AMBULANCE, "a_hosp_lakeshore", 8, {"als": 3, "bls": 5}, 1.3),
        ("res_rescue_ndrf", "NDRF 4th Battalion Detachment", ResourceType.RESCUE_TEAM, "a_fire_club", 3, {"swift_water": True, "cutting": True}, 1.0),
        ("res_rescue_civil", "Civil Defence Rapid Team", ResourceType.RESCUE_TEAM, "a_police_central", 6, {"swift_water": False, "lifeguard": True}, 1.0),
        ("res_fire_units", "Fire & Rescue Units", ResourceType.FIRE_UNIT, "a_fire_club", 10, {"pumping": True, "height_m": 34}, 0.7),
        ("res_sandbag", "Sandbag & Geobag Units", ResourceType.SANDBAG_UNIT, "a_waste_plant", 16, {"bags_per_hour": 400}, 2.0),
        ("res_gen_hospitals", "Hospital Backup Generators", ResourceType.GENERATOR, "a_hosp_general", 12, {"kva": 250}, 1.5),
        ("res_gen_pumps", "Pump Station Generator Trailers", ResourceType.GENERATOR, "a_pump_vennala", 8, {"kva": 500}, 2.0),
        ("res_drone_survey", "UAV Flood Survey Detachment", ResourceType.DRONE, "a_metro_kaloor", 4, {"thermal": True, "endurance_min": 38}, 0.9),
        ("res_mobile_tower", "Mobile Telecom Towers (CoW)", ResourceType.MOBILE_TOWER, "a_tel_kaloor", 3, {"coverage_km": 3.0}, 2.4),
    ]
    return [
        {
            "id": rid, "name": name, "resource_type": rt, "home_asset_id": home,
            "quantity": float(qty), "available": float(qty), "capabilities": caps,
            "status": "available", "attributes": {"response_time_median_min": eta * 10},
        }
        for rid, name, rt, home, qty, caps, eta in spec
    ]


RESOURCES: list[dict[str, Any]] = _resources()




# --------------------------------------------------------------------------- #
# zone attribution: every object belongs to a ward, so that ward-level ponding
# (and therefore ward-level accountability) reaches the mobility network too
# --------------------------------------------------------------------------- #
def _assign_zones() -> None:
    from ..fusion.normalize import haversine_m as _hav

    for asset in ASSETS:
        if asset.get("zone"):
            continue
        nearest = min(ZONES, key=lambda z: _hav(asset["lon"], asset["lat"], z["lon"], z["lat"]))
        asset["zone"] = nearest["id"]
        asset.setdefault("attributes", {})["zone_assignment"] = "nearest_centroid"


_assign_zones()


def _attach_ward_design_rainfall() -> None:
    """Wards declare the rainfall intensity their drainage was designed for.

    Carrying it on the assets keeps the hydrology engine free of a second lookup
    table and makes the assumption visible in the twin itself - which is what
    "explainable" means in practice.
    """
    design = {z["id"]: z.get("design_rainfall_mm_h") for z in ZONES}
    for asset in ASSETS:
        zone_id = asset.get("zone")
        if zone_id in design:
            asset.setdefault("attributes", {})["design_rainfall_mm_h"] = design[zone_id]


_attach_ward_design_rainfall()


# --------------------------------------------------------------------------- #
# relationships - built deterministically from geometry + explicit engineering
# --------------------------------------------------------------------------- #

#: the physical/operational chain that makes Kochi flood the way it does.
EXPLICIT_EDGES: list[tuple[str, str, RelationKind, dict[str, Any]]] = [
    # reservoir release -> river -> backwater: a *delayed* cascade with lead time
    ("a_regulator_bhoothathankettu", "a_periyar_aluva", RelationKind.RELEASES, {"lead_time_min": 90, "attenuation": 0.85, "criticality": 0.95, "failure_mode": FailureMode.DELAYED}),
    ("a_periyar_aluva", "a_periyar_eloor", RelationKind.FEEDS, {"lead_time_min": 45, "attenuation": 0.9, "criticality": 0.9}),
    ("a_periyar_eloor", "a_backwater_kumbalam", RelationKind.FEEDS, {"lead_time_min": 60, "attenuation": 0.75, "criticality": 0.85}),
    ("a_muvattupuzha_river", "a_kaniyampuzha", RelationKind.FEEDS, {"lead_time_min": 40, "attenuation": 0.8, "criticality": 0.8}),
    ("a_kaniyampuzha", "a_backwater_kumbalam", RelationKind.FEEDS, {"lead_time_min": 55, "attenuation": 0.7, "criticality": 0.78}),
    ("a_canal_edappally", "a_canal_perandoor", RelationKind.FEEDS, {"lead_time_min": 30, "attenuation": 0.85, "criticality": 0.8}),
    ("a_canal_perandoor", "a_canal_thevara_perandoor", RelationKind.FEEDS, {"lead_time_min": 20, "attenuation": 0.9, "criticality": 0.85}),
    ("a_canal_chilavannoor", "a_canal_thevara_perandoor", RelationKind.FEEDS, {"lead_time_min": 15, "attenuation": 0.85, "criticality": 0.75}),
    ("a_canal_thevara_perandoor", "a_backwater_kumbalam", RelationKind.FEEDS, {"lead_time_min": 25, "attenuation": 0.9, "criticality": 0.88}),
    ("a_canal_kadamakudy", "a_backwater_kumbalam", RelationKind.FEEDS, {"lead_time_min": 20, "attenuation": 0.8, "criticality": 0.7}),
    ("a_chitrapuzha", "a_periyar_eloor", RelationKind.FEEDS, {"lead_time_min": 15, "attenuation": 0.8, "criticality": 0.68}),
    ("a_regulator_kaniyampuzha", "a_kaniyampuzha", RelationKind.RELEASES, {"lead_time_min": 30, "attenuation": 0.8}),
    ("a_regulator_manjummel", "a_periyar_eloor", RelationKind.RELEASES, {"lead_time_min": 25, "attenuation": 0.85}),
    # pumps drain canals (the intervention target for "deploy pump")
    ("a_pump_elamkulam", "a_canal_thevara_perandoor", RelationKind.DRAINS, {"criticality": 0.95, "attributes": {"capacity_m3s": 6.0}}),
    ("a_pump_thevara", "a_canal_thevara_perandoor", RelationKind.DRAINS, {"criticality": 0.9, "attributes": {"capacity_m3s": 4.5}}),
    ("a_pump_kaloor", "a_canal_perandoor", RelationKind.DRAINS, {"criticality": 0.82, "attributes": {"capacity_m3s": 3.0}}),
    ("a_pump_vennala", "a_canal_chilavannoor", RelationKind.DRAINS, {"criticality": 0.85, "attributes": {"capacity_m3s": 5.0}}),
    ("a_pump_kumbalam", "a_backwater_kumbalam", RelationKind.DRAINS, {"criticality": 0.8, "attributes": {"capacity_m3s": 3.5}}),
    ("a_pump_vyttila", "a_canal_chilavannoor", RelationKind.DRAINS, {"criticality": 0.86, "attributes": {"capacity_m3s": 4.0}}),
    ("a_canal_thevara_perandoor", "a_pump_elamkulam", RelationKind.MONITORS, {"criticality": 0.9}),
    ("a_backwater_kumbalam", "a_pump_kumbalam", RelationKind.MONITORS, {"criticality": 0.8}),
    # power chain: 220 kV feeds the city, everything depends on it
    ("a_ss_kaloor", "a_ss_thevara", RelationKind.POWERS, {"criticality": 0.9, "attenuation": 0.98, "recovery_hours": 8.0}),
    ("a_ss_kaloor", "a_ss_edappally", RelationKind.POWERS, {"criticality": 0.85, "recovery_hours": 8.0}),
    ("a_ss_kaloor", "a_ss_kakkanad", RelationKind.POWERS, {"criticality": 0.85, "recovery_hours": 8.0}),
    ("a_ss_kalamassery", "a_ss_aluva", RelationKind.POWERS, {"criticality": 0.8, "recovery_hours": 6.0}),
    ("a_ss_kaloor", "a_ss_kalamassery", RelationKind.POWERS, {"criticality": 0.82, "recovery_hours": 8.0}),
    ("a_ss_thevara", "a_refinery_ss", RelationKind.POWERS, {"criticality": 0.85, "recovery_hours": 10.0}),
    ("a_ss_kaloor", "a_wtp_aluva", RelationKind.POWERS, {"criticality": 0.95, "failure_mode": FailureMode.DELAYED, "lead_time_min": 60, "recovery_hours": 12.0}),
    # water supply chain
    ("a_wtp_aluva", "a_wtp_thevara", RelationKind.SUPPLIES_WATER, {"criticality": 0.9, "lead_time_min": 45, "attenuation": 0.9}),
    # telecom coverage of emergency response
    ("a_tel_kaloor", "a_police_central", RelationKind.COVERS, {"criticality": 0.7}),
    ("a_tel_elamkulam", "a_hosp_general", RelationKind.COVERS, {"criticality": 0.65}),
    ("a_tel_vyttila", "a_metro_vyttila", RelationKind.COVERS, {"criticality": 0.6}),
    # critical access roads into hospitals (these become the evacuation questions)
    ("a_road_nh66", "a_hosp_lakeshore", RelationKind.ACCESSES, {"criticality": 0.8, "lead_time_min": 8}),
    ("a_road_kk", "a_hosp_lisie", RelationKind.ACCESSES, {"criticality": 0.85, "lead_time_min": 6}),
    ("a_road_mg", "a_hosp_general", RelationKind.ACCESSES, {"criticality": 0.9, "lead_time_min": 5}),
    ("a_road_nh544", "a_hosp_amrita", RelationKind.ACCESSES, {"criticality": 0.9, "lead_time_min": 6}),
    ("a_road_thevara_ferry", "a_hosp_ig", RelationKind.ACCESSES, {"criticality": 0.85, "lead_time_min": 7}),
    ("a_road_sa", "a_hosp_kims", RelationKind.ACCESSES, {"criticality": 0.85, "lead_time_min": 6}),
    ("a_road_bypass_vyttila", "a_hosp_lakeshore", RelationKind.ACCESSES, {"criticality": 0.8, "lead_time_min": 9}),
    ("a_road_aluva_main", "a_hosp_rajagiri", RelationKind.ACCESSES, {"criticality": 0.8, "lead_time_min": 8}),
    ("a_road_goshree", "a_hosp_aster", RelationKind.ACCESSES, {"criticality": 0.75, "lead_time_min": 10}),
    ("a_road_kk", "a_hosp_pvs", RelationKind.ACCESSES, {"criticality": 0.7, "lead_time_min": 5}),
    # bridges are single points of failure on their corridor
    ("a_bridge_venduruthy", "a_road_bypass_vyttila", RelationKind.CROSSES, {"criticality": 0.9, "attenuation": 1.0}),
    ("a_bridge_goshree", "a_road_goshree", RelationKind.CROSSES, {"criticality": 0.88}),
    ("a_bridge_kundannoor", "a_road_bypass_vyttila", RelationKind.CROSSES, {"criticality": 0.88}),
    ("a_bridge_mattancherry", "a_road_fortkochi", RelationKind.CROSSES, {"criticality": 0.7}),
    ("a_bridge_palarivattom", "a_road_nh66", RelationKind.CROSSES, {"criticality": 0.8}),
    ("a_bridge_marthanda_varma", "a_road_aluva_main", RelationKind.CROSSES, {"criticality": 0.86}),
    ("a_bridge_varapuzha", "a_road_container", RelationKind.CROSSES, {"criticality": 0.74}),
    ("a_bridge_kaniyampuzha", "a_road_bypass_vyttila", RelationKind.CROSSES, {"criticality": 0.72}),
    ("a_bridge_eloor", "a_road_container", RelationKind.CROSSES, {"criticality": 0.7}),
    # road continuity: the mobility skeleton used for ETA / isolation analysis
    ("a_road_nh544", "a_road_nh66", RelationKind.CONNECTS, {"criticality": 0.95, "lead_time_min": 4}),
    ("a_road_nh66", "a_road_bypass_vyttila", RelationKind.CONNECTS, {"criticality": 0.9, "lead_time_min": 5}),
    ("a_road_bypass_vyttila", "a_road_sa", RelationKind.CONNECTS, {"criticality": 0.85, "lead_time_min": 4}),
    ("a_road_sa", "a_road_kk", RelationKind.CONNECTS, {"criticality": 0.88, "lead_time_min": 5}),
    ("a_road_kk", "a_road_mg", RelationKind.CONNECTS, {"criticality": 0.85, "lead_time_min": 4}),
    ("a_road_mg", "a_road_banerjee", RelationKind.CONNECTS, {"criticality": 0.8, "lead_time_min": 3}),
    ("a_road_mg", "a_road_chittoor", RelationKind.CONNECTS, {"criticality": 0.78, "lead_time_min": 3}),
    ("a_road_banerjee", "a_road_market", RelationKind.CONNECTS, {"criticality": 0.7, "lead_time_min": 3}),
    ("a_road_kk", "a_road_thevara_ferry", RelationKind.CONNECTS, {"criticality": 0.82, "lead_time_min": 5}),
    ("a_road_thevara_ferry", "a_road_fortkochi", RelationKind.CONNECTS, {"criticality": 0.7, "lead_time_min": 12}),
    ("a_road_goshree", "a_road_container", RelationKind.CONNECTS, {"criticality": 0.85, "lead_time_min": 8}),
    ("a_road_container", "a_road_nh544", RelationKind.CONNECTS, {"criticality": 0.85, "lead_time_min": 8}),
    ("a_road_spap", "a_road_nh544", RelationKind.CONNECTS, {"criticality": 0.88, "lead_time_min": 6}),
    ("a_road_spap", "a_road_nh66", RelationKind.CONNECTS, {"criticality": 0.85, "lead_time_min": 6}),
    ("a_road_bypass_vyttila", "a_road_kumbalam", RelationKind.CONNECTS, {"criticality": 0.8, "lead_time_min": 6}),
    ("a_road_nh66", "a_road_bypass_vyttila", RelationKind.CONNECTS, {"criticality": 0.9, "lead_time_min": 5}),
    ("a_road_fortkochi", "a_road_mattancherry_palace", RelationKind.CONNECTS, {"criticality": 0.6, "lead_time_min": 3}),
    ("a_road_aluva_main", "a_road_nh544", RelationKind.CONNECTS, {"criticality": 0.82, "lead_time_min": 4}),
    ("a_road_kakkanad_infopark", "a_road_spap", RelationKind.CONNECTS, {"criticality": 0.7, "lead_time_min": 5}),
    # hospitals depend on their backup generator chain
    ("a_hosp_general", "a_tel_elamkulam", RelationKind.DEPENDS_ON, {"criticality": 0.6, "attributes": {"mechanism": "comms"}}),
]

#: candidate mechanism labels used to make cascade narratives readable
MECHANISM_BY_KIND: dict[RelationKind, str] = {
    RelationKind.CONNECTS: "corridor discontinuity",
    RelationKind.CROSSES: "bridge closure",
    RelationKind.ACCESSES: "access route loss",
    RelationKind.POWERS: "electrical supply loss",
    RelationKind.SUPPLIES_WATER: "water supply interruption",
    RelationKind.COVERS: "communications coverage loss",
    RelationKind.DRAINS: "drainage capacity loss",
    RelationKind.MONITORS: "measurement loss",
    RelationKind.SHELTERS: "shelter capacity loss",
    RelationKind.RELEASES: "upstream release",
    RelationKind.FEEDS: "hydraulic propagation",
    RelationKind.DEPENDS_ON: "hard dependency",
    RelationKind.ROUTES_TO: "response coverage loss",
}


def build_relationships() -> list[dict[str, Any]]:
    """Explicit engineering edges + deterministic proximity-derived dependencies."""
    by_id = {a["id"]: a for a in ASSETS}
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def link(source: str, target: str, kind: RelationKind, **attrs: Any) -> None:
        if source not in by_id or target not in by_id or source == target:
            return
        key = (source, target, kind.value)
        if key in seen:
            return
        seen.add(key)
        edges.append(
            {
                "source_id": source,
                "target_id": target,
                "kind": kind,
                "criticality": float(attrs.pop("criticality", 0.5)),
                "lead_time_min": int(attrs.pop("lead_time_min", 0)),
                "attenuation": float(attrs.pop("attenuation", 1.0)),
                "failure_mode": attrs.pop("failure_mode", FailureMode.IMMEDIATE),
                "recovery_hours": float(attrs.pop("recovery_hours", 6.0)),
                "redundancy_group": attrs.pop("redundancy_group", None),
                "attributes": attrs.pop("attributes", {}),
            }
        )

    for src, dst, kind, attrs in EXPLICIT_EDGES:
        link(src, dst, kind, **dict(attrs))

    # 1. every lifeline facility is powered by the nearest substation(s)
    substations = [a for a in ASSETS if a["asset_type"] is AssetType.SUBSTATION]
    power_takers = {
        AssetType.HOSPITAL, AssetType.PUMP_STATION, AssetType.WATER_TREATMENT,
        AssetType.TELECOM_TOWER, AssetType.SHELTER, AssetType.METRO_STATION,
        AssetType.FIRE_STATION, AssetType.POLICE_STATION, AssetType.WASTE_PLANT,
        AssetType.REFINERY, AssetType.PORT, AssetType.AIRPORT,
    }
    lifelines = [a for a in ASSETS if a["asset_type"] in power_takers]
    for facility in lifelines:
        ranked = sorted(substations, key=lambda s: haversine_m(facility["lon"], facility["lat"], s["lon"], s["lat"]))
        # on-site autonomy decides *when* a power loss becomes a capability loss.
        # A hospital with 18 h of generator fuel does not fail because its feeder
        # tripped; it fails 10 h later, and the twin must say exactly that.
        autonomy_h = float(
            (facility.get("capacity") or {}).get("autonomy_hours")
            or (facility.get("capacity") or {}).get("generator_hours")
            or (facility.get("capacity") or {}).get("backup_hours")
            or 0.0
        )
        buffer_min = int(autonomy_h * 60 * 0.55)  # 55 % duty: reserve depletes under load
        for idx, sub in enumerate(ranked[:2]):
            dist = haversine_m(facility["lon"], facility["lat"], sub["lon"], sub["lat"])
            link(
                sub["id"], facility["id"], RelationKind.POWERS,
                criticality=round(0.9 - 0.25 * idx, 2),
                attenuation=(1.0 if idx == 0 else 0.6) * (0.7 if buffer_min else 1.0),
                lead_time_min=buffer_min if idx == 0 else 0,
                failure_mode=FailureMode.DELAYED if buffer_min else (FailureMode.DEGRADED if idx else FailureMode.IMMEDIATE),
                recovery_hours=round(4.0 + dist / 4000.0, 1),
                redundancy_group=f"power_{sub['id']}",
                attributes={
                    "distance_m": round(dist),
                    "preferred": idx == 0,
                    "autonomy_hours": autonomy_h,
                    "mechanism": "generator/UPS buffer" if buffer_min else "direct supply",
                },
            )

    # 2. facilities are accessed by the nearest 2 road/bridge segments
    corridors = [a for a in ASSETS if a["asset_type"] in {AssetType.ROAD, AssetType.BRIDGE}]
    for facility in [a for a in ASSETS if a["asset_type"] in {
        AssetType.HOSPITAL, AssetType.SHELTER, AssetType.FIRE_STATION, AssetType.POLICE_STATION,
        AssetType.METRO_STATION, AssetType.PUMP_STATION, AssetType.WATER_TREATMENT, AssetType.AIRPORT,
        AssetType.PORT, AssetType.REFINERY, AssetType.WASTE_PLANT, AssetType.CANAL, AssetType.RIVER_GAUGE,
        AssetType.SUBSTATION, AssetType.TELECOM_TOWER,
    }]:
        ranked = sorted(corridors, key=lambda r: haversine_m(facility["lon"], facility["lat"], r["lon"], r["lat"]))
        for idx, road in enumerate(ranked[:2]):
            dist = haversine_m(facility["lon"], facility["lat"], road["lon"], road["lat"])
            speed = float(road["capacity"].get("free_flow_speed_kmh", 35.0)) or 35.0
            link(
                road["id"], facility["id"], RelationKind.ACCESSES,
                criticality=round(0.85 - 0.3 * idx, 2),
                attenuation=1.0 if idx == 0 else 0.55,
                lead_time_min=int(round(dist / (speed * 1000 / 60), 0)),
                recovery_hours=2.0,
                redundancy_group=f"access_{facility['id']}",
                attributes={"distance_m": round(dist), "preferred": idx == 0},
            )

    # 3. telecom coverage: each tower covers facilities and response units in 3 km
    towers = [a for a in ASSETS if a["asset_type"] is AssetType.TELECOM_TOWER]
    for tower in towers:
        for facility in lifelines:
            if facility["id"] == tower["id"]:
                continue
            dist = haversine_m(tower["lon"], tower["lat"], facility["lon"], facility["lat"])
            if dist <= 3500:
                link(
                    tower["id"], facility["id"], RelationKind.COVERS,
                    criticality=round(max(0.25, 0.7 - dist / 6000.0), 2),
                    attenuation=round(max(0.2, 1.0 - dist / 4000.0), 2),
                    attributes={"distance_m": round(dist)},
                )

    # 4. water treatment distributes to the facilities it serves
    for source_id in ("a_wtp_aluva", "a_wtp_thevara"):
        source = by_id[source_id]
        for facility in [a for a in ASSETS if a["asset_type"] in {AssetType.HOSPITAL, AssetType.SHELTER}]:
            dist = haversine_m(source["lon"], source["lat"], facility["lon"], facility["lat"])
            if dist <= 9000:
                link(
                    source_id, facility["id"], RelationKind.SUPPLIES_WATER,
                    criticality=round(max(0.3, 0.85 - dist / 12000.0), 2),
                    lead_time_min=int(round(dist / 500.0)),
                    recovery_hours=10.0,
                    attributes={"distance_m": round(dist)},
                )

    # 5. street-level flood sensors monitor the nearest canal/road asset, so that a
    #    depth reading can be attributed to a hydraulic or mobility object
    water_and_roads = [
        a for a in ASSETS
        if a["asset_type"] in {AssetType.CANAL, AssetType.RIVER_GAUGE, AssetType.ROAD, AssetType.BRIDGE}
    ]
    for sensor_asset in [a for a in ASSETS if a["subtype"] == "street_flood_sensor"]:
        ranked = sorted(
            water_and_roads,
            key=lambda w: haversine_m(sensor_asset["lon"], sensor_asset["lat"], w["lon"], w["lat"]),
        )
        for idx, watched in enumerate(ranked[:2]):
            dist = haversine_m(sensor_asset["lon"], sensor_asset["lat"], watched["lon"], watched["lat"])
            link(
                sensor_asset["id"], watched["id"], RelationKind.MONITORS,
                criticality=round(0.7 - 0.25 * idx, 2),
                attenuation=0.85,
                attributes={"distance_m": round(dist)},
            )

    # 6. roads route responders from their stations to the facilities they serve
    stations = [a for a in ASSETS if a["asset_type"] in {AssetType.FIRE_STATION, AssetType.POLICE_STATION}]
    response_targets = [
        a for a in ASSETS
        if a["asset_type"] in {AssetType.HOSPITAL, AssetType.SHELTER, AssetType.PUMP_STATION}
    ]
    for station in stations:
        ranked = sorted(
            response_targets,
            key=lambda t: haversine_m(station["lon"], station["lat"], t["lon"], t["lat"]),
        )[:4]
        for target in ranked:
            dist = haversine_m(station["lon"], station["lat"], target["lon"], target["lat"])
            link(
                station["id"], target["id"], RelationKind.ROUTES_TO,
                criticality=round(max(0.3, 0.8 - dist / 12000.0), 2),
                lead_time_min=int(round(dist / 550.0)),
                attributes={"distance_m": round(dist)},
            )

    return edges


RELATIONSHIPS: list[dict[str, Any]] = build_relationships()


# --------------------------------------------------------------------------- #
# live seeded incidents (so a fresh boot shows a crisis in progress)
# --------------------------------------------------------------------------- #
def seed_incidents() -> list[dict[str, Any]]:
    return [
        {
            "id": "inc_seed_elamkulam",
            "title": "Elamkulam low-lying pocket: canal surcharge, 0.62 m road depth",
            "category": IncidentCategory.URBAN_FLOOD,
            "severity": 4,
            "status": IncidentStatus.ACTIVE,
            "confidence": 0.88,
            "summary": (
                "Thevara-Perandoor canal is at 2.71 m against a 2.80 m bankfull level while the backwater "
                "stage is 1.34 m and rising on a spring tide (1.42 m predicted at 21:20). Gravity outfalls are "
                "inoperative, Elamkulam pump station has lost its preferred feeder (Kaloor 220 kV under load "
                "shedding) and is on generator. Street depth sensors report 0.62 m at Elamkulam and 0.41 m at "
                "Thevara Ferry. Two ambulance corridors (Kaloor-Kadavanthra Road, Thevara Ferry Road) are "
                "degraded; 6,400 residents are in the affected pocket with 22 % over 60."
            ),
            "lon": 76.2962,
            "lat": 9.9710,
            "radius_m": 900.0,
            "zone": "z_elamkulam",
            "population_exposed": 6_400,
            "reported_by": "fusion-engine",
            "detected_at": NOW - timedelta(minutes=52),
            "affected": [
                ("a_fs_elamkulam", "impacted", 0.86),
                ("a_canal_thevara_perandoor", "hazard_source", 0.92),
                ("a_pump_elamkulam", "degraded", 0.74),
                ("a_road_kk", "impacted", 0.68),
                ("a_road_thevara_ferry", "impacted", 0.61),
                ("a_hosp_ig", "at_risk", 0.44),
            ],
            "timeline": [
                ("collect", "IMD 3-hour nowcast: 62 mm/h convective cell over Ernakulam", 52),
                ("fuse", "Rain gauge cluster agreement 0.91; canal level corroborated by 2 sensors", 48),
                ("understand", "Anomaly z=4.1 on Thevara canal; classified urban_flood severity 4", 44),
                ("predict", "Canal bankfull projected in 38 min if rainfall continues above 40 mm/h", 30),
                ("recommend", "Pre-position 2 pump sets + open Sacred Heart shelter annex", 12),
            ],
        },
        {
            "id": "inc_seed_kumbalam",
            "title": "Kumbalam causeway: backwater stage above 1.55 m, causeway vulnerable",
            "category": IncidentCategory.ROAD_INUNDATION,
            "severity": 3,
            "status": IncidentStatus.TRIAGED,
            "confidence": 0.74,
            "summary": (
                "Vembanad backwater at Kumbalam is 1.55 m against a 1.60 m warning level with the tide still "
                "rising. The Kumbalam-Aroor road is the only southern connectivity for the Kumbalam pump station "
                "and the Rajagiri College shelter. If it closes, shelter reachability from Maradu rises by "
                "approximately 23 minutes."
            ),
            "lon": 76.3060,
            "lat": 9.9070,
            "radius_m": 700.0,
            "zone": "z_kumbalam",
            "population_exposed": 3_100,
            "reported_by": "field-observation",
            "detected_at": NOW - timedelta(minutes=95),
            "affected": [
                ("a_backwater_kumbalam", "hazard_source", 0.8),
                ("a_road_kumbalam", "at_risk", 0.66),
                ("a_pump_kumbalam", "at_risk", 0.6),
                ("a_shl_rajarajeshwari", "at_risk", 0.5),
            ],
            "timeline": [
                ("collect", "Tide table + backwater gauge 1.55 m", 95),
                ("fuse", "Single-source corroboration; confidence capped at 0.74", 88),
                ("understand", "Triaged as road inundation risk severity 3", 80),
            ],
        },
    ]
