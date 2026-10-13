"""Data fusion: normalization, identity resolution and quality scoring.

CUSTOS sits *above* existing systems, so the first hard problem is that a gauge
from the water authority, a tide table from the port, a field report from a
ward officer and a road closure from a traffic API all describe the same city in
different units, epochs, coordinate conventions and names. Everything downstream
(cascade reasoning, what-if simulation, recommendations) is only as trustworthy
as this module.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from ..observability import get_logger

log = get_logger("fusion.normalize")

# --------------------------------------------------------------------------- #
# units
# --------------------------------------------------------------------------- #

_METRIC_UNITS: dict[str, tuple[str, float]] = {
    # canonical target -> (target unit, multiplicative factor from source unit)
    "rainfall_mm_h": ("mm/h", 1.0),
    "rain_mm_h": ("mm/h", 1.0),
    "precipitation_mm_h": ("mm/h", 1.0),
    "water_level_m": ("m", 1.0),
    "river_level_m": ("m", 1.0),
    "depth_m": ("m", 1.0),
    "flood_depth_m": ("m", 1.0),
    "tide_m": ("m", 1.0),
    "discharge_m3s": ("m3/s", 1.0),
    "flow_m3s": ("m3/s", 1.0),
    "temperature_c": ("degC", 1.0),
    "humidity_pct": ("%", 1.0),
    "pressure_hpa": ("hPa", 1.0),
    "wind_speed_kmh": ("km/h", 1.0),
    "wind_gust_kmh": ("km/h", 1.0),
    "visibility_km": ("km", 1.0),
    "traffic_speed_kmh": ("km/h", 1.0),
    "power_kw": ("kW", 1.0),
    "voltage_kv": ("kV", 1.0),
    "population_count": ("people", 1.0),
    "battery_pct": ("%", 1.0),
    "pump_rate_m3s": ("m3/s", 1.0),
    "siltation_pct": ("%", 1.0),
}

_UNIT_ALIASES: dict[str, tuple[str, float]] = {
    # length
    "mm": ("m", 0.001),
    "millimetre": ("m", 0.001),
    "millimeter": ("m", 0.001),
    "cm": ("m", 0.01),
    "m": ("m", 1.0),
    "metre": ("m", 1.0),
    "meter": ("m", 1.0),
    "ft": ("m", 0.3048),
    "feet": ("m", 0.3048),
    "foot": ("m", 0.3048),
    "in": ("m", 0.0254),
    "inch": ("m", 0.0254),
    # rainfall intensity
    "mm/h": ("mm/h", 1.0),
    "mmhr": ("mm/h", 1.0),
    "mm_per_hour": ("mm/h", 1.0),
    "in/h": ("mm/h", 25.4),
    "mm/day": ("mm/h", 1 / 24),
    "mm/24h": ("mm/h", 1 / 24),
    # discharge
    "m3/s": ("m3/s", 1.0),
    "cumec": ("m3/s", 1.0),
    "cumecs": ("m3/s", 1.0),
    "m3s": ("m3/s", 1.0),
    "l/s": ("m3/s", 0.001),
    "cfs": ("m3/s", 0.0283168),
    # misc
    "degc": ("degC", 1.0),
    "c": ("degC", 1.0),
    "celsius": ("degC", 1.0),
    "%": ("%", 1.0),
    "pct": ("%", 1.0),
    "percent": ("%", 1.0),
    "hpa": ("hPa", 1.0),
    "mbar": ("hPa", 1.0),
    "kmh": ("km/h", 1.0),
    "km/h": ("km/h", 1.0),
    "mph": ("km/h", 1.60934),
    "knots": ("km/h", 1.852),
    "m/s": ("km/h", 3.6),
    "kw": ("kW", 1.0),
    "mw": ("kW", 1000.0),
    "kv": ("kV", 1.0),
    "v": ("kV", 0.001),
}


class UnitConversionError(ValueError):
    pass


def canonical_metric(metric: str) -> str:
    """Fold provider dialects onto one metric namespace."""
    m = (metric or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "rain": "rainfall_mm_h",
        "rainfall": "rainfall_mm_h",
        "rain_rate": "rainfall_mm_h",
        "precipitation": "rainfall_mm_h",
        "rain_intensity": "rainfall_mm_h",
        "rl": "river_level_m",
        "river_stage": "river_level_m",
        "gauge_level": "river_level_m",
        "waterlevel": "river_level_m",
        "water_level": "water_level_m",
        "flooddepth": "flood_depth_m",
        "inundation_depth": "flood_depth_m",
        "td": "tide_m",
        "tidal_height": "tide_m",
        "outflow": "discharge_m3s",
        "inflow": "discharge_m3s",
        "flow_rate": "discharge_m3s",
        "pump_flow": "pump_rate_m3s",
        "air_temp": "temperature_c",
        "temp": "temperature_c",
        "rh": "humidity_pct",
        "wind": "wind_speed_kmh",
        "gust": "wind_gust_kmh",
        "rainfall_total": "rainfall_mm_h",
    }
    m = aliases.get(m, m)
    if m not in _METRIC_UNITS:
        # unknown metrics are accepted but flagged; refusing data during a flood
        # is a worse failure than carrying a metric downstream with low confidence
        log.debug("unmapped metric retained as-is", extra={"custos": {"metric": metric}})
    return m


def convert_value(metric: str, value: float, unit: str | None) -> tuple[float, str, float]:
    """Return ``(value, unit, confidence_penalty)`` in the canonical unit."""
    target_unit, factor = _METRIC_UNITS.get(canonical_metric(metric), ("", 1.0))
    if not unit:
        return float(value), target_unit, 0.05
    key = unit.strip().lower()
    if key in _UNIT_ALIASES:
        canonical_unit, unit_factor = _UNIT_ALIASES[key]
        if target_unit and canonical_unit == target_unit:
            return float(value) * unit_factor * factor, target_unit, 0.0
        if target_unit:
            # the declared unit is dimensionally odd for this metric: convert what
            # we can, penalise confidence, and let the anomaly detector decide.
            return float(value) * unit_factor, target_unit, 0.2
        return float(value) * unit_factor, canonical_unit, 0.05
    return float(value), target_unit or unit, 0.15


def mm_per_hour_from_total(total_mm: float, window_minutes: float) -> float:
    if window_minutes <= 0:
        raise UnitConversionError("window_minutes must be positive")
    return total_mm * (60.0 / window_minutes)


# --------------------------------------------------------------------------- #
# time
# --------------------------------------------------------------------------- #

CITY_UTC_OFFSET_HOURS = 5.5  # Asia/Kolkata; overridden by config-driven helper


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(raw: Any, *, assume_tz_offset_hours: float = CITY_UTC_OFFSET_HOURS) -> datetime | None:
    """Normalize anything a provider might send into an aware UTC datetime.

    Handles: aware ISO strings, naive ISO strings (assumed *local* to the city,
    because that is what a field gateway writes), epoch seconds, epoch millis,
    and ``datetime`` objects. A naive datetime is *never* silently treated as
    UTC - doing so shifts an entire incident timeline by 5.5 hours in Kochi.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e11:  # milliseconds
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        text = str(raw).strip()
        if not text:
            return None
        if re.fullmatch(r"\d{10}(\.\d+)?", text):
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        if re.fullmatch(r"\d{13}", text):
            return datetime.fromtimestamp(float(text) / 1000.0, tz=timezone.utc)
        candidate = text.replace("Z", "+00:00").replace("z", "+00:00")
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%Y%m%d%H%M"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                log.warning("unparseable timestamp", extra={"custos": {"raw": text[:64]}})
                return None
    if dt.tzinfo is None:
        # a naive timestamp from a field gateway is *local* wall-clock time
        dt = dt.replace(tzinfo=timezone(timedelta(hours=assume_tz_offset_hours)))
    return dt.astimezone(timezone.utc)


