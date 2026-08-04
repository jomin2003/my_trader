"""walk_forward.py — Phase 8: chronological walk-forward with purge+embargo."""
from __future__ import annotations
from dataclasses import dataclass
@dataclass(frozen=True)
class Fold:
    train_start:int; train_end:int; val_start:int; val_end:int
def walk_forward_folds(n,n_folds=5,embargo=12):
    if n_folds<2 or n<n_folds*(embargo+10): return []
    fs=n//(n_folds+1); folds=[]
    for k in range(1,n_folds+1):
        te=fs*k; vs=te+embargo; ve=min(vs+fs,n)
        if vs>=n or ve<=vs: break
        folds.append(Fold(0,te,vs,ve))
    return folds
def has_no_leakage(fold,embargo): return (fold.val_start-fold.train_end)>=embargo
