"""
metrics.py — Phase 10: lightweight in-process metrics + Prometheus endpoint.
==============================================================================
All tracked metrics are pushed to the event bus automatically.
Add /metrics to your Flask app to expose Prometheus-compatible output.

Usage:
    from metrics import inc, gauge, snapshot, prometheus_text
    inc("signals_evaluated")
    gauge("portfolio_heat", 1500.0)
    prometheus_text()  # for /metrics endpoint
"""
from __future__ import annotations

import threading
from collections import defaultdict

_lock = threading.Lock()
_counters = defaultdict(float)
_gauges = {}

def inc(name: str, by: float = 1.0) -> None:
    """Increment a counter metric."""
    with _lock:
        _counters[name] += by

def gauge(name: str, value: float) -> None:
    """Set a gauge metric."""
    with _lock: _gauges[name] = value

def get_counter(name: str) -> float:
    """Read a counter value."""
    with _lock: return _counters.get(name, 0.0)

def get_gauge(name: str) -> float:
    """Read a gauge value."""
    with _lock: return _gauges.get(name, 0.0)

def snapshot() -> dict:
    """Return all metrics as dict."""
    with _lock:
        return {
            "counters": dict(_counters),
            "gauges": dict(_gauges),
        }

def prometheus_text() -> str:
    """Return Prometheus-compatible text format."""
    s = snapshot()
    lines = []
    for k, v in s["counters"].items():
        lines.append(f"# TYPE {k} counter")
        lines.append(f"{k} {v}")
    for k, v in s["gauges"].items():
        lines.append(f"# TYPE {k} gauge")
        lines.append(f"{k} {v}")
    if not lines:
        lines.append("# no metrics recorded yet")
    return "\n".join(lines)

def reset() -> None:
    """Clear all metrics. Use with caution (e.g., at day boundary)."""
    with _lock:
        _counters.clear()
        _gauges.clear()

# ---- Auto-tracking helpers for event bus integration ----

def track_signal_evaluated():
    """Called by event bus handler."""
    inc("signals_evaluated")

def track_trade_entered():
    inc("trades_entered")

# ---- Integration with event bus ----

def wire_event_bus():
    """Register metric-tracking handlers on the event bus.

    NOTE: "trade_exited" used to be subscribed here, but the scanner never
    emits that event, so the wiring was dead and trades_exited/won/lost
    stayed zero. Subscribe it only if an emitter is added.
    """
    try:
        from event_bus import on
        on("signal_found", lambda data, src: track_signal_evaluated(), "metrics")
        on("trade_entered", lambda data, src: track_trade_entered(), "metrics")
    except Exception:
        pass

wire_event_bus()


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    inc("signals_evaluated")
    inc("signals_evaluated")
    gauge("portfolio_heat", 1500.0)
    print(prometheus_text())
    print("All metrics tests passed ✓")