def is_plausible_timestamp(dt: datetime, *, max_skew_hours: float = 24.0, max_age_days: float = 14.0) -> bool:
    t = now_utc()
    delta = (t - dt).total_seconds()
    return -(max_skew_hours * 3600) <= delta <= max_age_days * 86400


# --------------------------------------------------------------------------- #
# coordinates
# --------------------------------------------------------------------------- #

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(a_lon: float, a_lat: float, b_lon: float, b_lat: float) -> float:
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dphi = p2 - p1
    dlambda = math.radians(b_lon - a_lon)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def local_metres(lon: float, lat: float, origin_lon: float, origin_lat: float) -> tuple[float, float]:
    """Equirectangular projection - accurate to ~0.1 % over a city, and cheap."""
    x = math.radians(lon - origin_lon) * math.cos(math.radians(origin_lat)) * EARTH_RADIUS_M
    y = math.radians(lat - origin_lat) * EARTH_RADIUS_M
    return x, y


def bbox_contains(bbox: tuple[float, float, float, float], lon: float, lat: float) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat


def normalize_coordinates(raw: Mapping[str, Any], bbox: tuple[float, float, float, float]) -> tuple[float, float] | None:
    """Accept the many ways providers express a point; reject off-city junk.

    Supported: ``lon``/``lat``, ``lng``/``lat``, ``longitude``/``latitude``,
    ``x``/``y``, GeoJSON ``coordinates``, and flipped ``lat``/``lon`` pairs
    (detected by testing both against the city bounding box).
    """
    lon = raw.get("lon", raw.get("lng", raw.get("longitude", raw.get("x"))))
    lat = raw.get("lat", raw.get("latitude", raw.get("y")))
    if (lon is None or lat is None) and "coordinates" in raw:
        coords = raw["coordinates"]
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            lon, lat = coords[0], coords[1]
    if lon is None or lat is None:
        return None
    try:
        lon_f, lat_f = float(lon), float(lat)
    except (TypeError, ValueError):
        return None
    if bbox_contains(bbox, lon_f, lat_f):
        return lon_f, lat_f
    if bbox_contains(bbox, lat_f, lon_f):  # provider swapped the axes
        return lat_f, lon_f
    return None


