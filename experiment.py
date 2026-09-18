from __future__ import annotations

import math
import os
import random
from datetime import datetime, timezone
from typing import ClassVar, List

from dallinger.experiment import experiment_route
from dallinger.experiment_server.utils import success_response
from dominate import tags
from flask import request
from pydantic import Field
from sqlalchemy import Column, String

import psynet.experiment
from psynet.asset import asset
from psynet.bot import BotDriver, advance_past_wait_pages
from psynet.db import with_transaction
from psynet.field import PythonDict, PythonList
from psynet.modular_page import Control, ModularPage
from psynet.page import InfoPage, WaitPage
from psynet.participant import Participant
from psynet.session import (
    LiveSession,
    LiveSessionControl,
    LiveSessionInitializer,
    session,
)
from psynet.sync import Barrier, GroupBarrier, SimpleGrouper
from psynet.timeline import PageMaker, Timeline, join
from psynet.trial.static import StaticNode, StaticTrial, StaticTrialMaker
from psynet.websocket import ClientWebSocketMessage, ServerWebSocketMessage

from psynet.consent import NoConsent

if __package__:
    from .session_worlds import (
        load_world_definitions,
        node_definition_from_world,
        public_world_payload,
        write_world_json,
    )
else:
    from session_worlds import (
        load_world_definitions,
        node_definition_from_world,
        public_world_payload,
        write_world_json,
    )


GROUP_TYPE = "shared_canvas_group"
GROUPER_ID = f"{GROUP_TYPE}_grouper"
GROUP_SIZE = 2
CANVAS_SIZE = 640
CANVAS_RENDER_WIDTH = 960
CANVAS_RENDER_HEIGHT = 540
TRIAL_SECONDS = 90
SEND_INTERVAL_MS = 100
DRAW_INTERVAL_MS = 25
PLAYER_RADIUS = 12
COIN_RADIUS = 10
COIN_BONUS = 0.10
STORM_CHASER_TRACK_COUNT = 8
CHASER_TRACK_VISIBLE_MS = 30 * 60 * 1000
POSITION_EVENT = "position"
COLLECT_EVENT = "collect"
LOBBY_POSITION_EVENT = "lobby_position"
LOBBY_PAGE_LABEL = "canvas_lobby"
LOBBY_MAX_PLAYER_SPEED = 80
STATIC_SESSION_ROOT = os.path.join(os.path.dirname(__file__), "static")
COLLECTION_TIME_GRACE_MS = 750

PLAYER_COLORS = [
    "#1f77b4",
    "#d62728",
    "#2ca02c",
    "#9467bd",
    "#ff7f0e",
    "#17becf",
]


def clamp(value, low, high):
    return max(low, min(high, value))


def get_world_nodes():
    worlds = load_world_definitions(
        static_root=STATIC_SESSION_ROOT,
        canvas_size=CANVAS_SIZE,
        trial_seconds=TRIAL_SECONDS,
        coin_radius=COIN_RADIUS,
        coin_bonus=COIN_BONUS,
        include_browser_layers=False,
    )
    return [
        StaticNode(
            definition=node_definition_from_world(world),
            assets={
                "world": asset(write_world_json, cache=True, extension=".json"),
            },
        )
        for world in worlds
    ]


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return value.astimezone(timezone.utc)


def receive_time_iso(receive_time: datetime):
    return as_utc(receive_time).isoformat()


def lobby_spawn_for_participant(participant_id: int) -> dict:
    rng = random.Random(int(participant_id) * 7919)
    margin = 80
    return {
        "participant_id": str(participant_id),
        "label": f"Player {participant_id}",
        "color": PLAYER_COLORS[int(participant_id) % len(PLAYER_COLORS)],
        "x": round(rng.uniform(margin, CANVAS_SIZE - margin), 2),
        "y": round(rng.uniform(margin, CANVAS_SIZE - margin), 2),
        "vx": 0.0,
        "vy": 0.0,
        "heading": 0.0,
    }


