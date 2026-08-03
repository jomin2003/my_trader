"""
rr_doctor.py  —  one-shot diagnostic for "my RR model isn't predicting live"
============================================================================
Run in your bot's repo/working dir:   python rr_doctor.py
It checks every link in the chain and prints a PASS/FAIL verdict + the fix.
"""
import os, json, glob

def line(): print("-" * 60)
print("=" * 60); print(" RR PREDICTOR DIAGNOSTIC"); print("=" * 60)

ok = True

# 1) env flag ---------------------------------------------------------------
line()
en = os.environ.get("RR_GATE_ENABLED", "")
if en == "1":
    print("[1] RR_GATE_ENABLED = 1            ✅")
else:
    ok = False
    print(f"[1] RR_GATE_ENABLED = {en!r}        ❌  <-- LIKELY CAUSE")
    print("    FIX: set env RR_GATE_ENABLED=1 on Render, redeploy.")

# 2) files present ----------------------------------------------------------
line()
mp = os.environ.get("RR_MODEL_PATH", "./rr_model.txt")
jp = os.environ.get("RR_META_PATH", "./rr_meta.json")
for label, p in [("rr_model.txt", mp), ("rr_meta.json", jp)]:
    if os.path.exists(p):
        print(f"[2] {label:14s} found at {p}   ✅")
    else:
        ok = False
        print(f"[2] {label:14s} MISSING at {p}  ❌")
        print(f"    cwd={os.getcwd()}  (files here: "
              f"{[f for f in glob.glob('*.txt')+glob.glob('*.json')][:6]})")
        print("    FIX: upload it, or set RR_MODEL_PATH/RR_META_PATH to its path.")

for helper in ["rr_predictor.py", "rr_features.py"]:
    if os.path.exists(helper):
        print(f"[2] {helper:14s} present            ✅")
    else:
        ok = False
        print(f"[2] {helper:14s} MISSING            ❌  upload it.")

# 3) lightgbm importable ----------------------------------------------------
line()
try:
    import lightgbm as lgb
    print(f"[3] lightgbm import OK (v{lgb.__version__})   ✅")
except Exception as e:
    ok = False
    print(f"[3] lightgbm import FAILED: {e}   ❌")
    print("    FIX: add 'lightgbm>=4.3.0' to requirements.txt, redeploy.")

# 4) meta skill gate --------------------------------------------------------
line()
meta = {}
try:
    meta = json.load(open(jp))
    skill = meta.get("skill_vs_mean", None)
    model = meta.get("model", "?")
    print(f"[4] meta.model={model}  skill_vs_mean={skill}")
    if model != "lightgbm":
        ok = False
        print("    ❌ model != 'lightgbm' -> gate stays neutral.")
    elif skill is None or float(skill) <= float(os.environ.get("RR_MIN_SKILL", "0.0")):
        ok = False
        print(f"    ❌ skill <= RR_MIN_SKILL -> predictor REFUSES to act (by design).")
        print("    FIX: retrain until skill_vs_mean > 0, OR (to test wiring only)")
        print("         set RR_MIN_SKILL=-1 temporarily. Do NOT trade on skill<=0.")
    else:
        print("    ✅ skill passes the gate.")
except Exception as e:
    print(f"[4] could not read meta: {e}")

# 5) end-to-end predictor check --------------------------------------------
line()
try:
    os.environ.setdefault("RR_GATE_ENABLED", en or "0")
    import rr_predictor
    print(f"[5] rr_summary(): {rr_predictor.rr_summary()}")
    print(f"    enabled(): {rr_predictor.enabled()}")
    demo = {"atr_frac":0.004,"rsi":55,"vwap_dist":0.001,"ema_dist":0.002,"adx":22,
            "vol_ratio":1.4,"ret1":0.001,"ret3":0.003,"range_pct":0.006,
            "gap_pct":0.0,"tod":10.5,"body_frac":0.6}
    rec = rr_predictor.best_rr(demo, 1)
    if rec:
        print(f"    best_rr() -> {rec}   ✅ PREDICTOR IS WORKING")
    else:
        ok = False
        print("    best_rr() -> None   ❌ (blocked by one of the gates above)")
except Exception as e:
    ok = False
    print(f"[5] predictor error: {e}   ❌")

line()
print(" VERDICT:", "✅ RR should predict live." if ok else
      "❌ Fix the ❌ item(s) above — that's why it's silent.")
print("=" * 60)
