"""Local compatibility layer for PsyNet's proposed websocket service API.

This mirrors the architecture in PsyNet MR !1127 closely so the experiment can
be migrated to ``psynet.websocket`` by swapping imports once that branch lands.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar, Literal, Optional, Type, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import PydanticUndefined

from psynet.timeline import WebSocketElt
from psynet.utils import get_logger


logger = get_logger()

# TODO: Add private WebSocket delivery in a follow-up PR.
# Dallinger currently relays every Redis message on a channel to every browser
# socket subscribed to that channel, so client-side ``target`` filtering is not
# a privacy boundary. A proper solution should split browser publish and
# subscribe channels, let the experiment subscribe to command channels, and
# let services publish server events to participant-specific private receive
# channels. Private channel access should use unguessable names or signed
# channel-authorization tokens, and PsyNet can then expose a helper such as
# ``publish_to_participant(participant_id, event)``.


class ClientWebSocketEvent(BaseModel):
    """A browser-authored event authorized against the current PsyNet page."""

    model_config = ConfigDict(extra="ignore", strict=True)

    type: str = Field(min_length=1)
    page_uuid: str = Field(min_length=1)
    receive_time: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        exclude=True,
    )

    def with_receive_time(self, receive_time):
        """Return a copy stamped with the server receive time."""
        if receive_time is None:
            receive_time = datetime.now(timezone.utc)
        elif receive_time.tzinfo is None:
            receive_time = receive_time.replace(tzinfo=timezone.utc)
        else:
            receive_time = receive_time.astimezone(timezone.utc)
        return self.model_copy(update={"receive_time": receive_time})


class ServerWebSocketEvent(BaseModel):
    """A server-authored event sent to browser WebSocket clients."""

    model_config = ConfigDict(extra="forbid", strict=True)

    def to_json(self):
        """Serialize the message for WebSocket delivery."""
        return self.model_dump_json(exclude_none=True)


@dataclass(frozen=True)
class _WebSocketHandler:
    event_model: Type[ClientWebSocketEvent]
    method_name: str


def get_websocket_event_type(event_model: Type[ClientWebSocketEvent]):
    """Return the event type declared by a WebSocket event model."""
    try:
        field = event_model.model_fields["type"]
    except KeyError as exc:
        raise ValueError(f"{event_model.__name__} must define a 'type' field.") from exc

    annotation = field.annotation
    if get_origin(annotation) is Literal:
        literal_args = get_args(annotation)
        if len(literal_args) == 1 and isinstance(literal_args[0], str):
            return literal_args[0]

    if field.default is not PydanticUndefined and isinstance(field.default, str):
        return field.default

    raise ValueError(
        f"{event_model.__name__}.type must be a single string Literal or default."
    )


def websocket_handler(
    event_model: Type[ClientWebSocketEvent], type_: Optional[str] = None
):
    """Decorate a service method as the handler for a WebSocket event model."""
    event_type = type_ or get_websocket_event_type(event_model)

    def decorate(method):
        method._psynet_websocket_event_model = event_model
        method._psynet_websocket_event_type = event_type
        return method

    return decorate


def extract_websocket_event_type(message):
    """Extract the ``type`` field from a raw WebSocket JSON message."""
    try:
        data = json.loads(message)
    except json.JSONDecodeError as exc:
        raise ValueError("WebSocket message is not valid JSON.") from exc

    if not isinstance(data, dict):
        raise ValueError("WebSocket message must be a JSON object.")

    event_type = data.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("WebSocket message must include a non-empty string type.")

    return event_type


def _extract_websocket_participant_id(message):
    """Extract a participant ID from a raw WebSocket JSON message, if present."""
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    participant_id = data.get("sender") or data.get("participant_id")
    client = data.get("client")
    if participant_id is None and isinstance(client, dict):
        participant_id = client.get("participant_id")

    if participant_id in (None, ""):
        return None

    try:
        return int(participant_id)
    except (TypeError, ValueError):
        return None


class WebSocketEventService:
    """Parse, authorize, and dispatch WebSocket events for one request context."""

    rejection_log_label = None
    _websocket_handlers: ClassVar[dict[str, _WebSocketHandler]] = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        handlers = {}
        for base in reversed(cls.__mro__[1:]):
            handlers.update(getattr(base, "_websocket_handlers", {}))

        for method_name, method in cls.__dict__.items():
            event_model = getattr(method, "_psynet_websocket_event_model", None)
            if event_model is not None:
                event_type = getattr(method, "_psynet_websocket_event_type")
                handlers[event_type] = _WebSocketHandler(event_model, method_name)

        cls._websocket_handlers = handlers

    def __init__(self, participant, experiment, channel, node=None, receive_time=None):
        self.participant = participant
        self.experiment = experiment
        self.channel = channel
        self.node = node
        self.receive_time = receive_time

    @classmethod
    def get_rejection_log_label(cls):
        """Return the label used when logging rejected WebSocket events."""
        return cls.rejection_log_label or cls.__name__

    @classmethod
    def parse_event(cls, message):
        """Parse a raw JSON message into a registered event model."""
        event_type = extract_websocket_event_type(message)
        try:
            handler = cls._websocket_handlers[event_type]
        except KeyError as exc:
            raise ValueError(
                f"No WebSocket handler registered for event type '{event_type}'."
            ) from exc
        return handler.event_model.model_validate_json(message)

    def dispatch(self, message):
        """Parse and dispatch a raw WebSocket message."""
        event = self.parse_event(message).with_receive_time(self.receive_time)
        return self.dispatch_event(event)

    def dispatch_event(self, event):
        """Dispatch a parsed WebSocket event."""
        if not self.accepts_event(event):
            return None

        handler = self._websocket_handlers[event.type]
        return getattr(self, handler.method_name)(event)

    def accepts_event(self, event):
        """Return whether an event is authorized for this participant context."""
        if event.page_uuid != self.participant.page_uuid:
            self.warn_rejected_event("stale page UUID", event)
            return False
        return True

    def publish(self, message, channel_name=None):
        """Publish a message to subscribers on this service's channel."""
        self.experiment.publish_to_subscribers(
            self.serialize_message(message), channel_name=channel_name or self.channel
        )

    @staticmethod
    def serialize_message(message):
        """Serialize a message object for WebSocket delivery."""
        if isinstance(message, str):
            return message
        if hasattr(message, "to_json"):
            return message.to_json()
        if isinstance(message, BaseModel):
            return message.model_dump_json(exclude_none=True)
        return json.dumps(message)

    def warn_rejected_event(self, reason, event=None, error=None):
        """Log a rejected WebSocket event with participant context."""
        warn_rejected_websocket_event(
            reason,
            participant=self.participant,
            channel=self.channel,
            event=event,
            error=error,
            label=self.get_rejection_log_label(),
        )


