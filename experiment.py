from __future__ import annotations

import json
import math
import os
import random
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from typing import List, Literal, Optional

from dallinger import db
from dominate import tags
from pydantic import Field, ValidationError
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)

import psynet.experiment
from psynet.bot import BotDriver, advance_past_wait_pages
from psynet.data import SQLBase, SQLMixin, register_table
from psynet.modular_page import Control, ModularPage
from psynet.page import InfoPage, WaitPage
from psynet.participant import Participant
from psynet.sync import GroupBarrier, SimpleGrouper
from psynet.timeline import NullElt, PageMaker, Timeline, join
from psynet.trial.static import StaticNode, StaticTrial, StaticTrialMaker

from psynet.consent import NoConsent

if __package__:
    from .websocket_protocol import (
        ClientWebSocketEvent,
        ServerWebSocketEvent,
        ValidatedWebSocketElt,
        WebSocketEventService,
        websocket_handler,
    )
else:
    from websocket_protocol import (
        ClientWebSocketEvent,
        ServerWebSocketEvent,
        ValidatedWebSocketElt,
        WebSocketEventService,
        websocket_handler,
    )


GROUP_TYPE = "shared_canvas_group"
CANVAS_WS_CHANNEL = "shared_canvas_live"
CANVAS_WS_IMMEDIATE = True
CANVAS_WS_TOLERANCE = 0.005
GROUP_SIZE = max(2, int(os.environ.get("CANVAS_GROUP_SIZE", "2")))
CANVAS_SIZE = 640
TRIAL_SECONDS = int(os.environ.get("CANVAS_TRIAL_SECONDS", "35"))
SEND_INTERVAL_MS = 50
DRAW_INTERVAL_MS = 25
PLAYER_RADIUS = 12
COIN_RADIUS = 10
COIN_BONUS = 0.10
COINS_PER_WORLD = 8
N_WORLDS = 3
POSITION_EVENT = "position"
COLLECT_EVENT = "collect"
STATE_REQUEST_EVENT = "state_request"

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


def generate_world(world_index: int) -> dict:
    seed = 20260706 + world_index * 101
    rng = random.Random(seed)
    margin = 60
    coins = []
    for coin_index in range(COINS_PER_WORLD):
        coins.append(
            {
                "id": f"world-{world_index}-coin-{coin_index + 1}",
                "x": round(rng.uniform(margin, CANVAS_SIZE - margin), 1),
                "y": round(rng.uniform(margin, CANVAS_SIZE - margin), 1),
                "radius": COIN_RADIUS,
            }
        )
    return {
        "world_id": f"world-{world_index}",
        "seed": seed,
        "canvas_size": CANVAS_SIZE,
        "player_radius": PLAYER_RADIUS,
        "coin_radius": COIN_RADIUS,
        "coin_bonus": COIN_BONUS,
        "coins": coins,
    }


WORLD_DEFINITIONS = [generate_world(i + 1) for i in range(N_WORLDS)]


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
    """Persisted coin-collection attempt."""

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


@register_table
class CanvasStateRequestEvent(SQLBase, SQLMixin):
    """Persisted reload/resume state request event."""

    __tablename__ = "canvas_state_request_event"

    session_id = Column(String(128), index=True)
    participant_id = Column(Integer, index=True, nullable=True)
    receive_time = Column(DateTime(timezone=True), nullable=False)


