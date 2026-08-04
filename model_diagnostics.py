"""model_diagnostics.py — Phase 1 (1.3+1.4): RR coverage + Telegram diagnostics. Stdlib."""
from __future__ import annotations
import os,threading
from collections import Counter
from datetime import datetime,timezone,timedelta
import reason_codes as RC
IST=timezone(timedelta(hours=5,minutes=30))
class _Coverage:
    def __init__(self): self._lock=threading.Lock(); self._day=self._t(); self.reset()
    @staticmethod
    def _t(): return datetime.now(IST).strftime("%Y-%m-%d")
    def reset(self):
        self.signals_evaluated=self.attempted=self.accepted=self.rejected=self.fallbacks=0
        self.reject_reasons=Counter(); self.last_accept_symbol=None; self.last_accept_time=None
    def _roll(self):
        t=self._t()
        if t!=self._day: self._day=t; self.reset()
    def on_signal(self):
        with self._lock: self._roll(); self.signals_evaluated+=1
    def on_attempt(self):
        with self._lock: self._roll(); self.attempted+=1
    def on_accept(self,sym):
        with self._lock: self._roll(); self.accepted+=1; self.last_accept_symbol=sym; self.last_accept_time=datetime.now(IST).strftime("%H:%M")
    def on_reject(self,s):
        with self._lock: self._roll(); self.rejected+=1; self.reject_reasons[s]+=1
    def on_fallback(self,s):
        with self._lock:
            self._roll(); self.fallbacks+=1
            if s: self.reject_reasons[s]+=1
    def snapshot(self):
        with self._lock:
            self._roll(); top=self.reject_reasons.most_common(1)
            return {"day":self._day,"signals_evaluated":self.signals_evaluated,"attempted":self.attempted,
            "accepted":self.accepted,"rejected":self.rejected,"fallbacks":self.fallbacks,
            "top_reject_reason":top[0][0] if top else None,"last_accept_symbol":self.last_accept_symbol,
            "last_accept_time":self.last_accept_time,
            "acceptance_rate":round(self.accepted/self.attempted,3) if self.attempted else None}
COVERAGE=_Coverage()
def mlstatus_text():
    s=COVERAGE.snapshot(); loaded=enabled=False; schema="UNKNOWN"; mv="-"
    try:
        import rr_predictor
        i=rr_predictor.diagnostics() if hasattr(rr_predictor,"diagnostics") else {}
        loaded=bool(i.get("loaded")); enabled=bool(i.get("enabled")); schema=i.get("schema","UNKNOWN"); mv=i.get("model_version","-")
    except Exception: pass
    return "\n".join(["🔬 <b>RR Predictor</b>",f"Loaded: {'YES' if loaded else 'NO'}",f"Enabled: {'YES' if enabled else 'NO'}",
    f"Schema: {schema}",f"Signals evaluated: {s['signals_evaluated']}",f"Predictions today: {s['attempted']}",
    f"Accepted: {s['accepted']}",f"Fallbacks: {s['fallbacks']}",f"Top fallback reason: {s['top_reject_reason'] or '-'}",
    f"Last accepted: {(s['last_accept_symbol']+' '+s['last_accept_time']) if s['last_accept_symbol'] else '-'}",f"Model: {mv}"])
def models_text():
    base=os.getenv("REPO_BASE_DIR","."); out=["📦 <b>Models</b>"]
    try:
        import json
        m=json.load(open(os.getenv("RR_META_PATH",os.path.join(base,"rr_meta.json"))))
        out.append(f"RR: {m.get('objective','?')} · skill={m.get('skill_vs_mean','?')} · auc={m.get('auc','?')} · H={m.get('horizon_bars','?')}b")
    except Exception: out.append("RR: (no rr_meta.json)")
    return "\n".join(out)
def decision_text(symbol):
    try:
        import decision_audit; d=decision_audit.last_for_symbol(symbol)
    except Exception: d=None
    if not d: return f"No decision recorded today for {symbol.upper()}."
    codes=", ".join(d.get("reason_codes",[])) or "-"
    return (f"🧾 <b>{d['decision_id']}</b>\nstrategy: {d['strategy']} · side: {d['side']}\n"
            f"baseline exit: {d['baseline_exit_source']} → final: {d['final_exit_source']}\n"
            f"RR: {d['rr_model_status']} · Kronos: {d['kronos_status']}\n"
            f"allocator: {d['allocator_mode']} · risk: {d['risk_weight']}x\n"
            f"SL {d.get('stop_price')} / TGT {d.get('target_price')} · RR {d.get('expected_rr')}\n"
            f"model: {d.get('model_version') or '-'} · cfg: {d.get('config_version') or '-'}\nreasons: {codes}")
def config_text(base_dir=None):
    try:
        import config_report; return "<pre>"+config_report.format_report(base_dir=base_dir)+"</pre>"
    except Exception as e: return f"config_report unavailable: {e}"