class ValidatedWebSocketElt(WebSocketElt):
    """A ``WebSocketElt`` that dispatches Pydantic-validated service events."""

    service_class = WebSocketEventService

    def resolve_participant(self, message):
        """Resolve a participant from a raw WebSocket message."""
        participant_id = _extract_websocket_participant_id(message)
        if participant_id is None:
            return None

        from psynet.participant import Participant

        return Participant.query.get(participant_id)

    def handle_message(
        self, message, channel_name, participant, node, receive_time, experiment
    ):
        """Parse, authorize, and dispatch an incoming WebSocket message."""
        if participant is None:
            participant = self.resolve_participant(message)

        if participant is None:
            try:
                event = self.service_class.parse_event(message)
            except (ValidationError, ValueError):
                return

            warn_rejected_websocket_event(
                "missing participant",
                channel=channel_name or self.channel,
                event=event,
                label=self.service_class.get_rejection_log_label(),
            )
            return

        service = self.service_class(
            participant=participant,
            experiment=experiment,
            channel=self.channel,
            node=node,
            receive_time=receive_time,
        )
        try:
            event = service.parse_event(message)
        except (ValidationError, ValueError) as err:
            service.warn_rejected_event("validation failed", error=err)
            return

        service.dispatch_event(event.with_receive_time(receive_time))


def warn_rejected_websocket_event(
    reason, *, participant=None, channel=None, event=None, error=None, label="websocket"
):
    """Log a rejected WebSocket event."""
    event_type = getattr(event, "type", None)
    logger.warning(
        "Rejected %s event: %s (participant_id=%s, channel=%s, event_type=%s, "
        "error=%s)",
        label,
        reason,
        getattr(participant, "id", None),
        channel,
        event_type,
        str(error) if error is not None else None,
    )
