from __future__ import annotations

import json
import math
import os
import random
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import List, Literal

from dallinger import db
from dominate import tags
from jinja2 import Environment, FileSystemLoader, select_autoescape
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
    from .session_worlds import (
        build_world_from_session_dir,
        load_world_definitions,
        public_world_payload,
    )
    from .websocket_protocol import (
        ClientWebSocketEvent,
        ServerWebSocketEvent,
        ValidatedWebSocketElt,
        WebSocketEventService,
        websocket_handler,
    )
else:
    from session_worlds import (
        build_world_from_session_dir,
        load_world_definitions,
        public_world_payload,
    )
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
STATE_REQUEST_EVENT = "state_request"
READY_EVENT = "ready"
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
    world_id = Column(String(256), index=True)
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
            "reward_targets": deepcopy(world.get("reward_targets", [])),
            "awarded_target_keys": [],
            "collected_coins": [],
            "bonuses": {participant_id: 0.0 for participant_id in ordered_ids},
            "collection_counts": {participant_id: 0 for participant_id in ordered_ids},
            "ready_participant_ids": [],
            "server_start_time": None,
        }

    @property
    def participant_ids(self) -> list[int]:
        state = self.state or {}
        return [int(p) for p in state.get("params", {}).get("participant_ids", [])]

    def ensure_participants(self, participant_ids: list[int], world: dict):
        state = deepcopy(self.state or {})
        params = state.setdefault("params", {})
        existing_ids = [str(p) for p in params.get("participant_ids", [])]
        ordered_ids = [str(p) for p in participant_ids]
        existing_world_id = params.get("world", {}).get("world_id")
        if existing_ids and (
            existing_ids != ordered_ids or existing_world_id != world.get("world_id")
        ):
            self.state = CanvasGameState.initial_state(participant_ids, world)
            return
        missing_ids = [participant_id for participant_id in ordered_ids if participant_id not in existing_ids]
        if not missing_ids:
            return

        all_ids = existing_ids + missing_ids
        for participant_id in missing_ids:
            state.setdefault("bonuses", {})[participant_id] = 0.0
            state.setdefault("collection_counts", {})[participant_id] = 0

        params["participant_ids"] = all_ids
        state.setdefault("ready_participant_ids", [])
        self.state = state

    @staticmethod
    def _parse_receive_time(value):
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def mark_ready(self, participant_id: int, receive_time) -> bool:
        state = deepcopy(self.state or {})
        participant_id = str(participant_id)
        participant_ids = [str(p) for p in state.get("params", {}).get("participant_ids", [])]
        ready = set(str(p) for p in state.setdefault("ready_participant_ids", []))
        if participant_id in participant_ids:
            ready.add(participant_id)
        state["ready_participant_ids"] = sorted(ready)

        started_now = False
        if participant_ids and ready.issuperset(participant_ids) and not state.get("server_start_time"):
            state["server_start_time"] = receive_time_iso(receive_time)
            started_now = True
        self.state = state
        return started_now

    def is_started(self) -> bool:
        return bool((self.state or {}).get("server_start_time"))

    def timing_payload(self) -> dict:
        state = self.state or {}
        world = state.get("params", {}).get("world", {})
        return {
            "game_start_ms": world.get("game_start_ms", 0),
            "game_end_ms": world.get("game_end_ms", TRIAL_SECONDS * 1000),
            "raw_time_min_ms": world.get("raw_time_min_ms"),
            "raw_time_max_ms": world.get("raw_time_max_ms"),
            "server_start_time": state.get("server_start_time"),
        }

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
        client_game_time_ms: float | None = None,
    ):
        """Retained for old browser messages; reward targets stay server-owned."""
        state = deepcopy(self.state or {})
        participant_id = str(participant_id)
        target = next(
            (c for c in state.get("reward_targets", []) if c["id"] == coin_id), None
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

    def state_snapshot_payload(self, participant_id: int) -> dict:
        state = self.state or {}
        world = state.get("params", {}).get("world", {})
        public_collections = [
            self.public_collection_payload(c)
            for c in state.get("collected_coins", [])
            if str(c.get("participant_id")) == str(participant_id)
        ]
        return {
            "session_id": self.session_id,
            "group_id": self.group_id,
            "network_id": self.network_id,
            "world_id": self.world_id,
            "target_participant_ids": [str(p) for p in self.participant_ids],
            "coins": state.get("coins", []),
            "collected_coins": public_collections,
            "bonuses": state.get("bonuses", {}),
            "collection_counts": state.get("collection_counts", {}),
            "ready_participant_ids": state.get("ready_participant_ids", []),
            "game_started": bool(state.get("server_start_time")),
            "params": state.get("params", {}),
            "timing": self.timing_payload(),
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
            "world_id": self.world_id,
            "session_label": state.get("params", {})
            .get("world", {})
            .get("session_label"),
        }


class CanvasGameService(WebSocketEventService):
    """Typed websocket service for the shared-canvas game protocol."""

    class PositionEvent(ClientWebSocketEvent):
        type: Literal[POSITION_EVENT]
        session_id: str = Field(min_length=1)
        group_id: int | None = None
        x: float = Field(ge=0, le=CANVAS_SIZE)
        y: float = Field(ge=0, le=CANVAS_SIZE)
        vx: float
        vy: float
        client_time: float
        game_time_ms: float = Field(default=0.0, ge=0)
        low_latency: bool = True

    class CollectEvent(ClientWebSocketEvent):
        type: Literal[COLLECT_EVENT]
        session_id: str = Field(min_length=1)
        coin_id: str = Field(min_length=1)
        x: float = Field(ge=0, le=CANVAS_SIZE)
        y: float = Field(ge=0, le=CANVAS_SIZE)
        client_time: float
        game_time_ms: float | None = Field(default=None, ge=0)

    class StateRequestEvent(ClientWebSocketEvent):
        type: Literal[STATE_REQUEST_EVENT]
        session_id: str = Field(min_length=1)

    class ReadyEvent(ClientWebSocketEvent):
        type: Literal[READY_EVENT]
        session_id: str = Field(min_length=1)

    class StateSnapshotEvent(ServerWebSocketEvent):
        type: Literal["state_snapshot"] = "state_snapshot"
        target_participant_ids: list[str]
        session_id: str
        group_id: int
        network_id: int
        world_id: str
        coins: list[dict]
        collected_coins: list[dict]
        bonuses: dict
        collection_counts: dict
        ready_participant_ids: list[str]
        game_started: bool
        params: dict
        timing: dict

    class ReadyStatusEvent(ServerWebSocketEvent):
        type: Literal["ready_status"] = "ready_status"
        session_id: str
        group_id: int
        target_participant_ids: list[str]
        ready_participant_ids: list[str]
        game_started: bool

    class GameStartedEvent(ServerWebSocketEvent):
        type: Literal["game_started"] = "game_started"
        session_id: str
        group_id: int
        target_participant_ids: list[str]
        ready_participant_ids: list[str]
        timing: dict

    class PositionUpdateEvent(ServerWebSocketEvent):
        type: Literal["position_update"] = "position_update"
        session_id: str
        group_id: int
        target_participant_ids: list[str] | None = None
        event_id: int | None = None
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
        player = self.position_player_payload(event)
        self.publish(
            self.PositionUpdateEvent(
                session_id=event.session_id,
                group_id=self.position_group_id(event),
                target_participant_ids=None,
                player=player,
            )
        )
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
        db.session.commit()

    @websocket_handler(ReadyEvent)
    def ready(self, event: ReadyEvent):
        game_state = self.get_game_state(event.session_id, for_update=True)
        game_state.mark_ready(self.participant.id, event.receive_time)
        target_participant_ids = [str(p_id) for p_id in game_state.participant_ids]
        ready_participant_ids = (game_state.state or {}).get("ready_participant_ids", [])
        if game_state.is_started():
            message = self.GameStartedEvent(
                session_id=game_state.session_id,
                group_id=game_state.group_id,
                target_participant_ids=target_participant_ids,
                ready_participant_ids=ready_participant_ids,
                timing=game_state.timing_payload(),
            )
        else:
            message = self.ReadyStatusEvent(
                session_id=game_state.session_id,
                group_id=game_state.group_id,
                target_participant_ids=target_participant_ids,
                ready_participant_ids=ready_participant_ids,
                game_started=False,
            )
        db.session.commit()
        self.publish(message)

    @websocket_handler(CollectEvent)
    def collect(self, event: CollectEvent):
        game_state = self.get_game_state(event.session_id, for_update=True)
        accepted, reason, collection = game_state.record_collection(
            participant_id=self.participant.id,
            coin_id=event.coin_id,
            x=event.x,
            y=event.y,
            receive_time=event.receive_time,
            client_game_time_ms=event.game_time_ms,
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
            message = self.CoinCollectedEvent(
                session_id=game_state.session_id,
                group_id=game_state.group_id,
                target_participant_ids=[
                    str(p_id) for p_id in game_state.participant_ids
                ],
                collection=CanvasGameState.public_collection_payload(collection),
                coins=(game_state.state or {}).get("coins", []),
                bonuses=(game_state.state or {}).get("bonuses", {}),
            )
        else:
            message = self.CollectRejectedEvent(
                session_id=game_state.session_id,
                target_participant_id=str(self.participant.id),
                participant_id=str(self.participant.id),
                coin_id=event.coin_id,
                reason=reason,
            )
        db.session.commit()
        self.publish(message)

    @websocket_handler(StateRequestEvent)
    def state_request(self, event: StateRequestEvent):
        game_state = self.get_game_state(event.session_id)
        message = self.StateSnapshotEvent(
            **game_state.state_snapshot_payload(self.participant.id)
        )
        db.session.add(
            CanvasStateRequestEvent(
                session_id=event.session_id,
                participant_id=self.participant.id,
                receive_time=event.receive_time,
            )
        )
        db.session.commit()
        self.publish(message)

    def accepts_event(self, event: ClientWebSocketEvent):
        if not super().accepts_event(event):
            return False
        if isinstance(event, self.PositionEvent):
            return True
        game_state = self.get_game_state(getattr(event, "session_id", ""), warn=False)
        if game_state is None:
            self.warn_rejected_event("unknown session ID", event)
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

    def position_group_id(self, event: PositionEvent) -> int:
        if event.group_id is not None:
            return int(event.group_id)
        return int(self.participant.active_sync_groups[GROUP_TYPE].id)

    def position_player_payload(self, event: PositionEvent):
        participant_id = str(self.participant.id)
        return {
            "participant_id": participant_id,
            "x": round(clamp(event.x, 0, CANVAS_SIZE), 3),
            "y": round(clamp(event.y, 0, CANVAS_SIZE), 3),
            "vx": round(event.vx, 3),
            "vy": round(event.vy, 3),
            "client_time": event.client_time,
            "receive_time": receive_time_iso(event.receive_time),
        }


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
            "Coins appear in places that are worth exploring. Colored potential "
            "markers and shaded zones can help you decide where to go."
        )
        tags.p(
            "Move through promising areas at the right time. Each coin you find "
            "adds $0.10 to your bonus."
        )
    return InfoPage(content, time_estimate=20)


def build_session_id(trial, group) -> str:
    return f"shared_canvas:group:{int(group.id)}"


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
    game_state = CanvasGameState.get_or_create(
        session_id,
        defaults={
            "group_id": int(group.id),
            "network_id": trial.network.id,
            "world_id": world["world_id"],
            "state": CanvasGameState.initial_state([p.id for p in ordered], world),
        },
    )
    game_state.ensure_participants([p.id for p in ordered], world)
    db.session.flush()
    initial_players = CanvasGameState.initial_players([p.id for p in ordered], world)
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
        "canvas_width": CANVAS_RENDER_WIDTH,
        "canvas_height": CANVAS_RENDER_HEIGHT,
        "trial_seconds": TRIAL_SECONDS,
        "send_interval_ms": SEND_INTERVAL_MS,
        "draw_interval_ms": DRAW_INTERVAL_MS,
        "player_radius": PLAYER_RADIUS,
        "initial_players": initial_players,
        "initial_player": initial_players[str(participant.id)],
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
                "Use the arrow keys to move. Watch the changing potential "
                "markers and shaded zones to find coins before the session ends."
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
        ordered = sorted(participants, key=lambda p: p.id)
        world = self.definition["world"]
        game_state = CanvasGameState.get_or_create(
            build_session_id(self, group),
            defaults={
                "group_id": int(group.id),
                "network_id": self.network.id,
                "world_id": world["world_id"],
                "state": CanvasGameState.initial_state([p.id for p in ordered], world),
            },
        )
        game_state.ensure_participants([p.id for p in ordered], world)
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
                    "group_id": 1,
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
    def _test_world():
        return {
            "world_id": "session-test",
            "session_label": "test",
            "seed": 123,
            "canvas_size": CANVAS_SIZE,
            "player_radius": PLAYER_RADIUS,
            "coin_radius": COIN_RADIUS,
            "coin_bonus": COIN_BONUS,
            "raw_time_min_ms": 0,
            "raw_time_max_ms": 30 * 60 * 1000,
            "game_start_ms": 0,
            "game_end_ms": TRIAL_SECONDS * 1000,
            "projection": {"mid_lat": 0, "scale": 1, "margin": 36},
            "spawn_points": [
                {"id": "spotter-a", "name": "Spotter A", "x": 140, "y": 150},
                {"id": "spotter-b", "name": "Spotter B", "x": 180, "y": 190},
            ],
            "storms": [
                {
                    "id": "storm-1",
                    "radar": "TEST",
                    "storm_id": "storm-1",
                    "start_ms": 0,
                    "end_ms": TRIAL_SECONDS * 1000,
                    "points": [[0, 100, 100, 30, 50, None, None, 0]],
                }
            ],
            "storm_points": [[0, TRIAL_SECONDS * 1000, 100, 100, 30, 50, 0]],
            "warnings": [
                {
                    "id": "zone-1",
                    "event": "Potential Zone",
                    "headline": "",
                    "sender": "",
                    "eff_ms": 0,
                    "exp_ms": TRIAL_SECONDS * 1000,
                    "polygons": [[[80, 80], [120, 80], [120, 120], [80, 120]]],
                }
            ],
            "reward_events": [
                {
                    "id": "target-1",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "x": 100,
                    "y": 100,
                    "line": [],
                    "radius": COIN_RADIUS,
                }
            ],
            "reward_targets": [
                {
                    "id": "target-1",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "x": 100,
                    "y": 100,
                    "line": [],
                    "radius": COIN_RADIUS,
                    "raw_start_ms": 0,
                    "raw_end_ms": 30 * 60 * 1000,
                    "mag": "UNK",
                    "location": "test",
                    "county": "",
                    "state": "",
                }
            ],
        }

    @staticmethod
    def test_websocket_event_parsing():
        event = Exp._valid_position_event()
        assert event.type == POSITION_EVENT
        assert event.session_id == "test-session"
        assert event.receive_time.tzinfo is not None
        ready_event = CanvasGameService.parse_event(
            json.dumps(
                {
                    "type": READY_EVENT,
                    "session_id": "test-session",
                    "page_uuid": "current-page",
                }
            )
        )
        assert ready_event.type == READY_EVENT
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
        world = Exp._test_world()
        state = CanvasGameState(
            session_id="state-transition-test",
            group_id=1,
            network_id=1,
            world_id=world["world_id"],
            state=CanvasGameState.initial_state([1, 2], world),
        )
        initial_players = CanvasGameState.initial_players([1, 2], world)
        spawn_xy = {(point["x"], point["y"]) for point in world["spawn_points"]}
        assert (
            initial_players["1"]["x"],
            initial_players["1"]["y"],
        ) in spawn_xy
        assert (
            initial_players["2"]["x"],
            initial_players["2"]["y"],
        ) in spawn_xy
        event = CanvasGameService.PositionEvent(
            type=POSITION_EVENT,
            session_id=state.session_id,
            x=100.0,
            y=100.0,
            vx=0.0,
            vy=0.0,
            client_time=1.0,
            game_time_ms=0.0,
            page_uuid="current-page",
        )

        assert not state.is_started()
        accepted, reason, collection = state.record_collection(
            participant_id=1,
            coin_id="target-1",
            x=event.x,
            y=event.y,
            receive_time=event.receive_time,
        )
        assert not accepted
        assert reason == "not_started"
        assert collection is None

        assert not state.mark_ready(1, event.receive_time)
        assert state.mark_ready(2, event.receive_time)
        assert state.is_started()
        state.ensure_participants([3, 4], world)
        assert state.participant_ids == [3, 4]
        assert (state.state or {}).get("ready_participant_ids") == []
        assert not state.is_started()

        accepted, reason, collection = state.record_collection(
            participant_id=3,
            coin_id="target-1",
            x=999.0,
            y=999.0,
            receive_time=event.receive_time,
        )
        assert not accepted
        assert reason == "not_started"
        assert collection is None

        assert not state.mark_ready(3, event.receive_time)
        assert state.mark_ready(4, event.receive_time)

        accepted, reason, collection = state.record_collection(
            participant_id=3,
            coin_id="target-1",
            x=999.0,
            y=999.0,
            receive_time=event.receive_time,
        )
        assert not accepted
        assert reason == "too_far"
        assert collection is None

        accepted, reason, collection = state.record_collection(
            participant_id=3,
            coin_id="target-1",
            x=event.x,
            y=event.y,
            receive_time=event.receive_time,
        )
        assert accepted
        assert reason is None
        assert collection["coin_id"] == "target-1"
        assert collection["public_coin_id"] == "coin-1"
        assert CanvasGameState.public_collection_payload(collection)["target_id"] == "target-1"
        assert state.participant_result(3)["coin_bonus"] == COIN_BONUS

        accepted, reason, collection = state.record_collection(
            participant_id=3,
            coin_id="target-1",
            x=event.x,
            y=event.y,
            receive_time=event.receive_time,
        )
        assert not accepted
        assert reason == "already_awarded"
        assert collection is None
        assert state.participant_result(3)["collection_count"] == 1

        accepted, reason, collection = state.record_collection(
            participant_id=4,
            coin_id="target-1",
            x=event.x,
            y=event.y,
            receive_time=event.receive_time,
        )
        assert accepted
        assert collection["coin_id"] == "target-1"
        assert state.participant_result(4)["coin_bonus"] == COIN_BONUS
        snapshot = state.state_snapshot_payload(3)
        assert "players" not in snapshot
        assert "reward_targets" not in snapshot["params"]["world"]
        assert "reward_events" not in snapshot
        assert "storm_points" not in snapshot
        assert "chaser_tracks" not in snapshot
        assert "warnings" not in snapshot
        assert "reward_events" not in snapshot["params"]["world"]
        assert "storm_points" not in snapshot["params"]["world"]
        assert "chaser_tracks" not in snapshot["params"]["world"]
        assert "warnings" not in snapshot["params"]["world"]

    @staticmethod
    def test_session_world_loader():
        with tempfile.TemporaryDirectory() as tmp:
            storms_csv = os.path.join(tmp, "storms.csv")
            warnings_csv = os.path.join(tmp, "warnings.csv")
            tornadoes_csv = os.path.join(tmp, "tornadoes.csv")
            spotters_csv = os.path.join(tmp, "spotters.csv")
            with open(storms_csv, "w", encoding="utf-8") as handle:
                handle.write(
                    "track_id,radar,storm_id,valid_unix,lat,lon,vil,max_dbz,mesh,cell_speed\n"
                    "s1,TEST,s1,1000,35.0,-97.0,42,55,1.2,35\n"
                    "s1,TEST,s1,1060,35.1,-96.9,50,60,1.4,40\n"
                )
            warning_feature = {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-97.1, 34.9],
                            [-96.8, 34.9],
                            [-96.8, 35.2],
                            [-97.1, 35.2],
                            [-97.1, 34.9],
                        ]
                    ],
                },
                "properties": {
                    "_map_py": {
                        "event": "Tornado Warning",
                        "effective": "1970-01-01T00:16:20Z",
                        "expires": "1970-01-01T00:18:20Z",
                    }
                },
            }
            with open(warnings_csv, "w", encoding="utf-8") as handle:
                handle.write("raw_feature_json\n")
                handle.write(json.dumps(warning_feature).replace('"', '""').join(['"', '"']))
                handle.write("\n")
            tornado_feature = {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-96.95, 35.05]},
                "properties": {"valid": "1970-01-01T00:17:00Z", "mag": "EF1"},
            }
            with open(tornadoes_csv, "w", encoding="utf-8") as handle:
                handle.write("feature_json\n")
                handle.write(json.dumps(tornado_feature).replace('"', '""').join(['"', '"']))
                handle.write("\n")
            with open(spotters_csv, "w", encoding="utf-8") as handle:
                handle.write("id,timestamp,unix_seconds,name,lat,lon,active\n")
                handle.write("1,1970-01-01T00:16:01Z,961,Spotter One,35.02,-96.98,1\n")
                handle.write("2,1970-01-01T00:16:02Z,962,Spotter Two,35.08,-96.92,1\n")
                handle.write("3,1970-01-01T00:16:41Z,1001,Spotter One,35.03,-96.97,1\n")
                handle.write("4,1970-01-01T00:16:50Z,1010,Spotter One,35.04,-96.96,1\n")
            world = build_world_from_session_dir(
                tmp,
                canvas_size=CANVAS_SIZE,
                trial_seconds=TRIAL_SECONDS,
                coin_radius=COIN_RADIUS,
                coin_bonus=COIN_BONUS,
            )
        assert {point[4] for point in world["storm_points"]} >= {42, 50}
        assert world["warnings"][0]["polygons"][0][0][0] >= 0
        assert world["reward_events"][0]["start_ms"] == world["reward_targets"][0]["start_ms"]
        assert world["reward_targets"][0]["start_ms"] >= 0
        assert world["reward_targets"][0]["end_ms"] <= TRIAL_SECONDS * 1000
        assert world["reward_targets"][0]["radius_miles"] == 10
        assert world["reward_targets"][0]["radius"] == round(
            world["projection"]["px_per_mile"] * 10, 3
        )
        assert world["chaser_track_count"] == 1
        assert world["chaser_tracks"][0]["name"] == "Spotter One"
        assert len(world["chaser_tracks"][0]["points"]) == 2
        assert world["chaser_tracks"][0]["points"][0][3] == 1001000
        assert 0 <= world["chaser_tracks"][0]["points"][0][1] <= CANVAS_SIZE
        assert 0 <= world["chaser_tracks"][0]["points"][0][2] <= CANVAS_SIZE
        assert world["debug_storm_fraction"] == 0.1
        assert world["rendered_storm_count"] <= world["storm_count"]
        assert world["storm_min_visible_game_ms"] == 0
        assert [point["name"] for point in world["spawn_points"]] == [
            "Spotter One",
            "Spotter Two",
        ]
        assert all(0 <= point["x"] <= CANVAS_SIZE for point in world["spawn_points"])
        assert all(0 <= point["y"] <= CANVAS_SIZE for point in world["spawn_points"])

    @staticmethod
    def test_websocket_event_authorization():
        world = Exp._test_world()
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
        assert service.accepts_event(event.model_copy(update={"session_id": "bad"}))
        ready_event = CanvasGameService.ReadyEvent(
            type=READY_EVENT,
            session_id="bad",
            page_uuid="current-page",
        )
        assert not service.accepts_event(ready_event)

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

    @staticmethod
    def test_canvas_template_config_initialization():
        template_dir = os.path.join(os.path.dirname(__file__), "templates")
        env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html"]),
        )
        template = env.get_template("shared_canvas.html")
        config = {
            "channel": CANVAS_WS_CHANNEL,
            "immediate": CANVAS_WS_IMMEDIATE,
            "tolerance": CANVAS_WS_TOLERANCE,
            "session_id": "shared_canvas:group:1",
            "participant_id": 1,
            "group_id": 1,
            "role": "Player 1",
            "world_id": WORLD_DEFINITIONS[0]["world_id"],
            "canvas_size": CANVAS_SIZE,
            "canvas_width": CANVAS_RENDER_WIDTH,
            "canvas_height": CANVAS_RENDER_HEIGHT,
            "trial_seconds": TRIAL_SECONDS,
            "send_interval_ms": SEND_INTERVAL_MS,
            "draw_interval_ms": DRAW_INTERVAL_MS,
            "player_radius": PLAYER_RADIUS,
            "initial_players": CanvasGameState.initial_players([1], WORLD_DEFINITIONS[0]),
            "initial_player": CanvasGameState.initial_players([1], WORLD_DEFINITIONS[0])["1"],
            "coin_radius": COIN_RADIUS,
            "coin_bonus": COIN_BONUS,
            "max_player_speed": WORLD_DEFINITIONS[0]["max_player_speed"],
            "speed_limit_mph": WORLD_DEFINITIONS[0].get("speed_limit_mph", 30),
            "projection": WORLD_DEFINITIONS[0]["projection"],
            "storm_points": WORLD_DEFINITIONS[0]["storm_points"],
            "chaser_tracks": WORLD_DEFINITIONS[0].get("chaser_tracks", [])[:1],
            "storm_chaser_track_count": 1,
            "chaser_track_visible_ms": CHASER_TRACK_VISIBLE_MS,
            "warnings": WORLD_DEFINITIONS[0]["warnings"],
            "reward_events": WORLD_DEFINITIONS[0]["reward_events"],
            "timing": {
                "game_start_ms": 0,
                "game_end_ms": TRIAL_SECONDS * 1000,
                "raw_time_min_ms": WORLD_DEFINITIONS[0]["raw_time_min_ms"],
                "raw_time_max_ms": WORLD_DEFINITIONS[0]["raw_time_max_ms"],
            },
        }
        html = template.module.shared_canvas_control(SimpleNamespace(game_config=config))
        config_line = next(
            line.strip()
            for line in html.splitlines()
            if line.strip().startswith("var cfg =")
        )
        assert config_line.startswith("var cfg = {")
        assert "&#" not in config_line
        assert '"channel": "shared_canvas_live"' in config_line
        assert '"immediate": true' in config_line
        assert 'tabindex="0"' in html
        assert 'aria-label="Shared canvas arrow-key navigation area"' in html
        assert 'window.addEventListener("keydown", handleArrowKeyDown, true);' in html
        assert 'window.addEventListener("keyup", handleArrowKeyUp, true);' in html
        assert 'canvas.addEventListener("click", focusCanvas);' in html
        assert "if (!wasPressed) {" in html
        assert "drawPotential" in html
        assert "drawChaserTracks" in html
        assert "rawTimeForGameTime" in html
        assert "drawRewardEvents" in html
        assert "game_time_ms" in html

    def test_canvas_websocket_contracts(self):
        self.test_websocket_event_parsing()
        self.test_canvas_state_transitions()
        self.test_session_world_loader()
        self.test_websocket_event_authorization()
        self.test_server_event_serialization()
        self.test_canvas_template_config_initialization()

    def test_serial_run_bots(self, bots: List[BotDriver]):
        self.test_canvas_websocket_contracts()

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