@register_table
class CanvasGameState(SQLBase, SQLMixin):
    """Authoritative shared state for one canvas game session."""

    __tablename__ = "canvas_game_state"
    __table_args__ = (UniqueConstraint("session_id"),)

    session_id = Column(String(128), index=True)
    group_id = Column(Integer, index=True)
    network_id = Column(Integer, index=True)
    world_id = Column(String(64), index=True)
    state = Column(JSON)

    @classmethod
    def get_or_create(cls, session_id: str, *, defaults=None, for_update=False):
        query = cls.query.filter_by(session_id=session_id)
        if for_update:
            query = query.with_for_update(of=cls)
        session = query.one_or_none()
        if session is None:
            session = cls(session_id=session_id, **(defaults or {}))
            db.session.add(session)
            db.session.flush()
        return session

    @staticmethod
    def initial_state(participant_ids: list[int], world: dict) -> dict:
        ordered_ids = [str(p) for p in participant_ids]
        canvas_size = world["canvas_size"]
        players = {}
        for index, participant_id in enumerate(ordered_ids):
            angle = (2 * math.pi * index) / max(1, len(ordered_ids))
            players[participant_id] = {
                "participant_id": participant_id,
                "label": f"Player {index + 1}",
                "color": PLAYER_COLORS[index % len(PLAYER_COLORS)],
                "x": round(canvas_size / 2 + math.cos(angle) * 70, 2),
                "y": round(canvas_size / 2 + math.sin(angle) * 70, 2),
                "vx": 0,
                "vy": 0,
                "client_time": 0,
                "receive_time": None,
            }

        return {
            "params": {
                "participant_ids": ordered_ids,
                "world": deepcopy(world),
                "trial_seconds": TRIAL_SECONDS,
                "send_interval_ms": SEND_INTERVAL_MS,
                "draw_interval_ms": DRAW_INTERVAL_MS,
            },
            "players": players,
            "coins": deepcopy(world["coins"]),
            "collected_coins": [],
            "bonuses": {participant_id: 0.0 for participant_id in ordered_ids},
            "collection_counts": {participant_id: 0 for participant_id in ordered_ids},
        }

    @property
    def participant_ids(self) -> list[int]:
        state = self.state or {}
        return [int(p) for p in state.get("params", {}).get("participant_ids", [])]

    def record_collection(
        self,
        *,
        participant_id: int,
        coin_id: str,
        x: float,
        y: float,
        receive_time,
    ):
        """Apply a validated collection attempt to authoritative state."""
        state = deepcopy(self.state or {})
        participant_id = str(participant_id)
        coin = next((c for c in state.get("coins", []) if c["id"] == coin_id), None)
        if coin is None:
            return False, "already_collected_or_unknown", None

        player = state.get("players", {}).get(participant_id)
        if player is None:
            return False, "unknown_player", None

        distance = math.hypot(float(coin["x"]) - x, float(coin["y"]) - y)
        collect_radius = float(coin.get("radius", COIN_RADIUS)) + PLAYER_RADIUS + 4
        if distance > collect_radius:
            return False, "too_far", None

        state["coins"] = [c for c in state.get("coins", []) if c["id"] != coin_id]
        collected = {
            "coin_id": coin_id,
            "participant_id": participant_id,
            "x": coin["x"],
            "y": coin["y"],
            "bonus": COIN_BONUS,
            "receive_time": receive_time_iso(receive_time),
        }
        state.setdefault("collected_coins", []).append(collected)
        state.setdefault("collection_counts", {}).setdefault(participant_id, 0)
        state["collection_counts"][participant_id] += 1
        state.setdefault("bonuses", {}).setdefault(participant_id, 0.0)
        state["bonuses"][participant_id] = round(
            float(state["bonuses"][participant_id]) + COIN_BONUS,
            2,
        )
        self.state = state
        return True, None, collected

    def state_snapshot_payload(self, participant_id: int) -> dict:
        state = self.state or {}
        return {
            "session_id": self.session_id,
            "group_id": self.group_id,
            "network_id": self.network_id,
            "world_id": self.world_id,
            "target_participant_ids": [str(p) for p in self.participant_ids],
            "players": state.get("players", {}),
            "coins": state.get("coins", []),
            "collected_coins": state.get("collected_coins", []),
            "bonuses": state.get("bonuses", {}),
            "collection_counts": state.get("collection_counts", {}),
            "params": state.get("params", {}),
        }

    def participant_result(self, participant_id: int) -> dict:
        state = self.state or {}
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
            "coin_bonus": round(
                float(state.get("bonuses", {}).get(participant_id_str, 0.0)), 2
            ),
            "collection_count": int(
                state.get("collection_counts", {}).get(participant_id_str, 0)
            ),
            "final_position": final_position,
            "world_id": self.world_id,
        }