class CanvasGameState(LiveSession):
    """Authoritative live session for one canvas game."""

    params = Column(PythonDict, default=lambda: {})
    coins = Column(PythonList, default=lambda: [])
    awarded_target_keys = Column(PythonList, default=lambda: [])
    collected_coins = Column(PythonList, default=lambda: [])
    bonuses = Column(PythonDict, default=lambda: {})
    collection_counts = Column(PythonDict, default=lambda: {})
    reward_targets = Column(PythonList, default=lambda: [])
    server_start_time = Column(String(64), nullable=True)

    def initialize(self, participant_ids, group):
        """Initialize public resumable state for a synchronized canvas group."""

        definition = self.node.definition
        state = self.initial_state(participant_ids, definition)
        self.params = state["params"]
        self.coins = state["coins"]
        self.awarded_target_keys = state["awarded_target_keys"]
        self.collected_coins = state["collected_coins"]
        self.bonuses = state["bonuses"]
        self.collection_counts = state["collection_counts"]
        # Copied once at trial start so collect never reads node.definition or assets.
        self.reward_targets = list(definition.get("reward_targets") or [])
        self.server_start_time = state["server_start_time"]

    @staticmethod
    def _spotter_spawn_points(world: dict, rng: random.Random) -> list[dict]:
        spawn_points = []
        for point in world.get("spawn_points", []):
            try:
                x = float(point.get("x"))
                y = float(point.get("y"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(x) and math.isfinite(y):
                spawn_points.append(point)
        rng.shuffle(spawn_points)
        return spawn_points

    @staticmethod
    def _initial_player_position(
        index: int,
        *,
        canvas_size: int,
        margin: float,
        rng: random.Random,
        spawn_points: list[dict],
    ) -> tuple[float, float]:
        if spawn_points:
            spawn = spawn_points[index % len(spawn_points)]
            return (
                round(clamp(float(spawn["x"]), margin, canvas_size - margin), 2),
                round(clamp(float(spawn["y"]), margin, canvas_size - margin), 2),
            )
        return (
            round(rng.uniform(margin, canvas_size - margin), 2),
            round(rng.uniform(margin, canvas_size - margin), 2),
        )

    @staticmethod
    def initial_players(participant_ids: list[int], world: dict) -> dict:
        ordered_ids = [str(p) for p in participant_ids]
        canvas_size = world["canvas_size"]
        seed = int(world.get("seed", 0)) + sum(int(p) * 7919 for p in participant_ids)
        rng = random.Random(seed)
        margin = max(PLAYER_RADIUS + 8, 40)
        spawn_points = CanvasGameState._spotter_spawn_points(world, rng)
        players = {}
        for index, participant_id in enumerate(ordered_ids):
            x, y = CanvasGameState._initial_player_position(
                index,
                canvas_size=canvas_size,
                margin=margin,
                rng=rng,
                spawn_points=spawn_points,
            )
            players[participant_id] = {
                "participant_id": participant_id,
                "label": f"Player {index + 1}",
                "color": PLAYER_COLORS[index % len(PLAYER_COLORS)],
                "x": x,
                "y": y,
                "vx": 0,
                "vy": 0,
                "client_time": 0,
                "receive_time": None,
            }
        return players

    @staticmethod
    def initial_state(participant_ids: list[int], world: dict) -> dict:
        ordered_ids = [str(p) for p in participant_ids]
        return {
            "params": {
                "participant_ids": ordered_ids,
                "world": public_world_payload(world),
                "trial_seconds": TRIAL_SECONDS,
                "send_interval_ms": SEND_INTERVAL_MS,
                "draw_interval_ms": DRAW_INTERVAL_MS,
            },
            "coins": [],
            "awarded_target_keys": [],
            "collected_coins": [],
            "bonuses": {participant_id: 0.0 for participant_id in ordered_ids},
            "collection_counts": {participant_id: 0 for participant_id in ordered_ids},
            "server_start_time": None,
        }

    @staticmethod
    def _parse_receive_time(value):
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value))
        return as_utc(parsed)

    def mark_ready(self, participant, receive_time=None) -> bool:
        started_now = super().mark_ready(participant)
        if not started_now:
            return False

        if not self.server_start_time:
            self.server_start_time = receive_time_iso(
                receive_time or datetime.now(timezone.utc)
            )
        return True

    def is_started(self) -> bool:
        return bool(self.started)

    @staticmethod
    def _distance_to_segment(px, py, ax, ay, bx, by):
        dx = bx - ax
        dy = by - ay
        if dx == 0 and dy == 0:
            return math.hypot(px - ax, py - ay)
        t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
        t = clamp(t, 0.0, 1.0)
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    @classmethod
    def _distance_to_target(cls, target: dict, x: float, y: float):
        distances = [math.hypot(float(target["x"]) - x, float(target["y"]) - y)]
        line = target.get("line") or []
        for point_a, point_b in zip(line, line[1:]):
            distances.append(
                cls._distance_to_segment(
                    x,
                    y,
                    float(point_a[0]),
                    float(point_a[1]),
                    float(point_b[0]),
                    float(point_b[1]),
                )
            )
        return min(distances)

    @staticmethod
    def public_collection_payload(collection: dict) -> dict:
        return {
            "target_id": collection["coin_id"],
            "coin_id": collection.get("public_coin_id", "coin"),
            "participant_id": collection["participant_id"],
            "bonus": collection["bonus"],
            "receive_time": collection["receive_time"],
        }

    def _server_start_time(self):
        if not self.server_start_time:
            return None
        return self._parse_receive_time(self.server_start_time)

    def record_collection(
        self,
        *,
        participant_id: int,
        coin_id: str,
        x: float,
        y: float,
        receive_time,
        client_game_time_ms: float | None = None,
    ):
        """Apply a reward collection attempt to authoritative state."""
        participant_id = str(participant_id)
        reward_targets = self.reward_targets or []
        target = next(
            (c for c in reward_targets if c["id"] == coin_id),
            None,
        )
        if target is None:
            return False, "unknown_hidden_coin", None

        server_start = self._server_start_time()
        if server_start is None:
            return False, "not_started", None
        receive_game_ms = int(
            max(
                0,
                (self._parse_receive_time(receive_time) - server_start).total_seconds()
                * 1000,
            )
        )
        game_ms = receive_game_ms
        if client_game_time_ms is not None and math.isfinite(client_game_time_ms):
            game_ms = int(max(0, client_game_time_ms))
        if (
            game_ms < int(target["start_ms"]) - COLLECTION_TIME_GRACE_MS
            or game_ms > int(target["end_ms"]) + COLLECTION_TIME_GRACE_MS
        ):
            return False, "not_available", None
        if self._distance_to_target(target, x, y) > float(
            target.get("radius", COIN_RADIUS)
        ):
            return False, "too_far", None

        award_key = f"{participant_id}:{target['id']}"
        awarded = set(self.awarded_target_keys or [])
        if award_key in awarded:
            return False, "already_awarded", None

        collection_counts = dict(self.collection_counts or {})
        collection_counts.setdefault(participant_id, 0)
        collection_counts[participant_id] += 1
        collected = {
            "coin_id": target["id"],
            "public_coin_id": f"coin-{collection_counts[participant_id]}",
            "participant_id": participant_id,
            "x": round(x, 3),
            "y": round(y, 3),
            "bonus": COIN_BONUS,
            "game_time_ms": game_ms,
            "target_start_ms": target["start_ms"],
            "target_end_ms": target["end_ms"],
            "receive_time": receive_time_iso(receive_time),
        }
        bonuses = dict(self.bonuses or {})
        bonuses.setdefault(participant_id, 0.0)
        bonuses[participant_id] = round(
            float(bonuses[participant_id]) + COIN_BONUS,
            2,
        )
        awarded.add(award_key)
        self.collection_counts = collection_counts
        self.collected_coins = [*(self.collected_coins or []), collected]
        self.bonuses = bonuses
        self.awarded_target_keys = sorted(awarded)
        return True, None, collected

    def participant_result(self, participant_id: int, raw_answer=None) -> dict:
        world = (self.params or {}).get("world", {})
        participant_id_str = str(participant_id)
        collected_coins = [
            c
            for c in self.collected_coins or []
            if str(c.get("participant_id")) == participant_id_str
        ]
        final_position = None
        if isinstance(raw_answer, dict):
            final_position = raw_answer.get("client_final_position")
        return {
            "completed_live_canvas": True,
            "participant_id": participant_id,
            "collected_coin_ids": [c["coin_id"] for c in collected_coins],
            "collected_public_coin_ids": [
                c.get("public_coin_id", c["coin_id"]) for c in collected_coins
            ],
            "coin_bonus": round(
                float((self.bonuses or {}).get(participant_id_str, 0.0)), 2
            ),
            "collection_count": int(
                (self.collection_counts or {}).get(participant_id_str, 0)
            ),
            "final_position": final_position,
            "world_id": world.get("world_id"),
            "session_label": world.get("session_label"),
        }


