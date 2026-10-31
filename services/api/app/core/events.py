from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict, deque
from typing import Any
from fastapi import WebSocket

from app.core.precision import utc_iso
from app.core.security import redact_sensitive_data


CHANNELS = (
    "dashboard",
    "market",
    "scanner",
    "agents",
    "orders",
    "positions",
    "risk",
    "system",
)


class EventBus:
    """
    Manages WebSocket subscriptions and recent event replay buffer across all 8 channels
    defined in Section 6.6:
    /ws/dashboard, /ws/market, /ws/scanner, /ws/agents, /ws/orders, /ws/positions, /ws/risk, /ws/system
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[WebSocket]] = defaultdict(set)
        self._recent_events: dict[str, deque[dict[str, Any]]] = {
            ch: deque(maxlen=100) for ch in CHANNELS
        }
        self._kv_cache: dict[str, Any] = {}

    def build_envelope(
        self,
        event_type: str,
        data: Any,
        source: str = "system",
    ) -> dict[str, Any]:
        return {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "occurred_at": utc_iso(),
            "source": source,
            "data": redact_sensitive_data(data),
        }

    async def subscribe(self, channel: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._subscribers[channel].add(websocket)
        # Send connection acknowledgment envelope
        ack = self.build_envelope(
            event_type=f"{channel}.connected",
            data={"channel": channel, " buffered_events": len(self._recent_events.get(channel, []))},
            source="system",
        )
        await websocket.send_json(ack)

    def unsubscribe(self, channel: str, websocket: WebSocket) -> None:
        self._subscribers[channel].discard(websocket)

    async def publish(
        self,
        channel: str,
        event_type: str,
        data: Any,
        source: str = "system",
        also_dashboard: bool = True,
    ) -> dict[str, Any]:
        envelope = self.build_envelope(event_type=event_type, data=data, source=source)
        targets = [channel]
        if also_dashboard and channel != "dashboard":
            targets.append("dashboard")

        for ch in targets:
            if ch in self._recent_events:
                self._recent_events[ch].append(envelope)
            dead: list[WebSocket] = []
            for ws in list(self._subscribers.get(ch, set())):
                try:
                    await ws.send_json(envelope)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._subscribers[ch].discard(ws)
        return envelope

    def publish_sync(
        self,
        channel: str,
        event_type: str,
        data: Any,
        source: str = "system",
        also_dashboard: bool = True,
    ) -> dict[str, Any]:
        envelope = self.build_envelope(event_type=event_type, data=data, source=source)
        targets = [channel]
        if also_dashboard and channel != "dashboard":
            targets.append("dashboard")
        for ch in targets:
            if ch in self._recent_events:
                self._recent_events[ch].append(envelope)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.publish(channel, event_type, data, source, also_dashboard))
        except RuntimeError:
            pass
        return envelope

    def get_recent_events(self, channel: str, limit: int = 25) -> list[dict[str, Any]]:
        items = list(self._recent_events.get(channel, []))
        return items[-limit:]

    def cache_set(self, key: str, value: Any) -> None:
        self._kv_cache[key] = value

    def cache_get(self, key: str, default: Any = None) -> Any:
        return self._kv_cache.get(key, default)

    def status(self) -> dict[str, Any]:
        return {
            "backend": "in_process_event_bus_with_redis_adapter",
            "channels": list(CHANNELS),
            "active_subscribers": {ch: len(subs) for ch, subs in self._subscribers.items()},
            "cached_keys": len(self._kv_cache),
        }


event_bus = EventBus()
