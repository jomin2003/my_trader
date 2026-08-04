"""model_registry.py — Phase 8: register/track models by version + promotion state."""
from __future__ import annotations
import json,os
from model_manifest import is_complete
class ModelRegistry:
    def __init__(self,path="model_registry.json"):
        self.path=path; self._d={}
        if os.path.exists(path):
            try: self._d=json.load(open(path))
            except Exception: self._d={}
    def register(self,m):
        ok,missing=is_complete(m)
        if not ok: raise ValueError(f"cannot register incomplete manifest: {missing}")
        self._d[m["model_version"]]=m; self._save(); return True
    def set_state(self,v,state): self._d[v]["promotion_state"]=state; self._save()
    def get(self,v): return self._d.get(v,{})
    def by_state(self,state): return [m for m in self._d.values() if m.get("promotion_state")==state]
    def _save(self): json.dump(self._d,open(self.path,"w"),indent=2)
