from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TORNADO_VISIBLE_MS = 30 * 60 * 1000
STORM_VISIBLE_MS = 5 * 60 * 1000
STORM_MIN_VISIBLE_GAME_MS = int(os.environ.get("CANVAS_STORM_MIN_VISIBLE_GAME_MS", "0"))
PROJECTION_MARGIN = 36
STORM_FRAME_MS = 50
MAX_STORM_POINTS_PER_FRAME = 100
DEBUG_STORM_FRACTION = 0.1
PLAYER_SPEED_LIMIT_MPH = 60
TORNADO_REWARD_RADIUS_MILES = 10
MILES_PER_LAT_DEGREE = 69.0
CONUS_LAT_MIN = 24.0
CONUS_LAT_MAX = 50.0
CONUS_LNG_MIN = -125.0
CONUS_LNG_MAX = -66.0
CONUS_MID_LAT = (CONUS_LAT_MIN + CONUS_LAT_MAX) / 2


def parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_iso_ms(value: str | None) -> int | None:
    if not value:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return int(parsed.timestamp() * 1000)


def _normalize_lng(lng: float) -> float:
    # Some CSV exports use positive west longitudes; normalize US-like values.
    if lng > 0:
        return -lng
    return lng


def _in_conus(lat: float, lng: float) -> bool:
    return (
        CONUS_LAT_MIN <= lat <= CONUS_LAT_MAX
        and CONUS_LNG_MIN <= lng <= CONUS_LNG_MAX
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _round_optional(value: Any, digits: int = 1) -> float | None:
    parsed = parse_float(value)
    return round(parsed, digits) if parsed is not None else None


def _lat_lng_from_coord(coord: list) -> tuple[float, float] | None:
    try:
        lon = float(coord[0])
        lat = float(coord[1])
    except (TypeError, ValueError, IndexError):
        return None
    if not math.isfinite(lat) or not math.isfinite(lon):
        return None
    lon = _normalize_lng(lon)
    return lat, lon


def _feature_from_json(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _load_tornadoes(path: Path) -> list[dict]:
    tornadoes = []
    for index, row in enumerate(_read_csv(path)):
        feature = _feature_from_json(row.get("feature_json"))
        if feature is None:
            lat = parse_float(row.get("lat"))
            lon = parse_float(row.get("lon"))
            if lat is None or lon is None:
                continue
            feature = {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "valid": row.get("valid"),
                    "mag": row.get("mag"),
                    "location": row.get("location"),
                    "county": row.get("county"),
                    "state": row.get("state"),
                    "remarks": row.get("remarks"),
                },
            }

        geom = feature.get("geometry") or {}
        props = feature.get("properties") or {}
        start_raw = props.get("valid")
        if not start_raw and props.get("date") and props.get("time"):
            start_raw = f"{props['date']}T{props['time']}Z"
        start_ms = parse_iso_ms(start_raw) or int(parse_float(row.get("valid_unix")) or 0) * 1000
        if not start_ms:
            continue

        point = None
        line = None
        coords = geom.get("coordinates")
        geom_type = geom.get("type")
        if geom_type == "Point" and coords:
            point = _lat_lng_from_coord(coords)
        elif geom_type == "LineString" and coords:
            line = [pt for pt in (_lat_lng_from_coord(coord) for coord in coords) if pt]
            point = line[0] if line else None
        elif geom_type == "MultiLineString" and coords:
            line = [
                pt
                for part in coords
                for pt in (_lat_lng_from_coord(coord) for coord in part)
                if pt
            ]
            point = line[0] if line else None

        if point is None or not _in_conus(point[0], point[1]):
            continue
        if line:
            line = [pt for pt in line if _in_conus(pt[0], pt[1])]

        mag = str(props.get("mag") or row.get("mag") or "UNK")
        if mag.upper().startswith("EF"):
            mag = mag[2:]
        tornadoes.append(
            {
                "id": f"target-{index + 1}",
                "start_ms": start_ms,
                "end_ms": start_ms + TORNADO_VISIBLE_MS,
                "mag": mag or "UNK",
                "point": point,
                "line": line,
                "location": props.get("location") or row.get("location") or "",
                "county": props.get("county") or row.get("county") or "",
                "state": props.get("state") or row.get("state") or "",
            }
        )
    return tornadoes


def _ring_to_lat_lng(ring: list) -> list[tuple[float, float]]:
    return [pt for pt in (_lat_lng_from_coord(coord) for coord in ring) if pt]


def _load_warnings(path: Path) -> list[dict]:
    warnings = []
    for index, row in enumerate(_read_csv(path)):
        feature = _feature_from_json(row.get("raw_feature_json"))
        if feature is None:
            geometry = _feature_from_json(row.get("geometry_json"))
            properties = _feature_from_json(row.get("properties_json")) or {}
            feature = {"type": "Feature", "geometry": geometry, "properties": properties}

        geom = feature.get("geometry") or {}
        props = feature.get("properties") or {}
        mapped = props.get("_map_py") or {}
        eff_ms = parse_iso_ms(mapped.get("effective") or props.get("effective"))
        exp_ms = parse_iso_ms(mapped.get("expires") or props.get("expires"))
        if eff_ms is None or exp_ms is None:
            continue

        polygons = []
        if geom.get("type") == "Polygon":
            coords = geom.get("coordinates") or []
            if coords:
                polygons.append(_ring_to_lat_lng(coords[0]))
        elif geom.get("type") == "MultiPolygon":
            for polygon in geom.get("coordinates") or []:
                if polygon:
                    polygons.append(_ring_to_lat_lng(polygon[0]))
        polygons = [polygon for polygon in polygons if len(polygon) >= 3]
        if not polygons:
            continue

        warnings.append(
            {
                "id": f"zone-{index + 1}",
                "event": mapped.get("event") or props.get("event") or "Unknown",
                "headline": str(mapped.get("headline") or props.get("headline") or "")[:120],
                "sender": mapped.get("sender") or props.get("senderName") or "",
                "eff_ms": eff_ms,
                "exp_ms": exp_ms,
                "polygons": polygons,
            }
        )
    return warnings


def _load_storms(path: Path) -> list[dict]:
    grouped: dict[str, list[list]] = {}
    meta: dict[str, tuple[str, str]] = {}
    for row in _read_csv(path):
        valid_unix = parse_float(row.get("valid_unix"))
        lat = parse_float(row.get("lat"))
        lon = parse_float(row.get("lon"))
        if valid_unix is None or lat is None or lon is None:
            continue
        lon = _normalize_lng(lon)
        if not _in_conus(lat, lon):
            continue
        track_id = row.get("track_id") or row.get("storm_id") or "storm"
        grouped.setdefault(track_id, []).append(
            [
                int(valid_unix * 1000),
                round(lat, 5),
                round(lon, 5),
                _round_optional(row.get("vil"), 2),
                _round_optional(row.get("max_dbz")),
                _round_optional(row.get("mesh"), 2),
                _round_optional(row.get("cell_speed")),
            ]
        )
        meta[track_id] = (row.get("radar") or "", row.get("storm_id") or "")

    storms = []
    for track_id, points in grouped.items():
        points.sort(key=lambda point: point[0])
        if not points:
            continue
        radar, storm_id = meta[track_id]
        storms.append(
            {
                "id": track_id,
                "radar": radar,
                "storm_id": storm_id,
                "start_ms": points[0][0],
                "end_ms": points[-1][0] + STORM_VISIBLE_MS,
                "points": points,
            }
        )
    return storms


def _load_spotter_initial_positions(path: Path) -> list[dict]:
    earliest_by_spotter = {}
    for index, row in enumerate(_read_csv(path)):
        lat = parse_float(row.get("lat"))
        lon = parse_float(row.get("lon"))
        if lat is None or lon is None:
            continue
        lon = _normalize_lng(lon)
        if not _in_conus(lat, lon):
            continue

        name = (row.get("name") or "").strip()
        spotter_id = (row.get("id") or name or f"spotter-{index + 1}").strip()
        key = name or spotter_id
        timestamp_ms = parse_iso_ms(row.get("timestamp"))
        if timestamp_ms is None:
            timestamp_ms = int((parse_float(row.get("unix_seconds")) or 0) * 1000)

        existing = earliest_by_spotter.get(key)
        candidate = {
            "id": spotter_id,
            "name": name or spotter_id,
            "raw_time_ms": timestamp_ms,
            "point": (round(lat, 5), round(lon, 5)),
            "source_index": index,
        }
        if existing is None or (timestamp_ms, index) < (
            existing["raw_time_ms"],
            existing["source_index"],
        ):
            earliest_by_spotter[key] = candidate

    return sorted(
        earliest_by_spotter.values(),
        key=lambda spotter: (spotter["raw_time_ms"], spotter["source_index"]),
    )


def _load_spotter_tracks(path: Path) -> list[dict]:
    grouped: dict[str, dict] = {}
    for index, row in enumerate(_read_csv(path)):
        lat = parse_float(row.get("lat"))
        lon = parse_float(row.get("lon"))
        if lat is None or lon is None:
            continue
        lon = _normalize_lng(lon)
        if not _in_conus(lat, lon):
            continue

        timestamp_ms = parse_iso_ms(row.get("timestamp"))
        if timestamp_ms is None:
            timestamp_ms = int((parse_float(row.get("unix_seconds")) or 0) * 1000)
        if timestamp_ms <= 0:
            continue

        name = (row.get("name") or "").strip()
        spotter_id = (row.get("id") or name or f"spotter-{index + 1}").strip()
        key = name or spotter_id
        track = grouped.setdefault(
            key,
            {
                "id": f"chaser-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:8]}",
                "name": name or spotter_id,
                "points": [],
            },
        )
        track["points"].append(
            [
                timestamp_ms,
                round(lat, 5),
                round(lon, 5),
                _round_optional(row.get("heading")),
            ]
        )

    tracks = []
    for track in grouped.values():
        points = sorted(track["points"], key=lambda point: point[0])
        deduped = []
        seen = set()
        for point in points:
            key = (point[0], point[1], point[2])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(point)
        if len(deduped) < 2:
            continue
        tracks.append({**track, "points": deduped})

    return sorted(tracks, key=lambda track: (-len(track["points"]), track["name"]))


class CanvasProjection:
    def __init__(self, canvas_size: int, storm_coords: list[tuple[float, float]]):
        self.canvas_size = canvas_size
        if storm_coords:
            lats = [coord[0] for coord in storm_coords]
            lngs = [coord[1] for coord in storm_coords]
            lat_min = min(lats)
            lat_max = max(lats)
            lng_min = min(lngs)
            lng_max = max(lngs)
        else:
            lat_min = CONUS_LAT_MIN
            lat_max = CONUS_LAT_MAX
            lng_min = CONUS_LNG_MIN
            lng_max = CONUS_LNG_MAX

        if lat_max - lat_min < 0.5:
            mid = (lat_min + lat_max) / 2
            lat_min = mid - 0.25
            lat_max = mid + 0.25
        if lng_max - lng_min < 0.5:
            mid = (lng_min + lng_max) / 2
            lng_min = mid - 0.25
            lng_max = mid + 0.25

        self.lat_min = max(CONUS_LAT_MIN, lat_min)
        self.lat_max = min(CONUS_LAT_MAX, lat_max)
        self.lng_min = max(CONUS_LNG_MIN, lng_min)
        self.lng_max = min(CONUS_LNG_MAX, lng_max)
        self.mid_lat = (self.lat_min + self.lat_max) / 2
        cos_lat = max(0.2, math.cos(math.radians(self.mid_lat)))
        self.min_x = self.lng_min * cos_lat
        self.max_x = self.lng_max * cos_lat
        self.min_y = self.lat_min
        self.max_y = self.lat_max
        width = max(self.max_x - self.min_x, 1e-9)
        height = max(self.max_y - self.min_y, 1e-9)
        drawable = max(1, canvas_size - 2 * PROJECTION_MARGIN)
        self.scale = min(drawable / width, drawable / height)
        self.offset_x = (canvas_size - width * self.scale) / 2
        self.offset_y = (canvas_size - height * self.scale) / 2

    def project(self, lat: float, lng: float) -> dict[str, float]:
        x_raw = lng * max(0.2, math.cos(math.radians(self.mid_lat)))
        x = self.offset_x + (x_raw - self.min_x) * self.scale
        y = self.offset_y + (self.max_y - lat) * self.scale
        return {"x": round(x, 2), "y": round(y, 2)}

    def metadata(self) -> dict:
        return {
            "mid_lat": round(self.mid_lat, 6),
            "scale": self.scale,
            "px_per_mile": self.px_per_mile(),
            "margin": PROJECTION_MARGIN,
            "lat_min": self.lat_min,
            "lat_max": self.lat_max,
            "lng_min": self.lng_min,
            "lng_max": self.lng_max,
        }

    def px_per_mile(self) -> float:
        return self.scale / MILES_PER_LAT_DEGREE


def _time_window(storms: list[dict], warnings: list[dict], tornadoes: list[dict]) -> tuple[int, int]:
    times = []
    for storm in storms:
        times.extend(point[0] for point in storm.get("points", []))
    for warning in warnings:
        times.extend([warning["eff_ms"], warning["exp_ms"]])
    for tornado in tornadoes:
        times.extend([tornado["start_ms"], tornado["end_ms"]])
    if not times:
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        return now, now + TORNADO_VISIBLE_MS
    start = min(times)
    end = max(times)
    if end <= start:
        end = start + TORNADO_VISIBLE_MS
    return start, end


def _game_ms(raw_ms: int, raw_min_ms: int, raw_max_ms: int, trial_seconds: int) -> int:
    duration = max(1, raw_max_ms - raw_min_ms)
    game_duration = trial_seconds * 1000
    return int(max(0, min(game_duration, round((raw_ms - raw_min_ms) / duration * game_duration))))


def _speed_limit_px_per_s(
    *,
    projection: CanvasProjection,
    raw_min_ms: int,
    raw_max_ms: int,
    trial_seconds: int,
) -> float:
    raw_seconds_per_game_second = (raw_max_ms - raw_min_ms) / max(1, trial_seconds * 1000)
    miles_per_raw_second = PLAYER_SPEED_LIMIT_MPH / 3600
    return round(
        miles_per_raw_second
        * raw_seconds_per_game_second
        * projection.px_per_mile(),
        3,
    )


def _thin_storm_points(storm_points: list[list]) -> list[list]:
    grouped: dict[int, list[list]] = {}
    for point in storm_points:
        frame_ms = int(point[0] // STORM_FRAME_MS * STORM_FRAME_MS)
        thinned = [frame_ms, point[1], point[2], point[3], point[4], point[5], point[6]]
        grouped.setdefault(frame_ms, []).append(thinned)

    output = []
    for frame_ms in sorted(grouped):
        frame_points = grouped[frame_ms]
        frame_points.sort(
            key=lambda point: (
                float(point[4]) if point[4] is not None else -1.0,
                float(point[5]) if point[5] is not None else -1.0,
            ),
            reverse=True,
        )
        output.extend(frame_points[:MAX_STORM_POINTS_PER_FRAME])
    return output


def _debug_storm_subset(storms: list[dict], *, label: str) -> list[dict]:
    fraction = max(0.0, min(1.0, DEBUG_STORM_FRACTION))
    if fraction >= 1.0 or not storms:
        return storms
    if fraction <= 0.0:
        return []

    keep_count = max(1, math.ceil(len(storms) * fraction))
    selected = sorted(
        storms,
        key=lambda storm: hashlib.sha256(
            f"{label}:{storm.get('id', '')}".encode("utf-8")
        ).hexdigest(),
    )[:keep_count]
    return sorted(selected, key=lambda storm: storm["start_ms"])


def build_world_from_session_dir(
    session_dir: str | os.PathLike[str],
    *,
    canvas_size: int,
    trial_seconds: int,
    coin_radius: int,
    coin_bonus: float,
    include_browser_layers: bool = True,
) -> dict:
    session_path = Path(session_dir)
    label = session_path.name
    storms = _load_storms(session_path / "storms.csv")
    warnings = _load_warnings(session_path / "warnings.csv")
    tornadoes = _load_tornadoes(session_path / "tornadoes.csv")
    spotter_initial_positions = _load_spotter_initial_positions(session_path / "spotters.csv")
    spotter_tracks = (
        _load_spotter_tracks(session_path / "spotters.csv")
        if include_browser_layers
        else []
    )
    raw_min_ms, raw_max_ms = _time_window(storms, warnings, tornadoes)
    storm_coords = [
        (point[1], point[2])
        for storm in storms
        for point in storm.get("points", [])
        if _in_conus(point[1], point[2])
    ]
    projection = CanvasProjection(canvas_size, storm_coords)

    def game(raw_ms: int) -> int:
        return _game_ms(raw_ms, raw_min_ms, raw_max_ms, trial_seconds)

    storm_points = []
    rendered_storms = _debug_storm_subset(storms, label=label) if include_browser_layers else []
    if include_browser_layers:
        for storm in rendered_storms:
            for raw_ms, lat, lng, vil, max_dbz, mesh, cell_speed in storm["points"]:
                xy = projection.project(lat, lng)
                game_ms = game(raw_ms)
                end_ms = max(
                    game(raw_ms + STORM_VISIBLE_MS),
                    game_ms + STORM_MIN_VISIBLE_GAME_MS,
                )
                storm_points.append(
                    [
                        game_ms,
                        min(trial_seconds * 1000, end_ms),
                        xy["x"],
                        xy["y"],
                        vil,
                        max_dbz,
                        raw_ms,
                    ]
                )
        storm_points = _thin_storm_points(storm_points)

    spawn_points = []
    for spotter in spotter_initial_positions:
        xy = projection.project(*spotter["point"])
        spawn_points.append(
            {
                "id": spotter["id"],
                "name": spotter["name"],
                "x": xy["x"],
                "y": xy["y"],
                "raw_time_ms": spotter["raw_time_ms"],
            }
        )

    chaser_tracks = []
    rendered_warnings = []
    if include_browser_layers:
        for track in spotter_tracks:
            points = []
            for raw_ms, lat, lng, heading in track["points"]:
                if raw_ms < raw_min_ms or raw_ms > raw_max_ms:
                    continue
                xy = projection.project(lat, lng)
                points.append(
                    [
                        game(raw_ms),
                        xy["x"],
                        xy["y"],
                        raw_ms,
                        heading,
                    ]
                )
            if len(points) < 2:
                continue
            chaser_tracks.append(
                {
                    "id": track["id"],
                    "name": track["name"],
                    "start_ms": points[0][0],
                    "end_ms": points[-1][0],
                    "points": points,
                }
            )

        for warning in warnings:
            rendered_warnings.append(
                {
                    "id": warning["id"],
                    "event": warning["event"],
                    "headline": warning["headline"],
                    "sender": warning["sender"],
                    "eff_ms": game(warning["eff_ms"]),
                    "exp_ms": game(warning["exp_ms"]),
                    "polygons": [
                        [
                            [
                                projection.project(lat, lng)["x"],
                                projection.project(lat, lng)["y"],
                            ]
                            for lat, lng in polygon
                        ]
                        for polygon in warning["polygons"]
                    ],
                }
            )

    reward_targets = []
    reward_events = []
    reward_radius_px = round(TORNADO_REWARD_RADIUS_MILES * projection.px_per_mile(), 3)
    for tornado in tornadoes:
        point = projection.project(*tornado["point"])
        line = [
            [projection.project(lat, lng)["x"], projection.project(lat, lng)["y"]]
            for lat, lng in (tornado.get("line") or [])
        ]
        start_ms = game(tornado["start_ms"])
        end_ms = game(tornado["end_ms"])
        reward_targets.append(
            {
                "id": tornado["id"],
                "start_ms": start_ms,
                "end_ms": end_ms,
                "x": point["x"],
                "y": point["y"],
                "line": line,
                "radius": reward_radius_px,
                "radius_miles": TORNADO_REWARD_RADIUS_MILES,
                "raw_start_ms": tornado["start_ms"],
                "raw_end_ms": tornado["end_ms"],
                "mag": tornado["mag"],
                "location": tornado["location"],
                "county": tornado["county"],
                "state": tornado["state"],
            }
        )
        if include_browser_layers:
            reward_events.append(
                {
                    "id": tornado["id"],
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "x": point["x"],
                    "y": point["y"],
                    "line": line,
                    "radius": reward_radius_px,
                    "radius_miles": TORNADO_REWARD_RADIUS_MILES,
                }
            )

    seed = int(hashlib.sha256(label.encode("utf-8")).hexdigest()[:8], 16)
    return {
        "world_id": f"session-{label}",
        "session_label": label,
        "seed": seed,
        "canvas_size": canvas_size,
        "player_radius": 12,
        "coin_radius": coin_radius,
        "coin_bonus": coin_bonus,
        "max_player_speed": _speed_limit_px_per_s(
            projection=projection,
            raw_min_ms=raw_min_ms,
            raw_max_ms=raw_max_ms,
            trial_seconds=trial_seconds,
        ),
        "speed_limit_mph": PLAYER_SPEED_LIMIT_MPH,
        "raw_time_min_ms": raw_min_ms,
        "raw_time_max_ms": raw_max_ms,
        "game_start_ms": 0,
        "game_end_ms": trial_seconds * 1000,
        "trial_seconds": trial_seconds,
        "projection": projection.metadata(),
        "debug_storm_fraction": DEBUG_STORM_FRACTION,
        "storm_min_visible_game_ms": STORM_MIN_VISIBLE_GAME_MS,
        "storm_count": len(storms),
        "rendered_storm_count": len(rendered_storms),
        "chaser_track_count": len(chaser_tracks),
        "storms": [],
        "storm_points": storm_points,
        "chaser_tracks": chaser_tracks,
        "warnings": rendered_warnings,
        "reward_targets": reward_targets,
        "reward_events": reward_events,
        "spawn_points": spawn_points,
    }


def _has_session_csvs(path: Path) -> bool:
    return (path / "storms.csv").exists() or (path / "warnings.csv").exists() or (path / "tornadoes.csv").exists()


def discover_session_dirs(static_root: str | os.PathLike[str]) -> list[Path]:
    root = Path(static_root)
    candidates = []
    streamlined_root = root / "streamlined_sessions"
    if streamlined_root.is_dir():
        candidates.extend(path for path in sorted(streamlined_root.iterdir()) if path.is_dir())
    if root.is_dir():
        candidates.extend(path for path in sorted(root.iterdir()) if path.is_dir())
    seen = set()
    session_dirs = []
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not _has_session_csvs(path):
            continue
        seen.add(resolved)
        session_dirs.append(path)
    return session_dirs


def load_world_definitions(
    *,
    static_root: str | os.PathLike[str],
    canvas_size: int,
    trial_seconds: int,
    coin_radius: int,
    coin_bonus: float,
    include_browser_layers: bool = True,
) -> list[dict]:
    session_dirs = discover_session_dirs(static_root)
    if not session_dirs:
        raise RuntimeError(
            "No streamlined session CSV folders found. Expected folders such "
            f"as {Path(static_root) / 'streamlined_sessions' / '<session_label>'} "
            "containing storms.csv, warnings.csv, and tornadoes.csv."
        )
    return [
        build_world_from_session_dir(
            session_dir,
            canvas_size=canvas_size,
            trial_seconds=trial_seconds,
            coin_radius=coin_radius,
            coin_bonus=coin_bonus,
            include_browser_layers=include_browser_layers,
        )
        for session_dir in session_dirs
    ]


NODE_DEFINITION_KEYS = (
    "world_id",
    "session_label",
    "seed",
    "canvas_size",
    "player_radius",
    "coin_radius",
    "coin_bonus",
    "max_player_speed",
    "speed_limit_mph",
    "raw_time_min_ms",
    "raw_time_max_ms",
    "game_start_ms",
    "game_end_ms",
    "trial_seconds",
    "projection",
    "spawn_points",
    "reward_targets",
)

BROWSER_WORLD_KEYS = (
    "storm_points",
    "chaser_tracks",
    "warnings",
    "reward_events",
)


def node_definition_from_world(world: dict) -> dict:
    return {key: world[key] for key in NODE_DEFINITION_KEYS}


def browser_world_payload(world: dict) -> dict:
    return {key: world.get(key, []) for key in BROWSER_WORLD_KEYS}


def session_dir_for_label(
    static_root: str | os.PathLike[str],
    session_label: str,
) -> Path:
    for session_dir in discover_session_dirs(static_root):
        if session_dir.name == session_label:
            return session_dir
    raise RuntimeError(
        f"No streamlined session folder named {session_label!r} under {static_root}."
    )


def write_world_json(
    path,
    session_label,
    canvas_size,
    trial_seconds,
    coin_radius,
    coin_bonus,
):
    """Write browser-only map arrays for a PsyNet cached function asset."""
    static_root = Path(__file__).resolve().parent / "static"
    world = build_world_from_session_dir(
        session_dir_for_label(static_root, session_label),
        canvas_size=canvas_size,
        trial_seconds=trial_seconds,
        coin_radius=coin_radius,
        coin_bonus=coin_bonus,
        include_browser_layers=True,
    )
    Path(path).write_text(
        json.dumps(browser_world_payload(world)),
        encoding="utf-8",
    )


def public_world_payload(world: dict) -> dict:
    return {
        "world_id": world["world_id"],
        "session_label": world.get("session_label"),
        "canvas_size": world["canvas_size"],
        "player_radius": world["player_radius"],
        "coin_radius": world["coin_radius"],
        "coin_bonus": world["coin_bonus"],
        "max_player_speed": world["max_player_speed"],
        "speed_limit_mph": world.get("speed_limit_mph", PLAYER_SPEED_LIMIT_MPH),
        "raw_time_min_ms": world["raw_time_min_ms"],
        "raw_time_max_ms": world["raw_time_max_ms"],
        "game_start_ms": world["game_start_ms"],
        "game_end_ms": world["game_end_ms"],
    }
