from __future__ import annotations

import json
import math
import os
import random
from copy import deepcopy
from dataclasses import dataclass
from datetime import timezone
from typing import List

from dallinger import db
from dominate import tags
from sqlalchemy import Boolean, Column, Integer, JSON, String

import psynet.experiment
from psynet.bot import BotDriver, advance_past_wait_pages
from psynet.data import SQLBase, SQLMixin, register_table
from psynet.page import InfoPage, WaitPage
from psynet.participant import Participant
from psynet.sync import GroupBarrier, SimpleGrouper
from psynet.timeline import NullElt, Page, PageMaker, Timeline, WebSocketElt, join
from psynet.trial.static import StaticNode, StaticTrial, StaticTrialMaker

from psynet.consent import NoConsent


GROUP_TYPE = "shared_canvas_group"
CANVAS_WS_CHANNEL = "shared_canvas_live"
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
POSITION_EVENT = "PositionEvent"
COLLECT_EVENT = "CollectEvent"

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


@register_table
class ClientEvent(SQLBase, SQLMixin):
    """Generic persisted client-originated event, usable without subclassing."""

    __tablename__ = "client_event"

    session_id = Column(String(128), index=True)
    participant_id = Column(Integer, index=True, nullable=True)
    event_type = Column(String(64), index=True)
    low_latency = Column(Boolean, default=False, index=True)
    payload = Column(JSON)

    @staticmethod
    def message_payload(data, receive_time) -> dict:
        payload = {
            key: value
            for key, value in data.items()
            if key not in {"type", "participant_id", "low_latency"}
        }
        payload["receive_time"] = (
            receive_time.astimezone(timezone.utc).isoformat() if receive_time else None
        )
        return payload

    @classmethod
    def from_message(cls, *, data, participant, receive_time, session):
        return cls(
            session_id=session.session_id,
            participant_id=participant.id,
            event_type=data.get("type", "unknown"),
            low_latency=bool(data.get("low_latency", False)),
            payload=cls.message_payload(data, receive_time),
        )


@dataclass(frozen=True)
class ServerEvent:
    """Transient server-originated event to broadcast after reducing a client event."""

    payloads: tuple[dict, ...] = ()

    @classmethod
    def from_payload(cls, payload: dict):
        return cls((payload,))

    @classmethod
    def state_snapshot(cls, session, participant_id: int):
        return cls.from_payload(session.state_snapshot(participant_id))


class LiveSessionMixin:
    """Shared live-session behavior for separately mapped session tables."""

    event_class = ClientEvent

    @staticmethod
    def initial_state(participant_ids=None, **params) -> dict:
        return {
            "params": {
                "participant_ids": [str(p) for p in (participant_ids or [])],
                **params,
            },
        }

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
    def cached_event(event: ClientEvent) -> dict:
        return {
            "id": event.id,
            "event_type": event.event_type,
            "participant_id": event.participant_id,
            "low_latency": bool(event.low_latency),
            "payload": event.payload or {},
        }

    @property
    def participant_ids(self) -> list[int]:
        state = self.state or {}
        return [int(p) for p in state.get("params", {}).get("participant_ids", [])]

    @property
    def events(self):
        return (
            self.event_class.query.filter_by(session_id=self.session_id)
            .order_by(self.event_class.id)
            .all()
        )

    def reduce_event(self, event: ClientEvent) -> ServerEvent | None:
        state = deepcopy(self.state or self.initial_state())
        cached_event = self.cached_event(event)
        self.state = state
        return ServerEvent.from_payload(
            {
                "type": "generic_event",
                "session_id": self.session_id,
                "target_participant_id": str(event.participant_id),
                "event": cached_event,
            }
        )

    def state_snapshot(self, participant_id: int) -> dict:
        return {
            "type": "state_snapshot",
            "target_participant_id": str(participant_id),
            "session_id": self.session_id,
            "state": self.state or {},
        }