# --------------------------------------------------------------------------- #
# identity resolution
# --------------------------------------------------------------------------- #

_SUFFIXES = (
    " road", " rd", " street", " st", " junction", " jn", " bridge", " canal",
    " hospital", " hpl", " station", " stn", " substation", " ss", " shelter",
)
_ABBREV = {
    "rd": "road",
    "st": "street",
    "jn": "junction",
    "jct": "junction",
    "hosp": "hospital",
    "hpl": "hospital",
    "substn": "substation",
    "ss": "substation",
    "stn": "station",
    "govt": "government",
    "dist": "district",
    "muncpl": "municipal",
    "co": "company",
    "nr": "near",
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def canonical_name(name: str) -> str:
    """Deterministic alias folding used for identity resolution."""
    n = unicodedata.normalize("NFKD", (name or "").lower())
    n = n.replace("&", " and ")
    tokens = [t for t in _NON_ALNUM.split(n) if t]
    tokens = [_ABBREV.get(t, t) for t in tokens]
    return " ".join(tokens).strip()


def name_similarity(a: str, b: str) -> float:
    """Token-set Jaccard with a suffix-tolerant bonus. Cheap, explainable, no model."""
    ta, tb = set(canonical_name(a).split()), set(canonical_name(b).split())
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    if canonical_name(a) in canonical_name(b) or canonical_name(b) in canonical_name(a):
        jaccard = max(jaccard, 0.82)
    return round(jaccard, 4)


def resolve_identity(
    candidate: Mapping[str, Any],
    existing: Iterable[Mapping[str, Any]],
    *,
    distance_weight_m: float = 250.0,
    accept_threshold: float = 0.72,
) -> tuple[Mapping[str, Any] | None, float, str]:
    """Match an incoming record to a canonical twin object.

    Score = 0.65 * name similarity + 0.35 * proximity, with a type-consistency
    gate. Returns ``(match, score, explanation)``. When no match clears the
    threshold the record is treated as a *new* object - CUSTOS prefers a
    duplicate over a wrong merge, because a wrong merge silently destroys
    situational awareness while a duplicate is visible and correctable.
    """
    best: Mapping[str, Any] | None = None
    best_score = 0.0
    best_reason = "no candidate cleared threshold"
    for row in existing:
        if str(row.get("asset_type")) != str(candidate.get("asset_type")):
            continue
        sim = name_similarity(str(candidate.get("name", "")), str(row.get("name", "")))
        dist = haversine_m(
            float(candidate.get("lon", 0.0)), float(candidate.get("lat", 0.0)),
            float(row.get("lon", 0.0)), float(row.get("lat", 0.0)),
        )
        proximity = max(0.0, 1.0 - dist / distance_weight_m)
        score = 0.65 * sim + 0.35 * proximity
        if score > best_score:
            best, best_score, best_reason = row, score, f"name_sim={sim:.2f} proximity={proximity:.2f} dist={dist:.0f}m"
    if best is not None and best_score >= accept_threshold:
        return best, round(best_score, 4), best_reason
    return None, round(best_score, 4), best_reason


def sensor_asset_binding(
    sensor: Mapping[str, Any],
    assets: Iterable[Mapping[str, Any]],
    *,
    type_preference: tuple[str, ...] = (),
    max_distance_m: float = 400.0,
) -> tuple[str | None, float, str]:
    """Bind a bare sensor to the twin object it measures (e.g. gauge -> canal)."""
    best_id: str | None = None
    best_score = 0.0
    best_reason = "unbound"
    for asset in assets:
        if type_preference and str(asset.get("asset_type")) not in type_preference:
            continue
        dist = haversine_m(
            float(sensor.get("lon", 0.0)), float(sensor.get("lat", 0.0)),
            float(asset.get("lon", 0.0)), float(asset.get("lat", 0.0)),
        )
        sim = name_similarity(str(sensor.get("name", "")), str(asset.get("name", "")))
        proximity = max(0.0, 1.0 - dist / max_distance_m)
        score = 0.4 * sim + 0.6 * proximity
        if score > best_score:
            best_id, best_score, best_reason = str(asset.get("id")), score, f"dist={dist:.0f}m name_sim={sim:.2f}"
    if best_score >= 0.55:
        return best_id, round(best_score, 4), best_reason
    return None, round(best_score, 4), "no asset within binding distance"


def identity_fingerprint(parts: Iterable[Any]) -> str:
    payload = "|".join(str(p) for p in parts if p is not None)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# quality / dedupe
# --------------------------------------------------------------------------- #

def dedupe_key(sensor_id: str, metric: str, observed_at: datetime, value: float, *, quantum: float = 1e-4) -> str:
    """Idempotency key: replaying a store-and-forward batch inserts nothing new.

    ``value`` is quantised so that float noise on a retransmit cannot create a
    duplicate row, while a genuinely different reading still gets its own key.
    """
    q = round(value / quantum) * quantum if quantum else value
    raw = f"{sensor_id}|{canonical_metric(metric)}|{observed_at.astimezone(timezone.utc).isoformat()}|{q:.6f}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def quality_score(
    *,
    timestamp_ok: bool,
    unit_known: bool,
    in_range: bool,
    is_duplicate: bool,
    provider_quality: float = 1.0,
    staleness_s: float | None = None,
) -> float:
    """A single 0..1 score combining every check the pipeline performed.

    The score travels with the reading all the way to the command UI, where a
    low-quality value is rendered differently rather than silently averaged in.
    """
    score = 1.0
    if not timestamp_ok:
        score -= 0.35
    if not unit_known:
        score -= 0.15
    if not in_range:
        score -= 0.25
    if is_duplicate:
        score -= 0.05
    if staleness_s is not None:
        if staleness_s > 900:
            score -= 0.2
        elif staleness_s > 120:
            score -= 0.05
    return max(0.0, min(1.0, score * max(0.0, min(1.0, provider_quality))))


def robust_baseline(values: Iterable[float]) -> tuple[float, float]:
    """Median + MAD. Used instead of mean/stddev because floods produce outliers
    that would inflate a standard deviation and mask the very anomaly we hunt."""
    data = sorted(float(v) for v in values if v is not None)
    if not data:
        return 0.0, 0.0
    n = len(data)
    median = data[n // 2] if n % 2 else 0.5 * (data[n // 2 - 1] + data[n // 2])
    deviations = sorted(abs(v - median) for v in data)
    mad = deviations[n // 2] if n % 2 else 0.5 * (deviations[n // 2 - 1] + deviations[n // 2])
    # 1.4826 scales MAD to be a consistent estimator of sigma for normal data
    return median, 1.4826 * mad


def z_score(value: float, median: float, mad_sigma: float) -> float:
    if mad_sigma <= 1e-9:
        return 0.0 if abs(value - median) < 1e-9 else math.copysign(8.0, value - median)
    return max(-8.0, min(8.0, (value - median) / mad_sigma))