class PositionMessage(ClientWebSocketMessage):
    """High-frequency player position payload."""

    event_type: ClassVar[str] = POSITION_EVENT
    save: ClassVar[bool] = True
    x: float = Field(ge=0, le=CANVAS_SIZE)
    y: float = Field(ge=0, le=CANVAS_SIZE)
    vx: float
    vy: float
    client_time: float
    game_time_ms: float = Field(default=0.0, ge=0)

    def player_payload(self, participant: Participant, receive_time):
        participant_id = str(participant.id)
        return {
            "participant_id": participant_id,
            "x": round(clamp(self.x, 0, CANVAS_SIZE), 3),
            "y": round(clamp(self.y, 0, CANVAS_SIZE), 3),
            "vx": round(self.vx, 3),
            "vy": round(self.vy, 3),
            "client_time": self.client_time,
            "receive_time": receive_time_iso(receive_time),
        }

    @session()
    def handle(
        self,
        experiment,
        participant,
        session: CanvasGameState,
        receive_time,
    ):
        """Broadcast this high-frequency position update."""

        PositionUpdateMessage(
            player=self.player_payload(participant, receive_time),
        ).send(session.participants)


class CollectMessage(ClientWebSocketMessage):
    """Reward collection attempt payload."""

    event_type: ClassVar[str] = COLLECT_EVENT
    save: ClassVar[bool] = True
    coin_id: str = Field(min_length=1)
    x: float = Field(ge=0, le=CANVAS_SIZE)
    y: float = Field(ge=0, le=CANVAS_SIZE)
    client_time: float
    game_time_ms: float | None = Field(default=None, ge=0)

    @session(mutate=True, logging=True)
    def handle(
        self,
        experiment,
        participant,
        session: CanvasGameState,
        receive_time,
    ):
        """Apply this reward collection attempt to authoritative state.

        Uses session.reward_targets only; live handlers must not load world assets.
        """

        accepted, reason, collection = session.record_collection(
            participant_id=participant.id,
            coin_id=self.coin_id,
            x=self.x,
            y=self.y,
            receive_time=receive_time,
            client_game_time_ms=self.game_time_ms,
        )
        if accepted:
            CoinCollectedMessage(
                collection=CanvasGameState.public_collection_payload(collection),
                coins=session.coins or [],
                bonuses=session.bonuses or {},
            ).send(session.participants)
            session.send_snapshot()
        else:
            CollectRejectedMessage(
                coin_id=self.coin_id,
                reason=reason or "unknown",
            ).send(participant)


