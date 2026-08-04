"""sqlite_repository.py — Phase 6 (6.3): transactional persistence + idempotent decision IDs (6.2)."""
from __future__ import annotations
import json,sqlite3,threading
from datetime import datetime,timezone,timedelta
IST=timezone(timedelta(hours=5,minutes=30))
SCHEMA="""
CREATE TABLE IF NOT EXISTS decisions (decision_id TEXT PRIMARY KEY, symbol TEXT, strategy TEXT, side TEXT, state TEXT, config_version TEXT, model_version TEXT, payload TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS positions (decision_id TEXT PRIMARY KEY, symbol TEXT, side TEXT, entry REAL, sl REAL, target REAL, qty INTEGER, strategy TEXT, opened_at TEXT, closed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, event TEXT, decision_id TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, decision_id TEXT, model_version TEXT, status TEXT, payload TEXT);
"""
class SQLiteRepository:
    def __init__(self,path="./state.db"):
        self.path=path; self._lock=threading.Lock()
        self._c=sqlite3.connect(path,check_same_thread=False); self._c.row_factory=sqlite3.Row
        with self._c: self._c.executescript(SCHEMA)
    def _now(self): return datetime.now(IST).isoformat()
    def decision_exists(self,did): return self._c.execute("SELECT 1 FROM decisions WHERE decision_id=?",(did,)).fetchone() is not None
    def create_decision(self,did,symbol,strategy,side,config_version="",model_version="",payload=None,state="SIGNAL_CREATED"):
        with self._lock,self._c:
            if self.decision_exists(did): return False
            self._c.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?)",
                (did,symbol,strategy,side,state,config_version,model_version,json.dumps(payload or {}),self._now()))
            return True
    def set_decision_state(self,did,state):
        with self._lock,self._c: self._c.execute("UPDATE decisions SET state=? WHERE decision_id=?",(state,did))
    def save_position(self,pos):
        with self._lock,self._c:
            self._c.execute("INSERT OR REPLACE INTO positions (decision_id,symbol,side,entry,sl,target,qty,strategy,opened_at,closed) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pos["decision_id"],pos["symbol"],pos["side"],pos["entry"],pos["sl"],pos["target"],pos["qty"],pos.get("strategy",""),pos.get("opened_at",self._now()),int(pos.get("closed",0))))
    def load_positions(self,open_only=True):
        q="SELECT * FROM positions"+(" WHERE closed=0" if open_only else "")
        return [dict(r) for r in self._c.execute(q).fetchall()]
    def close_position(self,did):
        with self._lock,self._c: self._c.execute("UPDATE positions SET closed=1 WHERE decision_id=?",(did,))
    def record_event(self,event,decision_id="",payload=None):
        with self._lock,self._c: self._c.execute("INSERT INTO events (ts,event,decision_id,payload) VALUES (?,?,?,?)",(self._now(),event,decision_id,json.dumps(payload or {})))
    def record_prediction(self,did,mv,status,payload=None):
        with self._lock,self._c: self._c.execute("INSERT INTO predictions (ts,decision_id,model_version,status,payload) VALUES (?,?,?,?,?)",(self._now(),did,mv,status,json.dumps(payload or {})))
    def backup(self,dest):
        with self._lock:
            b=sqlite3.connect(dest)
            with b: self._c.backup(b)
            b.close()
    def close(self): self._c.close()
