from __future__ import annotations

import math
import os
import random
from copy import deepcopy
from datetime import datetime, timezone
from typing import List

from dallinger import db
from dominate import tags
from pydantic import Field
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
)

import psynet.experiment
from psynet.bot import BotDriver, advance_past_wait_pages
from psynet.data import SQLBase, SQLMixin, register_table
from psynet.modular_page import ModularPage
from psynet.page import InfoPage, WaitPage
from psynet.participant import Participant
from psynet.session import LiveSession, LiveSessionControl
from psynet.sync import GroupBarrier, SimpleGrouper
from psynet.timeline import PageMaker, Timeline, join
from psynet.trial.static import StaticNode, StaticTrial, StaticTrialMaker
from psynet.websocket import WebSocketMessage, websocket_handler

from psynet.consent import NoConsent

if __package__:
    from .session_worlds import (
        load_world_definitions,
        public_world_payload,
    )
else:
    from session_worlds import (
        load_world_definitions,
        public_world_payload,
    )


GROUP_TYPE = "shared_canvas_group"
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


WORLD_DEFINITIONS = load_world_definitions(
    static_root=STATIC_SESSION_ROOT,
    canvas_size=CANVAS_SIZE,
    trial_seconds=TRIAL_SECONDS,
    coin_radius=COIN_RADIUS,
    coin_bonus=COIN_BONUS,
)


def receive_time_iso(receive_time: datetime):
    return receive_time.isoformat()


@register_table
class CanvasPositionEvent(SQLBase, SQLMixin):
    """Persisted high-frequency position event.

    Position events are recorded for analysis and replay, but they do not mutate
    the authoritative ``CanvasGameState``.
    """

    __tablename__ = "canvas_position_event"

    session_id = Column(String(128), index=True)
    participant_id = Column(Integer, index=True, nullable=True)
    x = Column(Float)
    y = Column(Float)
    vx = Column(Float)
    vy = Column(Float)
    client_time = Column(Float)
    receive_time = Column(DateTime(timezone=True), nullable=False)


@register_table
class CanvasCollectEvent(SQLBase, SQLMixin):
    """Persisted coin/reward collection event."""

    __tablename__ = "canvas_collect_event"

    session_id = Column(String(128), index=True)
    participant_id = Column(Integer, index=True, nullable=True)
    coin_id = Column(String(128), index=True)
    x = Column(Float)
    y = Column(Float)
    client_time = Column(Float)
    accepted = Column(Boolean, nullable=True, index=True)
    rejection_reason = Column(String(128), nullable=True)
    receive_time = Column(DateTime(timezone=True), nullable=False)


class CanvasGameState(LiveSession):
    """Authoritative live session for one canvas game."""

    @classmethod
    def build_initial_state(cls, participant_ids, group, trial):
        """Return public resumable state for a synchronized canvas group."""

        return cls.initial_state(participant_ids, trial.definition["world"])

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
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def mark_ready(self, participant, receive_time=None) -> bool:
        started_now = super().mark_ready(participant)
        if not started_now:
            return False

        state = deepcopy(self.state or {})
        if not state.get("server_start_time"):
            state["server_start_time"] = receive_time_iso(
                receive_time or datetime.now(timezone.utc)
            )
        self.state = state
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

    def _server_start_time(self, state: dict):
        if not state.get("server_start_time"):
            return None
        return self._parse_receive_time(state["server_start_time"])

    def record_collection(
        self,
        *,
        participant_id: int,
        coin_id: str,
        x: float,
        y: float,
        receive_time,
        reward_targets: list[dict],
        client_game_time_ms: float | None = None,
    ):
        """Apply a reward collection attempt to authoritative state."""
        state = deepcopy(self.state or {})
        participant_id = str(participant_id)
        target = next(
            (c for c in reward_targets if c["id"] == coin_id),
            None,
        )
        if target is None:
            return False, "unknown_hidden_coin", None

        server_start = self._server_start_time(state)
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
        if self._distance_to_target(target, x, y) > float(target.get("radius", COIN_RADIUS)):
            return False, "too_far", None

        award_key = f"{participant_id}:{target['id']}"
        awarded = set(state.setdefault("awarded_target_keys", []))
        if award_key in awarded:
            return False, "already_awarded", None

        state.setdefault("collection_counts", {}).setdefault(participant_id, 0)
        state["collection_counts"][participant_id] += 1
        collected = {
            "coin_id": target["id"],
            "public_coin_id": f"coin-{state['collection_counts'][participant_id]}",
            "participant_id": participant_id,
            "x": round(x, 3),
            "y": round(y, 3),
            "bonus": COIN_BONUS,
            "game_time_ms": game_ms,
            "target_start_ms": target["start_ms"],
            "target_end_ms": target["end_ms"],
            "receive_time": receive_time_iso(receive_time),
        }
        state.setdefault("collected_coins", []).append(collected)
        state.setdefault("bonuses", {}).setdefault(participant_id, 0.0)
        state["bonuses"][participant_id] = round(
            float(state["bonuses"][participant_id]) + COIN_BONUS,
            2,
        )
        awarded.add(award_key)
        state["awarded_target_keys"] = sorted(awarded)
        self.state = state
        return True, None, collected

    def participant_result(self, participant_id: int) -> dict:
        state = self.state or {}
        world = state.get("params", {}).get("world", {})
        participant_id_str = str(participant_id)
        collected_coins = [
            c
            for c in state.get("collected_coins", [])
            if str(c.get("participant_id")) == participant_id_str
        ]
        latest_position = (
            CanvasPositionEvent.query.filter_by(
                session_id=self.session_id,
                participant_id=participant_id,
            )
            .order_by(CanvasPositionEvent.id.desc())
            .first()
        )
        final_position = None
        if latest_position is not None:
            final_position = {
                "x": latest_position.x,
                "y": latest_position.y,
                "vx": latest_position.vx,
                "vy": latest_position.vy,
            }
        return {
            "completed_live_canvas": True,
            "participant_id": participant_id,
            "collected_coin_ids": [c["coin_id"] for c in collected_coins],
            "collected_public_coin_ids": [
                c.get("public_coin_id", c["coin_id"]) for c in collected_coins
            ],
            "coin_bonus": round(
                float(state.get("bonuses", {}).get(participant_id_str, 0.0)), 2
            ),
            "collection_count": int(
                state.get("collection_counts", {}).get(participant_id_str, 0)
            ),
            "final_position": final_position,
            "world_id": world.get("world_id"),
            "session_label": world.get("session_label"),
        }