class PositionUpdateMessage(ServerWebSocketMessage):
    """Broadcast player position update."""

    event_type: ClassVar[str] = "position_update"
    save: ClassVar[bool] = False
    player: dict


class CoinCollectedMessage(ServerWebSocketMessage):
    """Broadcast an accepted reward collection."""

    event_type: ClassVar[str] = "coin_collected"
    save: ClassVar[bool] = True
    collection: dict
    coins: list[dict]
    bonuses: dict


class CollectRejectedMessage(ServerWebSocketMessage):
    """Notify a participant that a collection attempt was rejected."""

    event_type: ClassVar[str] = "collect_rejected"
    save: ClassVar[bool] = False
    coin_id: str
    reason: str


class LobbyPositionMessage(ClientWebSocketMessage):
    """Unsaved waiter pose for the pre-group 3D lobby."""

    event_type: ClassVar[str] = LOBBY_POSITION_EVENT
    save: ClassVar[bool] = False
    x: float = Field(ge=0, le=CANVAS_SIZE)
    y: float = Field(ge=0, le=CANVAS_SIZE)
    vx: float
    vy: float
    client_time: float

    def player_payload(self, participant: Participant, receive_time):
        spawn = lobby_spawn_for_participant(int(participant.id))
        return {
            "participant_id": str(participant.id),
            "label": spawn["label"],
            "color": spawn["color"],
            "x": round(clamp(self.x, 0, CANVAS_SIZE), 3),
            "y": round(clamp(self.y, 0, CANVAS_SIZE), 3),
            "vx": round(self.vx, 3),
            "vy": round(self.vy, 3),
            "client_time": self.client_time,
            "receive_time": receive_time_iso(receive_time),
        }

    def handle(self, experiment, participant, receive_time):
        """Broadcast this pose to whoever is currently waiting at the grouper."""

        waiters = Barrier.get_waiting_participants_from_barrier_id(GROUPER_ID)
        waiter_ids = [str(waiter.id) for waiter in waiters]
        if str(participant.id) not in waiter_ids:
            return None
        LobbyPositionUpdateMessage(
            player=self.player_payload(participant, receive_time),
            waiter_ids=waiter_ids,
        ).send(waiters)
        return None


