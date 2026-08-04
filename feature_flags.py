"""feature_flags.py — Phase 3: typed flag accessors over loaded Settings."""
from __future__ import annotations
from config_loader import load_settings
def _s(): return load_settings()
def audit_enabled(): return _s().application.audit_enabled
def exit_v2_enabled(): return _s().application.exit_policy_v2_enabled
def rr_enabled(): return _s().model.rr_gate_enabled
def kronos_enabled(): return _s().model.kronos_enabled
def vol_gate_enabled(): return _s().model.vol_gate_enabled
def auto_trade_enabled(): return _s().execution.auto_trade_enabled
def allocator_mode(): return _s().model.alloc_mode
def config_version(): return _s().version()
def mode(): return _s().application.mode
