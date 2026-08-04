"""model_manifest.py — Phase 8: artifact manifest every model must carry."""
from __future__ import annotations
from dataclasses import dataclass,field,asdict
import json
REQUIRED_FIELDS=["model_version","feature_schema_hash","training_cutoff","dataset_fingerprint","label_definition",
"horizon","strategy_compatibility","validation_metrics","dependency_versions","git_commit","promotion_state"]
PROMOTION_STATES=["candidate","shadow","paper-approved","live-approved","retired"]
@dataclass
class ModelManifest:
    model_version:str; feature_schema_hash:str; training_cutoff:str; dataset_fingerprint:str
    label_definition:str; horizon:int; strategy_compatibility:list=field(default_factory=list)
    validation_metrics:dict=field(default_factory=dict); dependency_versions:dict=field(default_factory=dict)
    git_commit:str=""; promotion_state:str="candidate"
    def to_dict(self): return asdict(self)
    def save(self,path): json.dump(self.to_dict(),open(path,"w"),indent=2)
    @classmethod
    def load(cls,path): return cls(**json.load(open(path)))
def is_complete(m):
    missing=[f for f in REQUIRED_FIELDS if f not in m or m[f] in (None,"",[],{})]
    return (len(missing)==0,missing)