class CanvasGameService(WebSocketEventService):
    """Typed websocket service for the shared-canvas game protocol."""

    class PositionEvent(ClientWebSocketEvent):
        type: Literal[POSITION_EVENT]
        session_id: str = Field(min_length=1)
        x: float = Field(ge=0, le=CANVAS_SIZE)
        y: float = Field(ge=0, le=CANVAS_SIZE)
        vx: float
        vy: float
        client_time: float
        low_latency: bool = True

    class CollectEvent(ClientWebSocketEvent):
        type: Literal[COLLECT_EVENT]
        session_id: str = Field(min_length=1)
        coin_id: str = Field(min_length=1)
        x: float = Field(ge=0, le=CANVAS_SIZE)
        y: float = Field(ge=0, le=CANVAS_SIZE)
        client_time: float

    class StateRequestEvent(ClientWebSocketEvent):
        type: Literal[STATE_REQUEST_EVENT]
        session_id: str = Field(min_length=1)

    class StateSnapshotEvent(ServerWebSocketEvent):
        type: Literal["state_snapshot"] = "state_snapshot"
        target_participant_ids: list[str]
        session_id: str
        group_id: int
        network_id: int
        world_id: str
        players: dict
        coins: list[dict]
        collected_coins: list[dict]
        bonuses: dict
        collection_counts: dict
        params: dict

    class PositionUpdateEvent(ServerWebSocketEvent):
        type: Literal["position_update"] = "position_update"
        session_id: str
        group_id: int
        target_participant_ids: list[str]
        event_id: int
        player: dict

    class CoinCollectedEvent(ServerWebSocketEvent):
        type: Literal["coin_collected"] = "coin_collected"
        session_id: str
        group_id: int
        target_participant_ids: list[str]
        collection: dict
        coins: list[dict]
        bonuses: dict

    class CollectRejectedEvent(ServerWebSocketEvent):
        type: Literal["collect_rejected"] = "collect_rejected"
        session_id: str
        target_participant_id: str
        participant_id: str
        coin_id: str
        reason: str

    @websocket_handler(PositionEvent)
    def position(self, event: PositionEvent):
        game_state = self.get_game_state(event.session_id)
        logged_event = CanvasPositionEvent(
            session_id=event.session_id,
            participant_id=self.participant.id,
            x=event.x,
            y=event.y,
            vx=event.vx,
            vy=event.vy,
            client_time=event.client_time,
            receive_time=event.receive_time,
        )
        db.session.add(logged_event)
        db.session.flush()
        player = self.position_player_payload(game_state, event)
        self.publish(
            self.PositionUpdateEvent(
                session_id=game_state.session_id,
                group_id=game_state.group_id,
                target_participant_ids=[
                    str(p_id) for p_id in game_state.participant_ids
                ],
                event_id=logged_event.id,
                player=player,
            )
        )
        db.session.commit()

    @websocket_handler(CollectEvent)
    def collect(self, event: CollectEvent):
        game_state = self.get_game_state(event.session_id, for_update=True)
        accepted, reason, collection = game_state.record_collection(
            participant_id=self.participant.id,
            coin_id=event.coin_id,
            x=event.x,
            y=event.y,
            receive_time=event.receive_time,
        )
        db.session.add(
            CanvasCollectEvent(
                session_id=event.session_id,
                participant_id=self.participant.id,
                coin_id=event.coin_id,
                x=event.x,
                y=event.y,
                client_time=event.client_time,
                accepted=accepted,
                rejection_reason=reason,
                receive_time=event.receive_time,
            )
        )
        if accepted:
            self.publish(
                self.CoinCollectedEvent(
                    session_id=game_state.session_id,
                    group_id=game_state.group_id,
                    target_participant_ids=[
                        str(p_id) for p_id in game_state.participant_ids
                    ],
                    collection=collection,
                    coins=(game_state.state or {}).get("coins", []),
                    bonuses=(game_state.state or {}).get("bonuses", {}),
                )
            )
        else:
            self.publish(
                self.CollectRejectedEvent(
                    session_id=game_state.session_id,
                    target_participant_id=str(self.participant.id),
                    participant_id=str(self.participant.id),
                    coin_id=event.coin_id,
                    reason=reason,
                )
            )
        db.session.commit()

    @websocket_handler(StateRequestEvent)
    def state_request(self, event: StateRequestEvent):
        game_state = self.get_game_state(event.session_id)
        db.session.add(
            CanvasStateRequestEvent(
                session_id=event.session_id,
                participant_id=self.participant.id,
                receive_time=event.receive_time,
            )
        )
        self.publish(
            self.StateSnapshotEvent(**game_state.state_snapshot_payload(self.participant.id))
        )
        db.session.commit()

    def accepts_event(self, event: ClientWebSocketEvent):
        if not super().accepts_event(event):
            return False
        game_state = self.get_game_state(getattr(event, "session_id", ""), warn=False)
        if game_state is None:
            self.warn_rejected_event("unknown session ID", event)
            return False
        if int(self.participant.id) not in game_state.participant_ids:
            self.warn_rejected_event("participant not in canvas session", event)
            return False
        return True

    def get_game_state(self, session_id: str, *, for_update=False, warn=True):
        query = CanvasGameState.query.filter_by(session_id=session_id)
        if for_update:
            query = query.with_for_update(of=CanvasGameState)
        game_state = query.one_or_none()
        if game_state is None and warn:
            raise ValueError(f"Unknown canvas session_id: {session_id}")
        return game_state

    def position_player_payload(self, game_state: CanvasGameState, event: PositionEvent):
        state = game_state.state or {}
        participant_id = str(self.participant.id)
        players = state.get("players", {})
        player = deepcopy(players.get(participant_id, {}))
        canvas_size = state.get("params", {}).get("world", {}).get(
            "canvas_size", CANVAS_SIZE
        )
        player.update(
            {
                "participant_id": participant_id,
                "x": round(clamp(event.x, 0, canvas_size), 3),
                "y": round(clamp(event.y, 0, canvas_size), 3),
                "vx": round(event.vx, 3),
                "vy": round(event.vy, 3),
                "client_time": event.client_time,
                "receive_time": receive_time_iso(event.receive_time),
            }
        )
        return player


