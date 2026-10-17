#!/usr/bin/env python3
"""Create side-by-side real-data to experiment-interface visualizations.

The script creates four snapshots per streamlined session. Each snapshot uses:

- the original Leaflet session HTML from the tornado repository on the left;
- the current airdrop game template rendered in a browser on the right;
- a composed PNG with the experimental screenshot inside a simple monitor frame.

Run from the repository root:

    python static/visualization.py

The script requires Playwright for browser screenshots:

    python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import ast
import base64
import html
import json
import math
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape


SCRIPT_PATH = Path(__file__).resolve()
STATIC_ROOT = SCRIPT_PATH.parent
PROJECT_ROOT = STATIC_ROOT.parent
TORNADO_SOURCE_ROOT = Path.home() / "Documents" / "tornado" / "output" / "peaks"
DEFAULT_SESSION_ROOT = STATIC_ROOT / "streamlined_sessions"
DEFAULT_SOURCE_HTML_ROOT = TORNADO_SOURCE_ROOT / "sessions"
DEFAULT_OUTPUT_DIR = STATIC_ROOT / "visualizations"
SNAPSHOT_COUNT = 4
VISUALIZATION_STORM_PERSISTENCE_MINUTES = 10
VISUALIZATION_VIEWPORT_MILES = 750
DEFAULT_VISUALIZATION_ASPECT_RATIO = "4:3"
DEFAULT_VISUALIZATION_ASPECT_RATIO_VALUE = 4 / 3
DEFAULT_OUTPUT_SCALE = 4
COMPOSITE_ARROW_WIDTH = 68
COMPOSITE_ARROW_LINE_WIDTH = 48
PLAYER_COLORS = [
    "#1f77b4",
    "#d62728",
    "#2ca02c",
    "#9467bd",
    "#ff7f0e",
    "#17becf",
]


sys.path.insert(0, str(PROJECT_ROOT))
from session_worlds import (  # noqa: E402
    _load_storms,
    _load_tornadoes,
    _load_warnings,
    build_world_from_session_dir,
)


class ConfigError(RuntimeError):
    """Raised when the visualization inputs are incomplete."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create real-map to airdrop-game visualization snapshots."
    )
    parser.add_argument(
        "--session-root",
        type=Path,
        default=DEFAULT_SESSION_ROOT,
        help="Folder containing streamlined session CSV subfolders.",
    )
    parser.add_argument(
        "--source-html-root",
        type=Path,
        default=DEFAULT_SOURCE_HTML_ROOT,
        help="Folder containing original Leaflet session.html files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output folder for final PNGs and generated temporary HTML.",
    )
    parser.add_argument(
        "--snapshots",
        type=int,
        default=SNAPSHOT_COUNT,
        help="Number of snapshots per session.",
    )
    parser.add_argument(
        "--session-limit",
        type=int,
        default=None,
        help="Optional limit for quick test runs.",
    )
    parser.add_argument(
        "--aspect-ratio",
        default=DEFAULT_VISUALIZATION_ASPECT_RATIO,
        help="Aspect ratio for both visualization panels, e.g. 4:3 or 16:9.",
    )
    parser.add_argument(
        "--output-scale",
        type=float,
        default=DEFAULT_OUTPUT_SCALE,
        help="Screenshot device scale factor. Increase for higher-resolution PNGs.",
    )
    parser.add_argument(
        "--overview-position",
        choices=("top-right", "top-left"),
        default="top-right",
        help="Position of the participant overview map in generated screenshots.",
    )
    screen_options = parser.add_mutually_exclusive_group()
    screen_options.add_argument(
        "--no-screen",
        dest="no_screen",
        action="store_true",
        default=True,
        help="Render without the monitor frame and add arrow keys (default).",
    )
    screen_options.add_argument(
        "--with-screen",
        dest="no_screen",
        action="store_false",
        help="Wrap the experiment UI in the legacy monitor frame.",
    )
    parser.add_argument(
        "--reuse-intermediate",
        action="store_true",
        help="Recompose existing real-map and experiment-UI PNGs without rerendering.",
    )
    parser.add_argument(
        "--reuse-original-map",
        action="store_true",
        help="Reuse existing real-map PNGs while rerendering the experiment UI.",
    )
    parser.add_argument(
        "--keep-html",
        action="store_true",
        help="Keep generated intermediate HTML files.",
    )
    return parser.parse_args()


def parse_aspect_ratio(value: str) -> float:
    if ":" in value:
        width, height = value.split(":", 1)
        return float(width) / float(height)
    return float(value)


def aspect_ratio_css(value: str) -> str:
    if ":" in value:
        width, height = value.split(":", 1)
        return f"{float(width):g} / {float(height):g}"
    return f"{float(value):g}"


