"""
event_bus.py — Lightweight event bus for module decoupling.
=============================================================
Replaces direct import-based coupling between scanner → allocator,
scanner → ledger, scanner → telegram, etc.

Design:
  * Zero external deps (stdlib only)
  * Thread-safe (threading.Lock)
  * Sync handlers only
  * Persistent event log for audit trail
  * Graceful handler failure (never crashes the publisher)

Usage:
    from event_bus import on, emit, once
    
    on("signal_found", my_handler)
    emit("signal_found", data={"symbol": "RELIANCE", ...})
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("event_bus")


# --------------------------------------------------------------------------- #
# DATA STRUCTURES                                                       #
# --------------------------------------------------------------------------- #

@dataclass
class EventRecord:
    """Immutable record of an emitted event."""
    name: str
    data: dict
    timestamp: str
    source: str = "unknown"


# --------------------------------------------------------------------------- #
# EVENT BUS                                                               #
# --------------------------------------------------------------------------- #

class EventBus:
    """
    Thread-safe event bus.  Handlers are called synchronously in the
    publisher's thread.  If a handler raises, the error is logged but
    does NOT propagate — this is by design (one failed handler shouldn't
    kill the trading loop).
    """

    _instance: "EventBus | None" = None
    _lock = threading.RLock()

    def __new__(cls) -> "EventBus":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._handlers: dict[str, list[dict]] = defaultdict(list)
        self._once_handlers: dict[str, list[dict]] = defaultdict(list)
        self._event_log: list[EventRecord] = []
        self._max_log: int = 1000
        self._log_lock = threading.Lock()
        self._metrics = defaultdict(int)  # event_name -> count

    # ---- subscription -------------------------------------------------- #

    def on(self, event_name: str, handler: Callable, source: str = "unknown") -> None:
        """Subscribe to an event (persistent)."""
        with self._lock:
            self._handlers[event_name].append({
                "handler": handler,
                "source": source,
            })
        log.debug(f"Subscribed {source} to '{event_name}'")

    def once(self, event_name: str, handler: Callable, source: str = "unknown") -> None:
        """Subscribe to an event (fires once, then auto-removes)."""
        with self._lock:
            self._once_handlers[event_name].append({
                "handler": handler,
                "source": source,
            })

    def off(self, event_name: str, source: str | None = None) -> None:
        """Unsubscribe.  If source is None, removes all handlers for that event."""
        with self._lock:
            if source:
                self._handlers[event_name] = [
                    h for h in self._handlers.get(event_name, [])
                    if h["source"] != source
                ]
                self._once_handlers[event_name] = [
                    h for h in self._once_handlers.get(event_name, [])
                    if h["source"] != source
                ]
            else:
                self._handlers.pop(event_name, None)
                self._once_handlers.pop(event_name, None)

    # ---- emission ------------------------------------------------------ #

    def emit(self, event_name: str, data: dict | None = None, source: str = "unknown") -> None:
        """Emit an event to all subscribers.  Never raises."""
        data = data or {}
        record = EventRecord(event_name, data, datetime.now(timezone.utc).isoformat(), source)

        # Log the event
        with self._log_lock:
            self._event_log.append(record)
            if len(self._event_log) > self._max_log:
                self._event_log = self._event_log[-self._max_log:]

        with self._lock:
            self._metrics[event_name] += 1
            handlers = list(self._handlers.get(event_name, []))

        # Process once-handlers
        with self._lock:
            once = list(self._once_handlers.get(event_name, []))
            self._once_handlers[event_name] = []

        all_handlers = handlers + once
        for h in all_handlers:
            try:
                h["handler"](data, source)
            except Exception as e:
                log.error(f"Event handler error [{event_name}] from {h['source']}: {e}")

    # ---- queries ------------------------------------------------------- #

    def handler_count(self, event_name: str) -> int:
        """How many handlers are subscribed to this event?"""
        with self._lock:
            return len(self._handlers.get(event_name, []))

    def event_count(self, event_name: str) -> int:
        """Total times this event has been emitted."""
        with self._lock:
            return self._metrics.get(event_name, 0)

    def event_log(self, limit: int = 50) -> list[EventRecord]:
        """Recent event log (most recent first)."""
        with self._log_lock:
            return list(self._event_log[-limit:])

    def stats(self) -> dict:
        """Event bus statistics."""
        with self._lock:
            return {
                "total_events": dict(self._metrics),
                "subscriptions": {k: len(v) for k, v in self._handlers.items()},
            }

    def clear(self) -> None:
        """Remove all handlers and the event log."""
        with self._lock:
            self._handlers.clear()
            self._once_handlers.clear()
            self._metrics.clear()
        with self._log_lock:
            self._event_log.clear()


# --------------------------------------------------------------------------- #
# MODULE-LEVEL INSTANCE                                                 #
# --------------------------------------------------------------------------- #

_bus: EventBus | None = None

def get_bus() -> EventBus:
    """Get the global event bus instance."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus

def on(event_name: str, handler: Callable, source: str = "unknown") -> None:
    """Subscribe to an event."""
    get_bus().on(event_name, handler, source)

def once(event_name: str, handler: Callable, source: str = "unknown") -> None:
    """Subscribe once."""
    get_bus().once(event_name, handler, source)

def emit(event_name: str, data: dict | None = None, source: str = "unknown") -> None:
    """Emit an event."""
    get_bus().emit(event_name, data, source)

def off(event_name: str, source: str | None = None) -> None:
    """Unsubscribe."""
    get_bus().off(event_name, source)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.DEBUG)

    bus = EventBus()

    # Test subscription and emission
    calls = []
    def handler(data, source):
        calls.append((data, source))

    bus.on("test_event", handler, "test_source")
    bus.emit("test_event", {"key": "value"}, "test_source")
    assert len(calls) == 1, f"Expected 1 call, got {len(calls)}"
    assert calls[0][0]["key"] == "value"

    # Test once
    once_calls = []
    bus.once("once_event", lambda d, s: once_calls.append(True), "test")
    bus.emit("once_event", {}, "test")
    bus.emit("once_event", {}, "test")
    assert len(once_calls) == 1, f"Expected 1 once-call, got {len(once_calls)}"

    # Test error isolation
    bad_handler = lambda d, s: 1/0
    bus.on("error_test", bad_handler, "bad")
    bus.emit("error_test", {}, "good")  # Should not raise

    print("All event_bus tests passed ✓")