class PositionMessage(WebSocketMessage):
    """High-frequency player position payload."""

    session_id: str = Field(min_length=1)
    x: float = Field(ge=0, le=CANVAS_SIZE)
    y: float = Field(ge=0, le=CANVAS_SIZE)
    vx: float
    vy: float
    client_time: float
    game_time_ms: float = Field(default=0.0, ge=0)
    low_latency: bool = True

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


class CollectMessage(WebSocketMessage):
    """Reward collection attempt payload."""

    session_id: str = Field(min_length=1)
    coin_id: str = Field(min_length=1)
    x: float = Field(ge=0, le=CANVAS_SIZE)
    y: float = Field(ge=0, le=CANVAS_SIZE)
    client_time: float
    game_time_ms: float | None = Field(default=None, ge=0)


def waiting_page(participant: Participant):
    active_barrier = participant.active_barriers.get("canvas_grouper", None)
    if active_barrier:
        waiting = active_barrier.get_waiting_participants()
        content = (
            "Waiting for the shared canvas group. "
            f"{len(waiting)} participant(s) are currently ready."
        )
    else:
        content = "Preparing the shared canvas."
    return WaitPage(content=content, wait_time=2.5)


def instruction_page():
    content = tags.div()
    with content:
        tags.h2("Shared canvas navigation")
        tags.p(
            "You will enter a square canvas with other live participants. "
            "Use the arrow keys to move your avatar."
        )
        tags.p(
            "Your movement has a little inertia: when you release a key, your "
            "avatar slows down smoothly instead of stopping immediately."
        )
        tags.p(
            "Coins appear in places that are worth exploring. Colored potential "
            "markers and shaded zones can help you decide where to go."
        )
        tags.p(
            "Move through promising areas at the right time. Each coin you find "
            "adds $0.10 to your bonus."
        )
    return InfoPage(content, time_estimate=20)


def build_bot_answer(bot) -> dict:
    return {
        "completed_live_canvas_browser": True,
        "bot_participant_id": bot.id,
        "note": "PsyNet bot path bypasses browser websocket canvas interaction.",
    }