def eval_constant_expr(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -eval_constant_expr(node.operand)
    if isinstance(node, ast.BinOp):
        left = eval_constant_expr(node.left)
        right = eval_constant_expr(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
    raise ValueError(f"Unsupported constant expression: {ast.dump(node)}")


def load_experiment_constants() -> dict[str, Any]:
    wanted = {
        "CANVAS_SIZE",
        "CANVAS_RENDER_WIDTH",
        "CANVAS_RENDER_HEIGHT",
        "TRIAL_SECONDS",
        "PLAYER_RADIUS",
        "COIN_RADIUS",
        "COIN_BONUS",
        "SEND_INTERVAL_MS",
        "DRAW_INTERVAL_MS",
        "STORM_CHASER_TRACK_COUNT",
        "CHASER_TRACK_VISIBLE_MS",
    }
    source = (PROJECT_ROOT / "experiment.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    constants: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in wanted:
            continue
        try:
            constants[target.id] = eval_constant_expr(node.value)
        except ValueError:
            continue
    missing = sorted(wanted - constants.keys())
    if missing:
        raise ConfigError(f"Could not read constants from experiment.py: {missing}")
    return constants


def discover_session_dirs(session_root: Path, session_limit: int | None) -> list[Path]:
    if not session_root.is_dir():
        raise ConfigError(f"Session root does not exist: {session_root}")
    session_dirs = [
        path
        for path in sorted(session_root.iterdir())
        if path.is_dir() and (path / "storms.csv").exists()
    ]
    if session_limit is not None:
        session_dirs = session_dirs[: max(0, session_limit)]
    if not session_dirs:
        raise ConfigError(f"No session folders found in {session_root}")
    return session_dirs


def initial_players(participant_ids: list[int], world: dict) -> dict:
    spawn_points = world.get("spawn_points", [])
    players = {}
    for index, participant_id in enumerate(participant_ids):
        if spawn_points:
            spawn = spawn_points[index % len(spawn_points)]
            x = float(spawn["x"])
            y = float(spawn["y"])
        else:
            x = world["canvas_size"] / 2
            y = world["canvas_size"] / 2
        players[str(participant_id)] = {
            "participant_id": str(participant_id),
            "label": f"Player {index + 1}",
            "color": PLAYER_COLORS[index % len(PLAYER_COLORS)],
            "x": round(x, 2),
            "y": round(y, 2),
            "vx": 0,
            "vy": 0,
            "client_time": 0,
            "receive_time": None,
        }
    return players


def visualization_active_player_position(
    world: dict, focus_xy: dict[str, float] | None, aspect_ratio: float
) -> dict[str, float] | None:
    if focus_xy is None:
        return None
    projection = world.get("projection", {})
    px_per_mile = float(projection.get("px_per_mile", 0))
    if px_per_mile <= 0:
        return None

    width_px = VISUALIZATION_VIEWPORT_MILES * px_per_mile
    height_px = width_px / aspect_ratio
    canvas_size = float(world["canvas_size"])
    focus_x = float(focus_xy["x"])
    focus_y = float(focus_xy["y"])
    camera_x = max(0, min(canvas_size - width_px, focus_x - width_px / 2))
    camera_y = max(0, min(canvas_size - height_px, focus_y - height_px / 2))

    # Keep the synthetic active player in the screenshot without covering the main target.
    desired_x = focus_x - width_px * 0.25
    desired_y = focus_y + height_px * 0.10
    min_x = camera_x + width_px * 0.15
    max_x = camera_x + width_px * 0.85
    min_y = camera_y + height_px * 0.15
    max_y = camera_y + height_px * 0.85
    return {
        "x": round(max(min_x, min(max_x, desired_x)), 2),
        "y": round(max(min_y, min(max_y, desired_y)), 2),
    }


def visualization_other_player_position(
    world: dict,
    focus_xy: dict[str, float] | None,
    active_player_position: dict[str, float] | None,
    aspect_ratio: float,
    overview_position: str,
) -> dict[str, float] | None:
    if focus_xy is None or active_player_position is None:
        return None
    projection = world.get("projection", {})
    px_per_mile = float(projection.get("px_per_mile", 0))
    if px_per_mile <= 0:
        return None

    width_px = VISUALIZATION_VIEWPORT_MILES * px_per_mile
    height_px = width_px / aspect_ratio
    canvas_size = float(world["canvas_size"])
    focus_x = float(focus_xy["x"])
    focus_y = float(focus_xy["y"])
    camera_x = max(0, min(canvas_size - width_px, focus_x - width_px / 2))
    camera_y = max(0, min(canvas_size - height_px, focus_y - height_px / 2))
    active_x = float(active_player_position["x"])
    active_y = float(active_player_position["y"])
    heading_x = focus_x - active_x
    heading_y = focus_y - active_y
    heading_length = math.hypot(heading_x, heading_y)
    if heading_length <= 0:
        return None
    forward_x = heading_x / heading_length
    forward_y = heading_y / heading_length
    right_x = -forward_y
    right_y = forward_x
    lateral_sign = 1 if overview_position == "top-left" else -1
    forward_distance = min(width_px * 0.04, 8)
    lateral_distance = min(width_px * 0.025, 4) * lateral_sign
    desired_x = (
        active_x
        + forward_x * forward_distance
        + right_x * lateral_distance
    )
    desired_y = (
        active_y
        + forward_y * forward_distance
        + right_y * lateral_distance
    )
    return {
        "x": round(
            max(camera_x + width_px * 0.15, min(camera_x + width_px * 0.85, desired_x)),
            2,
        ),
        "y": round(
            max(
                camera_y + height_px * 0.15,
                min(camera_y + height_px * 0.85, desired_y),
            ),
            2,
        ),
    }


def storm_persistence_game_ms(world: dict) -> int:
    raw_span = max(1, int(world["raw_time_max_ms"]) - int(world["raw_time_min_ms"]))
    game_span = max(1, int(world["game_end_ms"]) - int(world["game_start_ms"]))
    persistence_raw_ms = VISUALIZATION_STORM_PERSISTENCE_MINUTES * 60 * 1000
    return max(1, round(persistence_raw_ms * game_span / raw_span))


def extend_storm_persistence_for_visualization(world: dict) -> dict:
    persistence_ms = storm_persistence_game_ms(world)
    game_end_ms = int(world["game_end_ms"])
    storm_points = []
    for point in world.get("storm_points", []):
        updated = list(point)
        updated[1] = min(
            game_end_ms, max(int(updated[1]), int(updated[0]) + persistence_ms)
        )
        storm_points.append(updated)
    return {**world, "storm_points": storm_points}


def build_game_config(
    world: dict,
    constants: dict[str, Any],
    game_ms: int,
    focus_xy: dict[str, float] | None = None,
    aspect_ratio: float = DEFAULT_VISUALIZATION_ASPECT_RATIO_VALUE,
    overview_position: str = "top-right",
) -> dict:
    participants = [1, 2]
    players = initial_players(participants, world)
    active_player_position = visualization_active_player_position(
        world, focus_xy, aspect_ratio
    )
    other_player_position = visualization_other_player_position(
        world,
        focus_xy,
        active_player_position,
        aspect_ratio,
        overview_position,
    )
    if active_player_position is not None:
        players["1"].update(active_player_position)
        if focus_xy is not None:
            dx = float(focus_xy["x"]) - active_player_position["x"]
            dy = float(focus_xy["y"]) - active_player_position["y"]
            distance = math.hypot(dx, dy)
            if distance > 0:
                # Game3D derives the car heading from velocity. A tiny
                # velocity points the parked visualization car at the focus.
                players["1"]["vx"] = round(dx / distance * 0.1, 4)
                players["1"]["vy"] = round(dy / distance * 0.1, 4)
    if other_player_position is not None:
        players["2"].update(other_player_position)
        if focus_xy is not None:
            dx = float(focus_xy["x"]) - other_player_position["x"]
            dy = float(focus_xy["y"]) - other_player_position["y"]
            distance = math.hypot(dx, dy)
            if distance > 0:
                players["2"]["vx"] = round(dx / distance * 0.1, 4)
                players["2"]["vy"] = round(dy / distance * 0.1, 4)
    return {
        "mode": "game",
        "session_id": f"visualization:{world['world_id']}",
        "participant_id": 1,
        "participant_ids": participants,
        "group_id": 1,
        "role": "Player 1",
        "world_id": world["world_id"],
        "canvas_size": world["canvas_size"],
        "canvas_width": constants["CANVAS_RENDER_WIDTH"],
        "canvas_height": round(constants["CANVAS_RENDER_WIDTH"] / aspect_ratio),
        "trial_seconds": constants["TRIAL_SECONDS"],
        "send_interval_ms": constants["SEND_INTERVAL_MS"],
        "draw_interval_ms": constants["DRAW_INTERVAL_MS"],
        "player_radius": constants["PLAYER_RADIUS"],
        "initial_players": players,
        "initial_player": players["1"],
        "coin_radius": world["coin_radius"],
        "coin_bonus": constants["COIN_BONUS"],
        "max_player_speed": world["max_player_speed"],
        "speed_limit_mph": world.get("speed_limit_mph", 60),
        "projection": world.get("projection", {}),
        "storm_points": world.get("storm_points", []),
        "chaser_tracks": world.get("chaser_tracks", [])[
            : constants["STORM_CHASER_TRACK_COUNT"]
        ],
        "storm_chaser_track_count": constants["STORM_CHASER_TRACK_COUNT"],
        "chaser_track_visible_ms": constants["CHASER_TRACK_VISIBLE_MS"],
        "warnings": world.get("warnings", []),
        "reward_events": world.get("reward_events", []),
        "timing": {
            "game_start_ms": 0,
            "game_end_ms": constants["TRIAL_SECONDS"] * 1000,
            "raw_time_min_ms": world["raw_time_min_ms"],
            "raw_time_max_ms": world["raw_time_max_ms"],
        },
        "visualization_game_ms": game_ms,
    }


def render_game_html(
    config: dict,
    aspect_ratio_style: str,
    overview_position: str = "top-right",
) -> str:
    env = Environment(
        loader=FileSystemLoader(PROJECT_ROOT / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("game.html")
    html_text = str(
        template.module.game_control(SimpleNamespace(canvas_config=config))
    )
    config_json = json.dumps(config).replace("</", "<\\/")
    css_uri = html.escape(
        html_file_uri(STATIC_ROOT / "css" / "game.css"), quote=True
    )
    three_uri = html.escape(
        html_file_uri(STATIC_ROOT / "js" / "three.min.js"), quote=True
    )
    renderer_uri = html.escape(
        html_file_uri(STATIC_ROOT / "js" / "game_3d.js"), quote=True
    )
    overview_position_css = ""
    if overview_position == "top-left":
        overview_position_css = """
#overview-map {
  left: 12px;
  right: auto;
}
.overview-label {
  left: 20px;
  right: auto;
}
"""
    prelude = """
<link rel="stylesheet" href="__CSS_URI__">
<script>
window.visualizationConfig = __CONFIG_JSON__;
window.psynet = {
  nextPage: function () {},
  addPageEventListener: function (target, eventName, handler, options) {
    if (target && target.addEventListener) target.addEventListener(eventName, handler, options);
  },
  addPageCleanupCallback: function () {},
  session: {
    participant_id: 1,
    onFreshState: function (handler) {
      window.requestAnimationFrame(function () {
        var cfg = window.visualizationConfig;
        handler({
          started: true,
          ready_participant_ids: cfg.participant_ids.map(String),
          state: {
            server_start_time: new Date(
              Date.now() - Number(cfg.visualization_game_ms || 0)
            ).toISOString(),
            bonuses: {"1": 0, "2": 0},
            collected_coins: []
          },
          timing: cfg.timing
        });
      });
      return function () {};
    },
    onEnd: function () { return function () {}; },
    ready: function () {}
  },
  websocket: {
    handle: function () { return function () {}; },
    send: function () {}
  }
};
window.dallinger = {identity: {workerId: "visualization"}};
</script>
<style>
html,
body {
  margin: 0;
  background: #ffffff;
}
.canvas-shell,
.canvas-stage {
  width: 1000px;
  max-width: none;
}
#game-canvas {
  aspect-ratio: __ASPECT_RATIO__;
}
.bonus-pill {
  margin-top: 0;
  margin-bottom: 1.2rem;
  padding: 0.75rem 1.35rem;
  font-size: 1.5rem;
  line-height: 1.35;
}
.zoom-control,
.canvas-help,
.canvas-legend {
  display: none !important;
}
__OVERVIEW_POSITION_CSS__
</style>
"""
    prelude = (
        prelude.replace("__CSS_URI__", css_uri)
        .replace("__CONFIG_JSON__", config_json)
        .replace("__ASPECT_RATIO__", aspect_ratio_style)
        .replace("__OVERVIEW_POSITION_CSS__", overview_position_css)
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Airdrop Game Snapshot</title>{prelude}</head><body>"
        f"{html_text}"
        f'<script src="{three_uri}"></script>'
        f'<script src="{renderer_uri}"></script>'
        "<script>Game3D.start(window.visualizationConfig);</script>"
        "</body></html>"
    )


def html_file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def find_local_chrome_executable() -> Path | None:
    browser_root = PROJECT_ROOT / ".playwright-browsers"
    matches = sorted(
        browser_root.glob(
            "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        )
    )
    return matches[0] if matches else None


def extract_original_time_constants(source_html: Path) -> tuple[int, int]:
    text = source_html.read_text(encoding="utf-8")
    time_min = re.search(r"const\s+TIME_MIN\s*=\s*(\d+)", text)
    step_ms = re.search(r"const\s+STEP_MS\s*=\s*(\d+)", text)
    if not time_min or not step_ms:
        raise ConfigError(f"Could not parse TIME_MIN/STEP_MS from {source_html}")
    return int(time_min.group(1)), int(step_ms.group(1))


def storm_vil_lookup(session_dir: Path) -> dict[str, dict[str, float]]:
    lookup: dict[str, dict[str, float]] = {}
    for storm in _load_storms(session_dir / "storms.csv"):
        keys = {str(storm["id"])}
        if storm.get("storm_id"):
            keys.add(str(storm["storm_id"]))
        for point in storm.get("points", []):
            timestamp = str(int(point[0]))
            vil = point[3]
            if vil is None:
                continue
            for key in keys:
                lookup.setdefault(key, {})[timestamp] = float(vil)
    return lookup


def snapshot_game_times(trial_seconds: int, count: int) -> list[int]:
    count = max(1, count)
    end_ms = trial_seconds * 1000
    return [round((index + 1) * end_ms / (count + 1)) for index in range(count)]


def game_to_raw_time(world: dict, game_ms: int, constants: dict[str, Any]) -> int:
    game_end = constants["TRIAL_SECONDS"] * 1000
    amount = min(1.0, max(0.0, game_ms / game_end if game_end else 0.0))
    return round(
        world["raw_time_min_ms"]
        + amount * (world["raw_time_max_ms"] - world["raw_time_min_ms"])
    )


def mean_point(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not points:
        return None
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def active_world_focus_xy(world: dict, game_ms: int) -> dict[str, float] | None:
    reward_points = []
    for event in world.get("reward_events", []):
        if int(event["start_ms"]) <= game_ms <= int(event["end_ms"]):
            if event.get("line"):
                reward_points.extend(
                    (float(point[0]), float(point[1])) for point in event["line"]
                )
            else:
                reward_points.append((float(event["x"]), float(event["y"])))
    focus = mean_point(reward_points)
    if focus is not None:
        return {"x": round(focus[0], 2), "y": round(focus[1], 2)}

    warning_points = []
    for warning in world.get("warnings", []):
        if int(warning["eff_ms"]) <= game_ms <= int(warning["exp_ms"]):
            for polygon in warning.get("polygons", []):
                warning_points.extend(
                    (float(point[0]), float(point[1])) for point in polygon
                )
    focus = mean_point(warning_points)
    if focus is not None:
        return {"x": round(focus[0], 2), "y": round(focus[1], 2)}

    storm_points = []
    for point in world.get("storm_points", []):
        if int(point[0]) <= game_ms <= int(point[1]):
            storm_points.append((float(point[2]), float(point[3])))
    focus = mean_point(storm_points)
    if focus is not None:
        return {"x": round(focus[0], 2), "y": round(focus[1], 2)}

    return None


def active_focus_lat_lng(session_dir: Path, raw_ms: int) -> tuple[float, float] | None:
    points: list[tuple[float, float]] = []
    warnings = _load_warnings(session_dir / "warnings.csv")
    for warning in warnings:
        if warning["eff_ms"] <= raw_ms <= warning["exp_ms"]:
            for polygon in warning["polygons"]:
                points.extend(polygon)
    if points:
        return mean_point(points)

    tornadoes = _load_tornadoes(session_dir / "tornadoes.csv")
    for tornado in tornadoes:
        if tornado["start_ms"] <= raw_ms <= tornado["end_ms"]:
            if tornado.get("line"):
                points.extend(tornado["line"])
            else:
                points.append(tornado["point"])
    if points:
        return mean_point(points)

    storms = _load_storms(session_dir / "storms.csv")
    for storm in storms:
        for point in storm.get("points", []):
            if abs(int(point[0]) - raw_ms) <= 5 * 60 * 1000:
                points.append((point[1], point[2]))
    return mean_point(points)


def project_focus(
    world: dict, focus_lat_lng: tuple[float, float] | None
) -> dict[str, float] | None:
    if focus_lat_lng is None:
        return None
    projection = world.get("projection", {})
    lat = focus_lat_lng[0]
    lng = focus_lat_lng[1]
    mid_lat = float(projection["mid_lat"])
    scale = float(projection["scale"])
    canvas_size = float(world["canvas_size"])
    margin = float(projection.get("margin", 36))
    lat_min = float(projection["lat_min"])
    lat_max = float(projection["lat_max"])
    lng_min = float(projection["lng_min"])
    lng_max = float(projection["lng_max"])
    cos_lat = max(0.2, math.cos(math.radians(mid_lat)))
    min_x = lng_min * cos_lat
    max_x = lng_max * cos_lat
    width = max(max_x - min_x, 1e-9)
    height = max(lat_max - lat_min, 1e-9)
    offset_x = (canvas_size - width * scale) / 2
    offset_y = (canvas_size - height * scale) / 2
    x_raw = lng * cos_lat
    x = offset_x + (x_raw - min_x) * scale
    y = offset_y + (lat_max - lat) * scale
    return {
        "x": round(max(margin, min(canvas_size - margin, x)), 2),
        "y": round(max(margin, min(canvas_size - margin, y)), 2),
    }


def unproject_xy(world: dict, x: float, y: float) -> tuple[float, float]:
    projection = world.get("projection", {})
    mid_lat = float(projection["mid_lat"])
    scale = float(projection["scale"])
    canvas_size = float(world["canvas_size"])
    lat_min = float(projection["lat_min"])
    lat_max = float(projection["lat_max"])
    lng_min = float(projection["lng_min"])
    lng_max = float(projection["lng_max"])
    cos_lat = max(0.2, math.cos(math.radians(mid_lat)))
    min_x = lng_min * cos_lat
    max_x = lng_max * cos_lat
    width = max(max_x - min_x, 1e-9)
    height = max(lat_max - lat_min, 1e-9)
    offset_x = (canvas_size - width * scale) / 2
    offset_y = (canvas_size - height * scale) / 2
    lng = ((x - offset_x) / scale + min_x) / cos_lat
    lat = lat_max - (y - offset_y) / scale
    return lat, lng


def unproject_focus(
    world: dict, focus_xy: dict[str, float] | None
) -> tuple[float, float] | None:
    if focus_xy is None:
        return None
    return unproject_xy(world, float(focus_xy["x"]), float(focus_xy["y"]))


def viewport_geo_bounds(
    world: dict,
    focus_xy: dict[str, float] | None,
    *,
    viewport_miles: float = VISUALIZATION_VIEWPORT_MILES,
    aspect_ratio: float = DEFAULT_VISUALIZATION_ASPECT_RATIO_VALUE,
) -> list[list[float]] | None:
    if focus_xy is None:
        return None
    projection = world.get("projection", {})
    px_per_mile = float(projection["px_per_mile"])
    if px_per_mile <= 0:
        return None
    width_px = viewport_miles * px_per_mile
    height_px = width_px / aspect_ratio
    canvas_size = float(world["canvas_size"])
    camera_x = max(0, min(canvas_size - width_px, float(focus_xy["x"]) - width_px / 2))
    camera_y = max(
        0, min(canvas_size - height_px, float(focus_xy["y"]) - height_px / 2)
    )
    north_west = unproject_xy(world, camera_x, camera_y)
    south_east = unproject_xy(world, camera_x + width_px, camera_y + height_px)
    south = min(north_west[0], south_east[0])
    north = max(north_west[0], south_east[0])
    west = min(north_west[1], south_east[1])
    east = max(north_west[1], south_east[1])
    return [[south, west], [north, east]]


def browser_screenshot(page, output_path: Path, *, selector: str | None = None) -> None:
    page.wait_for_timeout(500)
    if selector is None:
        page.screenshot(path=str(output_path))
    else:
        page.locator(selector).screenshot(path=str(output_path))


def screenshot_original_map(
    page,
    source_html: Path,
    raw_ms: int,
    output_path: Path,
    focus_lat_lng: tuple[float, float] | None,
    geo_bounds: list[list[float]] | None,
    player_view: dict[str, float] | None,
    other_player_views: list[dict[str, Any]],
    vil_lookup: dict[str, dict[str, float]],
    aspect_ratio: float,
) -> None:
    time_min, step_ms = extract_original_time_constants(source_html)
    step = max(0, round((raw_ms - time_min) / step_ms))
    page.set_viewport_size({"width": 960, "height": round(960 / aspect_ratio)})
    page.goto(html_file_uri(source_html), wait_until="networkidle")
    page.evaluate(
        """async ([
          step,
          focus,
          bounds,
          playerView,
          otherPlayerViews,
          vilByStorm
        ]) => {
          const slider = document.getElementById('timeSlider');
          const alerts = document.getElementById('alertsToggle');
          const storms = document.getElementById('stormsToggle');
          const tornadoes = document.getElementById('tornadoesToggle');
          let basemapReady = Promise.resolve();
          if (typeof map !== 'undefined' && typeof L !== 'undefined') {
            map.eachLayer(function (layer) {
              if (layer instanceof L.TileLayer) map.removeLayer(layer);
            });
            const basemap = L.tileLayer(
              'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
              {
                attribution: '&copy; OpenStreetMap contributors',
                maxZoom: 19
              }
            );
            basemapReady = new Promise(function (resolve) {
              basemap.once('load', resolve);
              window.setTimeout(resolve, 5000);
            });
            basemap.addTo(map);
          }
          if (alerts) alerts.checked = true;
          if (storms) storms.checked = true;
          if (tornadoes) tornadoes.checked = true;
          if (slider) {
            slider.value = Math.min(Number(slider.max || step), step);
          }
          if (typeof stormColor === 'function') {
            stormColor = function (intensity) {
              if (intensity == null || Number.isNaN(Number(intensity))) return '#377eb8';
              return interpolateColor([
                [0, '#377eb8'],
                [10, '#4daf4a'],
                [25, '#ffff33'],
                [45, '#ff7f00'],
                [65, '#e41a1c'],
                [85, '#984ea3']
              ], Number(intensity));
            };
          }
          if (typeof STORMS !== 'undefined' && vilByStorm) {
            STORMS.forEach(function (storm) {
              const baseId = String(storm.id || '').split('#')[0];
              const values = vilByStorm[String(storm.id)] ||
                vilByStorm[baseId] ||
                vilByStorm[String(storm.storm_id || '')];
              if (!values) return;
              (storm.points || []).forEach(function (point) {
                const vil = values[String(point[0])];
                if (vil != null && Number.isFinite(Number(vil))) {
                  point[3] = Number(vil);
                }
              });
            });
          }
          if (typeof render === 'function') {
            render(slider ? Number(slider.value) : step);
          }
          const controls = document.getElementById('controls');
          const search = document.getElementById('searchBox');
          if (controls) controls.style.display = 'none';
          if (search) search.style.display = 'none';
          const legend = document.getElementById('legend');
          if (legend) {
            legend.innerHTML = `
              <div class="viz-legend-title">Airdrop cues</div>
              <div class="viz-legend-row">
                <span class="viz-legend-swatch alert-tornado"></span>
                <span>Higher-chance landing zone</span>
              </div>
              <div class="viz-legend-row">
                <span class="viz-legend-swatch alert-storm"></span>
                <span>Medium-chance landing zone</span>
              </div>
              <div class="viz-legend-row">
                <span class="viz-legend-swatch storm-cell"></span>
                <span>Airplane sighting</span>
              </div>
            `;
            legend.style.padding = '19px 24px';
            legend.style.borderRadius = '11px';
            legend.style.fontSize = '24px';
            legend.style.lineHeight = '1.35';
            legend.style.boxShadow = '0 1px 5px rgba(0,0,0,.12)';
            const style = document.createElement('style');
            style.textContent = `
              #legend .viz-legend-title {
                margin: 0 0 11px;
                color: #c0392b;
                font-size: 24px;
                font-weight: 700;
              }
              #legend .viz-legend-row {
                display: flex;
                align-items: center;
                gap: 12px;
                margin: 7px 0;
                white-space: nowrap;
              }
              #legend .viz-legend-swatch {
                box-sizing: border-box;
                display: inline-block;
                width: 27px;
                height: 27px;
                flex: 0 0 27px;
                border-radius: 3px;
              }
              #legend .alert-tornado {
                background: #ff333355;
                border: 2px solid #cc0000;
              }
              #legend .alert-storm {
                background: #ffee4444;
                border: 2px solid #ddaa00;
              }
              #legend .storm-cell {
                background: linear-gradient(90deg, #377eb8, #4daf4a, #ffff33, #ff7f00, #e41a1c, #984ea3);
                border: 2px solid #225a8d;
              }
            `;
            document.head.appendChild(style);
          }
          const mapElement = document.getElementById('map');
          if (mapElement) mapElement.style.bottom = '0px';
          if (typeof map !== 'undefined' && typeof map.invalidateSize === 'function') {
            map.options.zoomSnap = 0;
            map.invalidateSize();
          }
          if (bounds && typeof map !== 'undefined' && typeof map.fitBounds === 'function') {
            const contextBounds = (
              typeof L !== 'undefined' && typeof L.latLngBounds === 'function'
            )
              ? L.latLngBounds(bounds).pad(0.16)
              : bounds;
            map.fitBounds(contextBounds, {animate: false, padding: [0, 0]});
          } else if (focus && typeof map !== 'undefined' && typeof map.setView === 'function') {
            map.setView([focus[0], focus[1]], 8, {animate: false});
          }
          if (
            bounds &&
            typeof map !== 'undefined' &&
            typeof L !== 'undefined' &&
            typeof L.rectangle === 'function'
          ) {
            const paneName = 'preview-bounds-pane';
            if (!map.getPane(paneName)) {
              const pane = map.createPane(paneName);
              pane.style.zIndex = '650';
              pane.style.pointerEvents = 'none';
            }
            L.rectangle(bounds, {
              pane: paneName,
              color: '#111111',
              weight: 5,
              opacity: 1,
              fill: false,
              interactive: false
            }).addTo(map);
          }
          if (
            playerView &&
            typeof map !== 'undefined' &&
            typeof L !== 'undefined'
          ) {
            const conePaneName = 'preview-cone-pane';
            if (!map.getPane(conePaneName)) {
              const pane = map.createPane(conePaneName);
              pane.style.zIndex = '625';
              pane.style.pointerEvents = 'none';
            }
            const markerPaneName = 'preview-car-pane';
            if (!map.getPane(markerPaneName)) {
              const pane = map.createPane(markerPaneName);
              pane.style.zIndex = '675';
              pane.style.pointerEvents = 'none';
            }
            const carLatLng = L.latLng(playerView.lat, playerView.lng);
            const carPoint = map.latLngToLayerPoint(carLatLng);
            const conePoints = [carLatLng];
            const halfAngle = 0.48;
            const coneRange = 160;
            const segmentCount = 18;
            for (let index = 0; index <= segmentCount; index += 1) {
              const angle = (
                playerView.heading -
                halfAngle +
                (2 * halfAngle * index) / segmentCount
              );
              conePoints.push(map.layerPointToLatLng([
                carPoint.x + Math.cos(angle) * coneRange,
                carPoint.y + Math.sin(angle) * coneRange
              ]));
            }
            L.polygon(conePoints, {
              pane: conePaneName,
              color: '#111827',
              weight: 3,
              opacity: 0.85,
              fillColor: '#d3d3d3',
              fillOpacity: 0.42,
              interactive: false
            }).addTo(map);
            L.circleMarker(carLatLng, {
              pane: markerPaneName,
              radius: 16,
              color: '#111827',
              weight: 6,
              opacity: 1,
              fillColor: '#1f77b4',
              fillOpacity: 1,
              interactive: false
            }).addTo(map);
            (otherPlayerViews || []).forEach(function (otherPlayer) {
              L.circleMarker([otherPlayer.lat, otherPlayer.lng], {
                pane: markerPaneName,
                radius: 18,
                color: '#ffffff',
                weight: 5,
                opacity: 1,
                fillColor: otherPlayer.color || '#d62728',
                fillOpacity: 1,
                interactive: false
              }).addTo(map);
            });
          }
          await basemapReady;
        }""",
        [
            step,
            list(focus_lat_lng) if focus_lat_lng else None,
            geo_bounds,
            player_view,
            other_player_views,
            vil_lookup,
        ],
    )
    browser_screenshot(page, output_path, selector="#map")


def screenshot_experimental_ui(
    page, html_path: Path, output_path: Path, aspect_ratio: float
) -> None:
    page.set_viewport_size(
        {"width": 1100, "height": max(760, round(1000 / aspect_ratio) + 160)}
    )
    page.goto(html_file_uri(html_path), wait_until="domcontentloaded")
    browser_screenshot(page, output_path, selector=".canvas-shell")


def image_data_uri(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def compose_html(
    *,
    session_label: str,
    snapshot_label: str,
    raw_time_label: str,
    real_map_path: Path,
    ui_path: Path,
    aspect_ratio: float,
    no_screen: bool,
) -> str:
    real_map = image_data_uri(real_map_path)
    ui = image_data_uri(ui_path)
    panel_height = 360
    panel_width = round(panel_height * aspect_ratio)
    arrow_width = COMPOSITE_ARROW_WIDTH
    gap = 20
    horizontal_padding = 54
    monitor_padding_x = 48
    monitor_width = panel_width + monitor_padding_x
    right_panel_width = panel_width if no_screen else monitor_width
    stage_width = (
        horizontal_padding * 2 + panel_width + arrow_width + right_panel_width + gap * 2
    )
    stage_height = panel_height + 24 if no_screen else 500
    monitor_stand_inset = round(monitor_width * 0.35)
    monitor_base_inset = round(monitor_width * 0.27)
    keyboard_html = (
        """
      <div class="keyboard-arrows" aria-hidden="true">
        <div class="key up">&uarr;</div>
        <div class="key left">&larr;</div>
        <div class="key down">&darr;</div>
        <div class="key right">&rarr;</div>
      </div>"""
        if no_screen
        else ""
    )
    right_panel_html = (
        f"""<div class="right-panel">
    <img class="screenless-ui" src="{ui}" alt="Shared canvas experiment UI">
    {keyboard_html}
  </div>"""
        if no_screen
        else f"""<div class="monitor">
      <div class="screen"><img src="{ui}" alt="Shared canvas experiment UI"></div>
    </div>"""
    )
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
html, body {{
  margin: 0;
  width: {stage_width}px;
  height: {stage_height}px;
  background: #ffffff;
  font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
.stage {{
  box-sizing: border-box;
  width: {stage_width}px;
  height: {stage_height}px;
  padding: 0 {horizontal_padding}px;
  display: grid;
  grid-template-columns: {panel_width}px {arrow_width}px {right_panel_width}px;
  gap: {gap}px;
  align-items: start;
}}
.real-map {{
  width: 100%;
  height: {panel_height}px;
  margin-top: 24px;
  object-fit: contain;
  display: block;
}}
.arrow {{
  display: flex;
  align-items: center;
  justify-content: center;
  height: {panel_height}px;
  margin-top: 24px;
}}
.arrow-line {{
  width: {COMPOSITE_ARROW_LINE_WIDTH}px;
  height: 6px;
  background: #111111;
  position: relative;
}}
.arrow-line:after {{
  content: "";
  position: absolute;
  right: -3px;
  top: -9px;
  border-left: 22px solid #111111;
  border-top: 12px solid transparent;
  border-bottom: 12px solid transparent;
}}
.monitor {{
  background: #162033;
  border-radius: 30px 30px 20px 20px;
  padding: 24px 24px 42px;
  position: relative;
}}
.monitor:after {{
  content: "";
  position: absolute;
  left: {monitor_stand_inset}px;
  right: {monitor_stand_inset}px;
  bottom: -40px;
  height: 40px;
  background: #162033;
  border-radius: 0 0 18px 18px;
}}
.monitor:before {{
  content: "";
  position: absolute;
  left: {monitor_base_inset}px;
  right: {monitor_base_inset}px;
  bottom: -56px;
  height: 16px;
  background: #253249;
  border-radius: 999px;
}}
.screen {{
  background: #f8fbff;
  border-radius: 20px;
  overflow: hidden;
  border: 1px solid #44546d;
}}
.screen img {{
  width: 100%;
  height: {panel_height}px;
  object-fit: contain;
  display: block;
  background: #f8fbff;
}}
.right-panel {{
  position: relative;
  width: {panel_width}px;
  height: {panel_height}px;
  margin-top: 24px;
}}
.screenless-ui {{
  width: 100%;
  height: 100%;
  object-fit: contain;
  display: block;
  background: #f8fbff;
}}
.keyboard-arrows {{
  position: absolute;
  z-index: 2;
  right: 30px;
  bottom: 12px;
  display: grid;
  grid-template-columns: repeat(3, 28px);
  grid-template-rows: repeat(2, 28px);
  gap: 3px;
  filter: drop-shadow(0 2px 4px rgba(0, 0, 0, 0.25));
}}
.key {{
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 5px;
  border: 1px solid #9ca3af;
  background: linear-gradient(#ffffff, #e5e7eb);
  color: #111827;
  font-size: 17px;
  font-weight: 700;
  box-shadow: inset 0 -2px 0 #cbd5e1;
}}
.key.up {{
  grid-column: 2;
}}
.key.left {{
  grid-column: 1;
  grid-row: 2;
}}
.key.down {{
  grid-column: 2;
  grid-row: 2;
}}
.key.right {{
  grid-column: 3;
  grid-row: 2;
}}
</style>
</head>
<body>
<div class="stage">
  <div>
    <img class="real-map" src="{real_map}" alt="Original real-data session map">
  </div>
  <div class="arrow">
    <div class="arrow-line"></div>
  </div>
  <div>
    {right_panel_html}
  </div>
</div>
</body>
</html>"""


def screenshot_composite(
    page, html_path: Path, output_path: Path, aspect_ratio: float, no_screen: bool
) -> None:
    panel_height = 360
    panel_width = round(panel_height * aspect_ratio)
    right_panel_width = panel_width if no_screen else panel_width + 48
    stage_width = (
        54 * 2
        + panel_width
        + COMPOSITE_ARROW_WIDTH
        + right_panel_width
        + 20 * 2
    )
    stage_height = panel_height + 24 if no_screen else 500
    page.set_viewport_size({"width": stage_width, "height": stage_height})
    page.goto(html_file_uri(html_path), wait_until="domcontentloaded")
    browser_screenshot(page, output_path)


def utc_label(raw_ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(raw_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def run() -> None:
    args = parse_args()
    aspect_ratio = parse_aspect_ratio(args.aspect_ratio)
    aspect_ratio_style = aspect_ratio_css(args.aspect_ratio)
    output_scale = max(1, float(args.output_scale))
    constants = load_experiment_constants()
    output_dir = args.output_dir
    intermediate_dir = output_dir / "_intermediate"
    output_dir.mkdir(parents=True, exist_ok=True)
    intermediate_dir.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ConfigError(
            "Playwright is required for actual browser screenshots. Install it "
            "and run `python -m playwright install chromium`."
        ) from exc

    session_dirs = discover_session_dirs(args.session_root, args.session_limit)
    game_times = snapshot_game_times(constants["TRIAL_SECONDS"], args.snapshots)

    with sync_playwright() as p:
        executable_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        if executable_path is None:
            local_chrome = find_local_chrome_executable()
            if local_chrome is not None:
                executable_path = str(local_chrome)
        launch_kwargs = {}
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page(device_scale_factor=output_scale)
        page.route(
            re.compile(r"https://[^/]*basemaps\.cartocdn\.com/.*"),
            lambda route: route.abort(),
        )
        try:
            for session_dir in session_dirs:
                source_html = args.source_html_root / session_dir.name / "session.html"
                if not source_html.exists():
                    raise ConfigError(f"Missing original HTML: {source_html}")
                source_storm_vil = storm_vil_lookup(source_html.parent)

                world = build_world_from_session_dir(
                    session_dir,
                    canvas_size=constants["CANVAS_SIZE"],
                    trial_seconds=constants["TRIAL_SECONDS"],
                    coin_radius=constants["COIN_RADIUS"],
                    coin_bonus=constants["COIN_BONUS"],
                )
                visualization_world = extend_storm_persistence_for_visualization(world)

                for index, game_ms in enumerate(game_times, start=1):
                    raw_ms = game_to_raw_time(visualization_world, game_ms, constants)
                    stem = f"{session_dir.name}_snapshot-{index:02d}"
                    real_png = intermediate_dir / f"{stem}_real-map.png"
                    ui_png = intermediate_dir / f"{stem}_experiment-ui.png"
                    ui_html = intermediate_dir / f"{stem}_experiment-ui.html"
                    composite_html = intermediate_dir / f"{stem}_composite.html"
                    final_png = output_dir / f"{stem}.png"

                    focus_xy = active_world_focus_xy(visualization_world, game_ms)
                    focus_lat_lng = unproject_focus(visualization_world, focus_xy)
                    if focus_lat_lng is None:
                        focus_lat_lng = active_focus_lat_lng(source_html.parent, raw_ms)
                        focus_xy = project_focus(visualization_world, focus_lat_lng)
                    geo_bounds = viewport_geo_bounds(
                        visualization_world, focus_xy, aspect_ratio=aspect_ratio
                    )
                    config = build_game_config(
                        visualization_world,
                        constants,
                        game_ms,
                        focus_xy,
                        aspect_ratio,
                        args.overview_position,
                    )
                    player = config["initial_player"]
                    player_lat, player_lng = unproject_xy(
                        visualization_world,
                        float(player["x"]),
                        float(player["y"]),
                    )
                    player_view = {
                        "lat": player_lat,
                        "lng": player_lng,
                        "heading": math.atan2(
                            float(player.get("vy", 0)),
                            float(player.get("vx", 0)),
                        ),
                    }
                    other_player_views = []
                    for participant_id, other_player in config[
                        "initial_players"
                    ].items():
                        if str(participant_id) == str(config["participant_id"]):
                            continue
                        other_lat, other_lng = unproject_xy(
                            visualization_world,
                            float(other_player["x"]),
                            float(other_player["y"]),
                        )
                        other_player_views.append(
                            {
                                "lat": other_lat,
                                "lng": other_lng,
                                "color": other_player.get("color", "#d62728"),
                            }
                        )
                    if args.reuse_intermediate:
                        missing = [
                            path for path in (real_png, ui_png) if not path.exists()
                        ]
                        if missing:
                            raise ConfigError(
                                "Missing intermediate PNGs: "
                                + ", ".join(str(path) for path in missing)
                            )
                    else:
                        ui_html.write_text(
                            render_game_html(
                                config,
                                aspect_ratio_style,
                                args.overview_position,
                            ),
                            encoding="utf-8",
                        )
                        if args.reuse_original_map:
                            if not real_png.exists():
                                raise ConfigError(
                                    f"Missing original-map PNG: {real_png}"
                                )
                        else:
                            screenshot_original_map(
                                page,
                                source_html,
                                raw_ms,
                                real_png,
                                focus_lat_lng,
                                geo_bounds,
                                player_view,
                                other_player_views,
                                source_storm_vil,
                                aspect_ratio,
                            )
                        screenshot_experimental_ui(
                            page, ui_html, ui_png, aspect_ratio
                        )
                    composite_html.write_text(
                        compose_html(
                            session_label=session_dir.name,
                            snapshot_label=f"snapshot {index}/{len(game_times)}",
                            raw_time_label=utc_label(raw_ms),
                            real_map_path=real_png,
                            ui_path=ui_png,
                            aspect_ratio=aspect_ratio,
                            no_screen=args.no_screen,
                        ),
                        encoding="utf-8",
                    )
                    screenshot_composite(
                        page, composite_html, final_png, aspect_ratio, args.no_screen
                    )
                    print(f"Wrote {final_png}")
        finally:
            browser.close()

    if not args.keep_html:
        for path in intermediate_dir.glob("*.html"):
            path.unlink()


if __name__ == "__main__":
    try:
        run()
    except ConfigError as err:
        raise SystemExit(str(err)) from err