class EnableSharedCanvas(NullElt, ValidatedWebSocketElt):
    """Timeline element that activates the shared-canvas websocket channel."""

    channel = CANVAS_WS_CHANNEL
    service_class = CanvasGameService


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
            "Coins are visible to everyone. Move over a coin to collect it. "
            "Each coin you collect adds $0.10 to your bonus."
        )
    return InfoPage(content, time_estimate=20)


def build_session_id(trial, group) -> str:
    return f"shared_canvas:{trial.network.id}:group:{int(group.id)}"


def participant_order(participant: Participant):
    group = participant.active_sync_groups[GROUP_TYPE]
    return sorted(group.participants, key=lambda p: p.id)


def build_bot_answer(bot) -> dict:
    return {
        "completed_live_canvas_browser": True,
        "bot_participant_id": bot.id,
        "note": "PsyNet bot path bypasses browser websocket canvas interaction.",
    }


def build_game_config(trial, participant: Participant) -> dict:
    ordered = participant_order(participant)
    group = participant.active_sync_groups[GROUP_TYPE]
    role_index = [p.id for p in ordered].index(participant.id)
    world = trial.definition["world"]
    session_id = build_session_id(trial, group)
    CanvasGameState.get_or_create(
        session_id,
        defaults={
            "group_id": int(group.id),
            "network_id": trial.network.id,
            "world_id": world["world_id"],
            "state": CanvasGameState.initial_state([p.id for p in ordered], world),
        },
    )
    return {
        "channel": CANVAS_WS_CHANNEL,
        "immediate": CANVAS_WS_IMMEDIATE,
        "tolerance": CANVAS_WS_TOLERANCE,
        "session_id": session_id,
        "participant_id": participant.id,
        "group_id": int(group.id),
        "role": f"Player {role_index + 1}",
        "world_id": world["world_id"],
        "canvas_size": world["canvas_size"],
        "trial_seconds": TRIAL_SECONDS,
        "send_interval_ms": SEND_INTERVAL_MS,
        "draw_interval_ms": DRAW_INTERVAL_MS,
        "player_radius": PLAYER_RADIUS,
        "coin_radius": world["coin_radius"],
        "coin_bonus": COIN_BONUS,
    }