class LobbyPositionUpdateMessage(ServerWebSocketMessage):
    """Broadcast a lobby pose to current SimpleGrouper waiters."""

    event_type: ClassVar[str] = "lobby_position_update"
    save: ClassVar[bool] = False
    player: dict
    waiter_ids: list[str]


def lobby_page(participant: Participant):
    return ModularPage(
        LOBBY_PAGE_LABEL,
        "",
        LobbyControl(participant),
        save_answer=False,
        time_estimate=5,
        session_id="canvas_lobby",
        show_next_button=False,
    )


def advance_past_lobby_pages(bots: List[BotDriver], max_iterations=20):
    iteration = 0
    while True:
        iteration += 1
        any_waiting = False
        for bot in bots:
            current_page = bot.get_current_page()
            if getattr(current_page, "label", None) == LOBBY_PAGE_LABEL:
                any_waiting = True
                bot.take_page()
        if not any_waiting:
            break
        if iteration >= max_iterations:
            raise RuntimeError("Not all bots finished lobby waiting in time.")


def instruction_page():
    content = tags.div()
    with content:
        tags.h2("Shared canvas navigation")
        tags.p(
            "You will drive a car across a live storm map with other "
            "participants. Use the arrow keys or WASD to accelerate, brake, "
            "and steer."
        )
        tags.p(
            "A map in the upper right shows the wider area around your car. "
            "Use it to spot storm columns, warning zones, and other drivers."
        )
        tags.p(
            "Coins appear in places that are worth exploring. Drive through "
            "promising areas at the right time. Each coin you find adds $0.10 "
            "to your bonus."
        )
    return InfoPage(content, time_estimate=20)


def build_bot_answer(bot) -> dict:
    return {
        "completed_live_canvas_browser": True,
        "bot_participant_id": bot.id,
        "note": "PsyNet bot path bypasses browser websocket canvas interaction.",
    }


class SharedCanvasAssets:
    """PsyNet 14 page assets for the shared canvas lobby and game controls."""

    def get_css_links(self):
        return ["/static/css/shared_canvas.css"]

    def get_js_dependencies(self):
        return ["/static/js/three.min.js", "/static/js/shared_canvas_3d.js"]

    def get_js_page_modules(self):
        return ["/static/js/shared_canvas_page.js"]

    def get_js_vars(self):
        return {"canvas_config": self.canvas_config}


class LobbyControl(SharedCanvasAssets, Control):
    """Pre-group 3D wait room with no LiveSession or saved answers."""

    external_template = "shared_canvas.html"
    macro = "shared_canvas_control"

    def __init__(self, participant):
        self.participant = participant
        super().__init__(show_next_button=False)

    @property
    def canvas_config(self):
        participant_id = int(self.participant.id)
        initial_player = lobby_spawn_for_participant(participant_id)
        return {
            "mode": "lobby",
            "role": initial_player["label"],
            "participant_id": participant_id,
            "canvas_size": CANVAS_SIZE,
            "canvas_width": CANVAS_RENDER_WIDTH,
            "canvas_height": CANVAS_RENDER_HEIGHT,
            "send_interval_ms": SEND_INTERVAL_MS,
            "player_radius": PLAYER_RADIUS,
            "initial_players": {str(participant_id): initial_player},
            "initial_player": initial_player,
            "max_player_speed": LOBBY_MAX_PLAYER_SPEED,
            "trial_seconds": 180,
        }

    def get_bot_response(self, experiment, bot, page, prompt):
        return None


