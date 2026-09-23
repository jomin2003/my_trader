"""deployment_check.py — Phase 10: preflight + model checksum verification."""
from __future__ import annotations
import argparse,hashlib,os,sys
def sha256(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(65536),b""): h.update(chunk)
    return h.hexdigest()
def verify_model_checksum(base="."):
    mp=os.path.join(base,"rr_model.txt"); ef=os.path.join(base,"rr_model.sha256")
    if not os.path.exists(mp): return True,"no model (RR off)"
    if not os.path.exists(ef): return True,"no checksum file (skip)"
    expect=open(ef).read().strip().split()[0]; actual=sha256(mp)
    return (actual==expect,f"expect={expect[:12]} actual={actual[:12]}")
def preflight(base="."):
    from health_service import readiness
    rep,code=readiness(base); print("readiness:",rep)
    ok,msg=verify_model_checksum(base); print("model checksum:",msg)
    if code!=200 or not ok: print("PREFLIGHT FAILED",file=sys.stderr); return 1
    print("PREFLIGHT OK"); return 0
if __name__=="__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("--preflight",action="store_true"); a=ap.parse_args()
    sys.exit(preflight() if a.preflight else 0)