@register_table
class LiveSession(SQLBase, SQLMixin, LiveSessionMixin):
    """Generic persisted live-session projection, usable without subclassing."""

    __tablename__ = "live_session"

    session_id = Column(String(128), index=True)
    state = Column(JSON)


@register_table
class CanvasLiveSession(SQLBase, SQLMixin, LiveSessionMixin):
    __tablename__ = "canvas_live_session"

    session_id = Column(String(128), index=True)
    group_id = Column(Integer, index=True)
    network_id = Column(Integer, index=True)
    world_id = Column(String(64), index=True)
    state = Column(JSON)

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

    def reduce_event(self, event: ClientEvent) -> ServerEvent | None:
        state = deepcopy(self.state or {})
        event_type = event.event_type
        payload = event.payload or {}
        server_event = None

        if event_type == POSITION_EVENT:
            server_event = self._reduce_position_event(state, event, payload)
        elif event_type == COLLECT_EVENT:
            server_event = self._reduce_collect_event(state, event, payload)
        elif event_type == "state_request":
            server_event = ServerEvent.state_snapshot(self, event.participant_id)

        self.state = state
        return server_event

    def _reduce_position_event(
        self, state: dict, event: ClientEvent, payload: dict
    ) -> ServerEvent | None:
        participant_id = str(event.participant_id)
        if participant_id not in state.get("players", {}):
            return ServerEvent.state_snapshot(self, event.participant_id)
        try:
            x = float(payload["x"])
            y = float(payload["y"])
            vx = float(payload["vx"])
            vy = float(payload["vy"])
        except (KeyError, TypeError, ValueError):
            return ServerEvent.state_snapshot(self, event.participant_id)

        canvas_size = state["params"]["world"]["canvas_size"]
        player = state["players"][participant_id]
        player.update(
            {
                "x": round(clamp(x, 0, canvas_size), 3),
                "y": round(clamp(y, 0, canvas_size), 3),
                "vx": round(vx, 3),
                "vy": round(vy, 3),
                "client_time": payload.get("client_time"),
                "receive_time": payload.get("receive_time"),
            }
        )
        return ServerEvent.from_payload(
            {
                "type": "position_update",
                "session_id": self.session_id,
                "group_id": self.group_id,
                "target_participant_ids": [str(p_id) for p_id in self.participant_ids],
                "player": deepcopy(player),
            }
        )

    def _reduce_collect_event(
        self, state: dict, event: ClientEvent, payload: dict
    ) -> ServerEvent:
        participant_id = str(event.participant_id)
        coin_id = payload.get("coin_id")
        coin = next((c for c in state.get("coins", []) if c["id"] == coin_id), None)
        if coin is None:
            return self.collect_rejected_event(
                event=event,
                participant_id=participant_id,
                coin_id=coin_id,
                reason="already_collected_or_unknown",
            )

        player = state.get("players", {}).get(participant_id)
        if player is None:
            return self.collect_rejected_event(
                event=event,
                participant_id=participant_id,
                coin_id=coin_id,
                reason="unknown_player",
            )

        try:
            x = float(payload.get("x", player["x"]))
            y = float(payload.get("y", player["y"]))
        except (TypeError, ValueError):
            x, y = float(player["x"]), float(player["y"])

        distance = math.hypot(float(coin["x"]) - x, float(coin["y"]) - y)
        collect_radius = float(coin.get("radius", COIN_RADIUS)) + PLAYER_RADIUS + 4
        if distance > collect_radius:
            return self.collect_rejected_event(
                event=event,
                participant_id=participant_id,
                coin_id=coin_id,
                reason="too_far",
            )

        state["coins"] = [c for c in state.get("coins", []) if c["id"] != coin_id]
        collected = {
            "event_id": event.id,
            "coin_id": coin_id,
            "participant_id": participant_id,
            "x": coin["x"],
            "y": coin["y"],
            "bonus": COIN_BONUS,
            "receive_time": payload.get("receive_time"),
        }
        state.setdefault("collected_coins", []).append(collected)
        state.setdefault("collection_counts", {}).setdefault(participant_id, 0)
        state["collection_counts"][participant_id] += 1
        state.setdefault("bonuses", {}).setdefault(participant_id, 0.0)
        state["bonuses"][participant_id] = round(
            float(state["bonuses"][participant_id]) + COIN_BONUS,
            2,
        )
        return ServerEvent.from_payload(
            {
                "type": "coin_collected",
                "session_id": self.session_id,
                "group_id": self.group_id,
                "target_participant_ids": [str(p_id) for p_id in self.participant_ids],
                "collection": collected,
                "coins": state.get("coins", []),
                "bonuses": state.get("bonuses", {}),
            }
        )

    def collect_rejected_event(
        self,
        *,
        event: ClientEvent,
        participant_id: str,
        coin_id,
        reason: str,
    ) -> ServerEvent:
        return ServerEvent.from_payload(
            {
                "type": "collect_rejected",
                "session_id": self.session_id,
                "target_participant_id": str(event.participant_id),
                "participant_id": participant_id,
                "coin_id": coin_id,
                "reason": reason,
            }
        )

    def state_snapshot(self, participant_id: int) -> dict:
        state = self.state or {}
        return {
            "type": "state_snapshot",
            "target_participant_id": str(participant_id),
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


class LiveSessionWebSocket(NullElt, WebSocketElt):
    session_class = LiveSession
    event_class = ClientEvent

    def handle_message(
        self, message, channel_name, participant, node, receive_time, experiment
    ):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        if participant is None:
            participant = self.get_participant(data)
            if participant is None:
                return

        low_latency = self.is_low_latency_event(data)
        session = self.get_session(
            self.get_session_id(data),
            for_update=not low_latency,
        )

        event = self.create_event(data, participant, receive_time, session)
        db.session.add(event)
        db.session.flush()

        if low_latency:
            server_event = self.server_event_from_client_event(session, event)
        else:
            server_event = session.reduce_event(event)

        if server_event is not None:
            self.broadcast_server_event(
                experiment=experiment,
                server_event=server_event,
            )

        db.session.commit()

    def get_session_id(self, data) -> str:
        if data.get("session_id") is None:
            raise ValueError("Live websocket message missing session_id")
        return str(data["session_id"])

    def is_low_latency_event(self, data) -> bool:
        return bool(data.get("low_latency", False))

    def get_participant(self, data):
        participant_id = data.get("participant_id")
        if participant_id is None:
            return None
        try:
            return Participant.query.get(int(participant_id))
        except (TypeError, ValueError):
            return None

    def get_session(self, session_id: str, *, for_update: bool = True):
        query = self.session_class.query.filter_by(session_id=session_id)
        if for_update:
            query = query.with_for_update(of=self.session_class)
        session = query.one_or_none()
        if session is None:
            raise ValueError(f"Unknown live session_id: {session_id}")
        return session

    def create_event(self, data, participant, receive_time, session):
        return self.event_class.from_message(
            data=data,
            participant=participant,
            receive_time=receive_time,
            session=session,
        )

    def server_event_from_client_event(self, session, event) -> ServerEvent | None:
        return None

    def broadcast_server_event(self, *, experiment, server_event: ServerEvent):
        for payload in server_event.payloads:
            self.broadcast(experiment, payload)

    def broadcast(self, experiment, payload):
        experiment.publish_to_subscribers(json.dumps(payload), channel_name=self.channel)


class CanvasWebSocket(LiveSessionWebSocket):
    channel = CANVAS_WS_CHANNEL
    session_class = CanvasLiveSession
    event_class = ClientEvent

    def server_event_from_client_event(self, session, event) -> ServerEvent | None:
        if event.event_type != POSITION_EVENT:
            return None

        state = session.state or {}
        participant_id = str(event.participant_id)
        players = state.get("players", {})
        if participant_id not in players:
            return None

        payload = event.payload or {}
        try:
            x = float(payload["x"])
            y = float(payload["y"])
            vx = float(payload["vx"])
            vy = float(payload["vy"])
        except (KeyError, TypeError, ValueError):
            return None

        canvas_size = state.get("params", {}).get("world", {}).get(
            "canvas_size", CANVAS_SIZE
        )
        player = deepcopy(players[participant_id])
        player.update(
            {
                "x": round(clamp(x, 0, canvas_size), 3),
                "y": round(clamp(y, 0, canvas_size), 3),
                "vx": round(vx, 3),
                "vy": round(vy, 3),
                "client_time": payload.get("client_time"),
                "receive_time": payload.get("receive_time"),
            }
        )
        return ServerEvent.from_payload(
            {
                "type": "position_update",
                "session_id": session.session_id,
                "group_id": session.group_id,
                "target_participant_ids": [str(p_id) for p_id in session.participant_ids],
                "event_id": event.id,
                "player": player,
            }
        )


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
        "completed_live_canvas": True,
        "bot_participant_id": bot.id,
        "collected_coin_ids": [],
        "coin_bonus": 0.0,
        "note": "PsyNet bot path bypasses browser websocket canvas interaction.",
    }


