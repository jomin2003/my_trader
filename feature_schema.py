"""feature_schema.py — Phase 7 (7.5): feature schema hashing -> SCHEMA_MISMATCH guard."""
from __future__ import annotations
import hashlib,json
def schema_hash(feature_names,version="v1"):
    return "fs-"+hashlib.sha256(json.dumps({"version":version,"features":list(feature_names)}).encode()).hexdigest()[:12]
def current_hash():
    try:
        import rr_features; return schema_hash(rr_features.FEATURES)
    except Exception: return "fs-unknown"
def matches(model_hash): return bool(model_hash) and model_hash==current_hash()