class SharedCanvasControl(LiveSessionControl):
    """Custom canvas renderer wrapped in PsyNet's modular page API."""

    external_template = "shared_canvas.html"
    macro = "shared_canvas_control"

    def __init__(self, trial, participant):
        super().__init__(
            participant=participant,
            group_type=GROUP_TYPE,
            trial=trial,
            show_next_button=False,
        )

    def build_control_params(self):
        world = self.trial.definition["world"]
        initial_players = CanvasGameState.initial_players(self.participant_ids, world)
        role_index = self.participant_ids.index(self.participant_id)
        return {
            "role": f"Player {role_index + 1}",
            "world_id": world["world_id"],
            "canvas_size": world["canvas_size"],
            "canvas_width": CANVAS_RENDER_WIDTH,
            "canvas_height": CANVAS_RENDER_HEIGHT,
            "trial_seconds": TRIAL_SECONDS,
            "send_interval_ms": SEND_INTERVAL_MS,
            "draw_interval_ms": DRAW_INTERVAL_MS,
            "player_radius": PLAYER_RADIUS,
            "initial_players": initial_players,
            "initial_player": initial_players[str(self.participant_id)],
            "coin_radius": world["coin_radius"],
            "coin_bonus": COIN_BONUS,
            "max_player_speed": world["max_player_speed"],
            "speed_limit_mph": world.get("speed_limit_mph", 30),
            "projection": world.get("projection", {}),
            "storm_points": world.get("storm_points", []),
            "chaser_tracks": world.get("chaser_tracks", [])[:STORM_CHASER_TRACK_COUNT],
            "storm_chaser_track_count": STORM_CHASER_TRACK_COUNT,
            "chaser_track_visible_ms": CHASER_TRACK_VISIBLE_MS,
            "warnings": world.get("warnings", []),
            "reward_events": world.get("reward_events", []),
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
    live_session_class = CanvasGameState
    time_estimate = TRIAL_SECONDS + 35

    def show_trial(self, experiment, participant):
        return join(
            instruction_page(),
            GroupBarrier(
                id_="canvas_start",
                group_type=GROUP_TYPE,
                max_wait_time=90,
            ),
            self.play_canvas(participant),
            GroupBarrier(
                id_="canvas_finished",
                group_type=GROUP_TYPE,
                on_release=self.score_canvas_game,
                max_wait_time=90,
            ),
        )

    def play_canvas(self, participant):
        prompt = tags.div()
        with prompt:
            tags.p(
                "Use the arrow keys to move. Watch the changing potential "
                "markers and shaded zones to find coins before the session ends."
            )
        return ModularPage(
            "shared_canvas",
            prompt,
            SharedCanvasControl(self, participant),
            save_answer="shared_canvas_browser_answer",
            time_estimate=TRIAL_SECONDS + 5,
        )

    def score_canvas_game(self, participants: List[Participant]):
        game_state = self.live_session
        if game_state is None:
            group = participants[0].active_sync_groups[GROUP_TYPE]
            game_state = CanvasGameState.get(
                CanvasGameState.build_session_id(group, self)
            )
        if game_state is None:
            return
        for participant in participants:
            participant.var.shared_canvas_result = game_state.participant_result(
                participant.id
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
            "world_id": self.definition["world"]["world_id"],
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
            tags.p("Thank you for exploring the shared canvas.")
        return InfoPage(content, time_estimate=5)


class WorldNode(StaticNode):
    def create_definition_from_seed(self, seed, experiment, participant):
        return self.definition


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
            waiting_logic=PageMaker(waiting_page, time_estimate=5),
            max_wait_time=180,
        ),
        StaticTrialMaker(
            id_="shared_canvas_worlds",
            trial_class=SharedCanvasTrial,
            nodes=[WorldNode(definition={"world": world}) for world in WORLD_DEFINITIONS],
            expected_trials_per_participant=1,
            max_trials_per_participant=1,
            sync_group_type=GROUP_TYPE,
            check_performance_at_end=False,
        ),
    )

    test_n_bots = 4
    test_mode = "serial"

    @websocket_handler(POSITION_EVENT, model=PositionMessage)
    def position(self, participant, message: PositionMessage, receive_time):
        """Persist and broadcast a high-frequency position update."""

        live_session = CanvasGameState.get_current_for_participant(
            participant, message.session_id
        )
        if live_session is None:
            return

        logged_event = CanvasPositionEvent(
            session_id=live_session.session_id,
            participant_id=participant.id,
            x=message.x,
            y=message.y,
            vx=message.vx,
            vy=message.vy,
            client_time=message.client_time,
            receive_time=receive_time,
        )
        db.session.add(logged_event)
        db.session.flush()
        self.websocket.send(
            live_session.participant_ids,
            "position_update",
            {
                "event_id": logged_event.id,
                "player": message.player_payload(participant, receive_time),
            },
        )
        db.session.commit()

    @websocket_handler(COLLECT_EVENT, model=CollectMessage)
    def collect(self, participant, message: CollectMessage, receive_time):
        """Apply a reward collection attempt to authoritative state."""

        live_session = CanvasGameState.get_current_for_participant(
            participant, message.session_id, for_update=True
        )
        if live_session is None:
            return

        trial = live_session.get_participant_trial(participant)
        if trial is None:
            return
        reward_targets = trial.definition["world"].get("reward_targets", [])
        accepted, reason, collection = live_session.record_collection(
            participant_id=participant.id,
            coin_id=message.coin_id,
            x=message.x,
            y=message.y,
            receive_time=receive_time,
            reward_targets=reward_targets,
            client_game_time_ms=message.game_time_ms,
        )
        db.session.add(
            CanvasCollectEvent(
                session_id=live_session.session_id,
                participant_id=participant.id,
                coin_id=message.coin_id,
                x=message.x,
                y=message.y,
                client_time=message.client_time,
                accepted=accepted,
                rejection_reason=reason,
                receive_time=receive_time,
            )
        )
        if accepted:
            state = live_session.state or {}
            self.websocket.send(
                live_session.participant_ids,
                "coin_collected",
                {
                    "collection": CanvasGameState.public_collection_payload(collection),
                    "coins": state.get("coins", []),
                    "bonuses": state.get("bonuses", {}),
                },
            )
            live_session.send_snapshot(self)
        else:
            self.websocket.send(
                participant,
                "collect_rejected",
                {
                    "coin_id": message.coin_id,
                    "reason": reason,
                },
            )
        db.session.commit()

    def test_serial_run_bots(self, bots: List[BotDriver]):
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
        assert all(len(group_answers) == GROUP_SIZE for group_answers in answers_by_group.values())