class RealTimeCanvasPage(Page):
    def __init__(self, *, trial, participant, **kwargs):
        ordered = participant_order(participant)
        group = participant.active_sync_groups[GROUP_TYPE]
        role_index = [p.id for p in ordered].index(participant.id)
        role = f"Player {role_index + 1}"
        world = trial.definition["world"]
        session_id = build_session_id(trial, group)
        CanvasLiveSession.get_or_create(
            session_id,
            defaults={
                "group_id": int(group.id),
                "network_id": trial.network.id,
                "world_id": world["world_id"],
                "state": CanvasLiveSession.initial_state([p.id for p in ordered], world),
            },
        )
        template_path = os.path.join(
            os.path.dirname(__file__), "templates", "shared_canvas.html"
        )
        game_config = {
            "channel": CANVAS_WS_CHANNEL,
            "session_id": session_id,
            "participant_id": participant.id,
            "group_id": int(group.id),
            "role": role,
            "world_id": world["world_id"],
            "canvas_size": world["canvas_size"],
            "trial_seconds": TRIAL_SECONDS,
            "send_interval_ms": SEND_INTERVAL_MS,
            "draw_interval_ms": DRAW_INTERVAL_MS,
            "player_radius": PLAYER_RADIUS,
            "coin_radius": world["coin_radius"],
            "coin_bonus": COIN_BONUS,
        }
        super().__init__(
            label="shared_canvas",
            template_path=template_path,
            template_arg={"trial_seconds": TRIAL_SECONDS},
            js_vars={"game_config": game_config},
            time_estimate=TRIAL_SECONDS + 5,
            **kwargs,
        )

    def get_bot_response(self, experiment, bot):
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
            RealTimeCanvasPage(trial=self, participant=participant),
        )

    def format_answer(self, raw_answer, **kwargs):
        if isinstance(raw_answer, dict):
            raw_answer = {**raw_answer}
            raw_answer.setdefault("world_id", self.definition["world"]["world_id"])
            raw_answer.setdefault("coin_bonus", 0.0)
            return raw_answer
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
        CanvasWebSocket(),
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

    def test_serial_run_bots(self, bots: List[BotDriver]):
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
            participant = Participant.query.get(bot.id)
            group_id = int(participant.active_sync_groups[GROUP_TYPE].id)
            answers_by_group.setdefault(group_id, []).append(answer)

        assert len(answers_by_group) == len(bots) // GROUP_SIZE
        assert all(len(group_answers) == GROUP_SIZE for group_answers in answers_by_group.values())