class SharedCanvasControl(SharedCanvasAssets, LiveSessionControl):
    """Custom canvas renderer wrapped in PsyNet's modular page API."""

    external_template = "shared_canvas.html"
    macro = "shared_canvas_control"

    def __init__(self, trial, participant):
        self.trial = trial
        super().__init__(
            participant=participant,
            session_class=CanvasGameState,
            group_type=GROUP_TYPE,
            session_initializer_id="shared_canvas_session",
            show_next_button=False,
        )

    @property
    def canvas_config(self):
        world = self.trial.definition
        participants = sorted(self._get_group().active_participants, key=lambda p: p.id)
        participant_ids = [int(participant.id) for participant in participants]
        if not participant_ids:
            participant_ids = [int(self.participant.id)]
        initial_players = CanvasGameState.initial_players(participant_ids, world)
        participant_id = int(self.participant.id)
        role_index = participant_ids.index(participant_id)
        return {
            "role": f"Player {role_index + 1}",
            "participant_id": participant_id,
            "world_id": world["world_id"],
            "world_url": self.trial.assets["world"].url,
            "canvas_size": world["canvas_size"],
            "canvas_width": CANVAS_RENDER_WIDTH,
            "canvas_height": CANVAS_RENDER_HEIGHT,
            "trial_seconds": TRIAL_SECONDS,
            "send_interval_ms": SEND_INTERVAL_MS,
            "draw_interval_ms": DRAW_INTERVAL_MS,
            "player_radius": PLAYER_RADIUS,
            "initial_players": initial_players,
            "initial_player": initial_players[str(participant_id)],
            "coin_radius": world["coin_radius"],
            "coin_bonus": COIN_BONUS,
            "max_player_speed": world["max_player_speed"],
            "speed_limit_mph": world.get("speed_limit_mph", 30),
            "projection": world.get("projection", {}),
            "storm_chaser_track_count": STORM_CHASER_TRACK_COUNT,
            "chaser_track_visible_ms": CHASER_TRACK_VISIBLE_MS,
            "timing": {
                "game_start_ms": world.get("game_start_ms", 0),
                "game_end_ms": world.get("game_end_ms", TRIAL_SECONDS * 1000),
                "raw_time_min_ms": world.get("raw_time_min_ms"),
                "raw_time_max_ms": world.get("raw_time_max_ms"),
            },
        }

    def format_answer(self, raw_answer, **kwargs):
        return raw_answer

    def get_bot_response(self, experiment, bot, page, prompt):
        return build_bot_answer(bot)


class SharedCanvasTrial(StaticTrial):
    time_estimate = TRIAL_SECONDS + 35

    def show_trial(self, experiment, participant):
        return join(
            instruction_page(),
            LiveSessionInitializer(
                id_="shared_canvas_session",
                group_type=GROUP_TYPE,
                session_class=CanvasGameState,
                waiting_logic=WaitPage(wait_time=0.5, save_answer=False),
                max_wait_time=90,
            ),
            self.play_canvas(participant),
            GroupBarrier(
                id_="canvas_finished",
                group_type=GROUP_TYPE,
                waiting_logic=WaitPage(wait_time=0.5, save_answer=False),
                on_release=self.score_canvas_game,
                max_wait_time=90,
            ),
        )

    def play_canvas(self, participant):
        prompt = tags.div()
        with prompt:
            tags.p(
                "Drive with the arrow keys or WASD. Use the upper-right map "
                "to find coins before the session ends."
            )
        return ModularPage(
            "shared_canvas",
            prompt,
            SharedCanvasControl(self, participant),
            save_answer="shared_canvas_browser_answer",
            time_estimate=TRIAL_SECONDS + 5,
            requires_full_page_reload=True,
        )

    def score_canvas_game(self, participants: List[Participant], **kwargs):
        group = participants[0].active_sync_groups[GROUP_TYPE]
        node_id, network_id = CanvasGameState._current_trial_details(group)
        game_state = CanvasGameState.get_for_group(
            group=group,
            initializer_id="shared_canvas_session",
            node_id=node_id,
            network_id=network_id,
        )
        if game_state is None:
            return
        for participant in participants:
            try:
                raw_answer = participant.var.shared_canvas_browser_answer
            except AttributeError:
                raw_answer = None
            participant.var.shared_canvas_result = game_state.participant_result(
                participant.id,
                raw_answer=raw_answer,
            )

    def format_answer(self, raw_answer, **kwargs):
        participant = kwargs.get("participant", self.participant)
        if participant is not None:
            try:
                result = participant.var.shared_canvas_result
            except AttributeError:
                result = None
            if isinstance(result, dict):
                return result
        return {
            "completed_live_canvas": False,
            "world_id": self.definition["world_id"],
            "coin_bonus": 0.0,
            "raw_answer": raw_answer,
        }

    def score_answer(self, answer, definition):
        if isinstance(answer, dict):
            return int(round(float(answer.get("coin_bonus", 0.0)) / COIN_BONUS))
        return 0

    def compute_performance_reward(self, score):
        return max(0.0, score * COIN_BONUS)

    def show_feedback(self, experiment, participant):
        try:
            answer = participant.var.shared_canvas_result
        except AttributeError:
            answer = self.answer if isinstance(self.answer, dict) else {}
        bonus = float(answer.get("coin_bonus", 0.0))
        content = tags.div()
        with content:
            tags.h2("Navigation complete")
            tags.p(f"Your coin bonus is ${bonus:.2f}.")
            tags.p("Thank you for exploring the shared map.")
        return InfoPage(content, time_estimate=5)


