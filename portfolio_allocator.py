"""portfolio_allocator.py — Phase 9 (9.5): allocator weight-change limiter."""
from __future__ import annotations
W_FLOOR=0.4; W_CEIL=1.5; MAX_STEP=0.15
def clamp(w): return max(W_FLOOR,min(W_CEIL,w))
def limited_update(old_w,target_w,max_step=MAX_STEP):
    old_w=clamp(old_w); delta=target_w-old_w
    if delta>max_step: delta=max_step
    elif delta<-max_step: delta=-max_step
    return round(clamp(old_w+delta),3)