class SharedCanvasControl(Control):
    """Custom canvas renderer wrapped in PsyNet's modular page API."""

    external_template = "shared_canvas.html"
    macro = "shared_canvas_control"

    def __init__(self, game_config):
        super().__init__(show_next_button=False)
        self.game_config = game_config

    def format_answer(self, raw_answer, **kwargs):
        return raw_answer

    def get_bot_response(self, experiment, bot, page, prompt):
        return build_bot_answer(bot)


class SharedCanvasTrial(StaticTrial):
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
                "Use the arrow keys to move. Collect the gold coins before the "
                "session ends."
            )
        return ModularPage(
            "shared_canvas",
            prompt,
            SharedCanvasControl(build_game_config(self, participant)),
            save_answer="shared_canvas_browser_answer",
            time_estimate=TRIAL_SECONDS + 5,
        )

    def score_canvas_game(self, participants: List[Participant]):
        group = participants[0].active_sync_groups[GROUP_TYPE]
        game_state = CanvasGameState.query.filter_by(
            session_id=build_session_id(self, group)
        ).one_or_none()
        for participant in participants:
            if game_state is None:
                participant.var.shared_canvas_result = {
                    "completed_live_canvas": False,
                    "participant_id": participant.id,
                    "collected_coin_ids": [],
                    "coin_bonus": 0.0,
                    "collection_count": 0,
                    "final_position": None,
                    "world_id": self.definition["world"]["world_id"],
                    "error": "missing_canvas_game_state",
                }
            else:
                participant.var.shared_canvas_result = game_state.participant_result(
                    participant.id
                )

    def format_answer(self, raw_answer, **kwargs):
        participant = kwargs.get("participant")
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
        "soft_max_experiment_payment": 1000.0,
        "hard_max_experiment_payment": 1100.0,
        "max_participant_payment": 25.0,
        "soft_max_experiment_payment_email_sent": False,
        "hard_max_experiment_payment_email_sent": False,
    }

    timeline = Timeline(
        NoConsent(),
        EnableSharedCanvas(),
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

    @staticmethod
    def _valid_position_event():
        return CanvasGameService.parse_event(
            json.dumps(
                {
                    "type": POSITION_EVENT,
                    "session_id": "test-session",
                    "x": 12.5,
                    "y": 13.5,
                    "vx": 1.0,
                    "vy": -1.0,
                    "client_time": 100.0,
                    "page_uuid": "current-page",
                }
            )
        )

    @staticmethod
    def _assert_payload_rejected(payload):
        try:
            CanvasGameService.parse_event(json.dumps(payload))
        except (ValidationError, ValueError):
            pass
        else:
            raise AssertionError(f"Expected payload to be rejected: {payload}")

    @staticmethod
    def test_websocket_event_parsing():
        event = Exp._valid_position_event()
        assert event.type == POSITION_EVENT
        assert event.session_id == "test-session"
        assert event.receive_time.tzinfo is not None
        invalid_payloads = [
            {"type": POSITION_EVENT, "session_id": "test-session", "x": 1.0},
            {
                "type": POSITION_EVENT,
                "session_id": "test-session",
                "x": 1.0,
                "y": 1.0,
                "vx": 0.0,
                "vy": 0.0,
                "client_time": 1.0,
            },
            {
                "type": COLLECT_EVENT,
                "session_id": "test-session",
                "coin_id": "",
                "x": 1.0,
                "y": 1.0,
                "client_time": 1.0,
                "page_uuid": "current-page",
            },
            {"type": "unknown", "page_uuid": "current-page"},
        ]
        for payload in invalid_payloads:
            Exp._assert_payload_rejected(payload)

    @staticmethod
    def test_canvas_state_transitions():
        world = generate_world(99)
        state = CanvasGameState(
            session_id="state-transition-test",
            group_id=1,
            network_id=1,
            world_id=world["world_id"],
            state=CanvasGameState.initial_state([1], world),
        )
        coin = state.state["coins"][0]
        event = CanvasGameService.CollectEvent(
            type=COLLECT_EVENT,
            session_id=state.session_id,
            coin_id=coin["id"],
            x=coin["x"],
            y=coin["y"],
            client_time=1.0,
            page_uuid="current-page",
        )

        accepted, reason, collection = state.record_collection(
            participant_id=1,
            coin_id=event.coin_id,
            x=event.x,
            y=event.y,
            receive_time=event.receive_time,
        )

        assert accepted is True
        assert reason is None
        assert collection["coin_id"] == coin["id"]
        assert coin["id"] not in [c["id"] for c in state.state["coins"]]
        assert state.participant_result(1)["coin_bonus"] == COIN_BONUS

        accepted, reason, _ = state.record_collection(
            participant_id=1,
            coin_id=event.coin_id,
            x=event.x,
            y=event.y,
            receive_time=event.receive_time,
        )
        assert accepted is False
        assert reason == "already_collected_or_unknown"

    @staticmethod
    def test_websocket_event_authorization():
        world = generate_world(100)
        session = CanvasGameState(
            session_id="authorization-test",
            group_id=1,
            network_id=1,
            world_id=world["world_id"],
            state=CanvasGameState.initial_state([1], world),
        )
        db.session.add(session)
        db.session.flush()
        service = CanvasGameService(
            SimpleNamespace(id=1, page_uuid="current-page"),
            SimpleNamespace(),
            CANVAS_WS_CHANNEL,
        )
        event = Exp._valid_position_event().model_copy(
            update={"session_id": session.session_id}
        )
        assert service.accepts_event(event)
        assert not service.accepts_event(event.model_copy(update={"page_uuid": "old"}))
        assert not service.accepts_event(event.model_copy(update={"session_id": "bad"}))

    @staticmethod
    def test_server_event_serialization():
        event = CanvasGameService.CollectRejectedEvent(
            session_id="test-session",
            target_participant_id="1",
            participant_id="1",
            coin_id="coin-1",
            reason="too_far",
        )
        assert json.loads(event.to_json()) == {
            "type": "collect_rejected",
            "session_id": "test-session",
            "target_participant_id": "1",
            "participant_id": "1",
            "coin_id": "coin-1",
            "reason": "too_far",
        }

    def test_canvas_websocket_contracts(self):
        self.test_websocket_event_parsing()
        self.test_canvas_state_transitions()
        self.test_websocket_event_authorization()
        self.test_server_event_serialization()

    def test_serial_run_bots(self, bots: List[BotDriver]):
        self.test_canvas_websocket_contracts()

        advance_past_wait_pages(bots)

        for bot in bots:
            assert "Shared canvas navigation" in bot.current_page_text
            assert "Each coin you collect adds $0.10" in bot.current_page_text
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
            participant = Participant.query.get(bot.id)
            group_id = int(participant.active_sync_groups[GROUP_TYPE].id)
            answers_by_group.setdefault(group_id, []).append(answer)

        assert len(answers_by_group) == len(bots) // GROUP_SIZE
        assert all(len(group_answers) == GROUP_SIZE for group_answers in answers_by_group.values())