class Exp(psynet.experiment.Experiment):
    label = "Real-time shared canvas navigation"
    variables_initial_values = {
        "group_size": GROUP_SIZE,
        "canvas_size": CANVAS_SIZE,
        "trial_seconds": TRIAL_SECONDS,
        "send_interval_ms": SEND_INTERVAL_MS,
        "draw_interval_ms": DRAW_INTERVAL_MS,
        "coin_bonus": COIN_BONUS,
        "storm_chaser_track_count": STORM_CHASER_TRACK_COUNT,
        "chaser_track_visible_ms": CHASER_TRACK_VISIBLE_MS,
        "soft_max_experiment_payment": 1000.0,
        "hard_max_experiment_payment": 1100.0,
        "max_participant_payment": 25.0,
        "soft_max_experiment_payment_email_sent": False,
        "hard_max_experiment_payment_email_sent": False,
    }

    timeline = Timeline(
        NoConsent(),
        SimpleGrouper(
            group_type=GROUP_TYPE,
            initial_group_size=GROUP_SIZE,
            batch_size=GROUP_SIZE,
            waiting_logic=PageMaker(lobby_page, time_estimate=5),
            waiting_logic_expected_repetitions=1,
            max_wait_time=180,
        ),
        StaticTrialMaker(
            id_="shared_canvas_worlds",
            trial_class=SharedCanvasTrial,
            nodes=get_world_nodes,
            expected_trials_per_participant=1,
            max_trials_per_participant=1,
            sync_group_type=GROUP_TYPE,
            check_performance_at_end=False,
        ),
    )

    test_n_bots = 4
    test_mode = "serial"

    @experiment_route("/lobby/waiting", methods=["GET"])
    @classmethod
    @with_transaction
    def lobby_waiting(cls):
        unique_id = request.args.get("unique_id")
        if not unique_id:
            return success_response(waiting=True, waiter_count=0)
        try:
            participant = cls.get_participant_from_unique_id(
                unique_id, for_update=False
            )
        except Exception:
            return success_response(waiting=True, waiter_count=0)
        waiting = GROUPER_ID in participant.active_barriers
        waiters = Barrier.get_waiting_participants_from_barrier_id(GROUPER_ID)
        return success_response(waiting=waiting, waiter_count=len(waiters))

    def test_serial_run_bots(self, bots: List[BotDriver]):
        advance_past_lobby_pages(bots)
        advance_past_wait_pages(bots)

        for bot in bots:
            assert "Shared canvas navigation" in bot.current_page_text
            assert "Each coin you find adds $0.10" in bot.current_page_text
            bot.take_page()

        advance_past_wait_pages(bots)

        for bot in bots:
            assert bot.current_page_label == "shared_canvas"
            bot.take_page(response=build_bot_answer(bot))

        advance_past_wait_pages(bots)

        answers_by_group = {}
        for bot in bots:
            assert "Navigation complete" in bot.current_page_text
            answer = bot.current_trial.answer
            assert isinstance(answer, dict)
            assert answer["completed_live_canvas"] is True
            assert answer["coin_bonus"] == 0.0
            assert answer["collected_coin_ids"] == []
            assert answer["collected_public_coin_ids"] == []
            assert answer["participant_id"] == bot.id
            participant = Participant.query.get(bot.id)
            group_id = int(participant.active_sync_groups[GROUP_TYPE].id)
            answers_by_group.setdefault(group_id, []).append(answer)

        assert len(answers_by_group) == len(bots) // GROUP_SIZE
        assert all(
            len(group_answers) == GROUP_SIZE
            for group_answers in answers_by_group.values()
        )
