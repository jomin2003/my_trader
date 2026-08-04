"""parquet_store.py — Phase 7 (7.1): partitioned Parquet helpers (CSV fallback)."""
from __future__ import annotations
import os
def partition_path(root,interval,symbol,year,month):
    return os.path.join(root,f"interval={interval}",f"symbol={symbol.upper()}",f"year={year}",f"month={month:02d}","bars.parquet")
def write_partition(df,root,interval,symbol,year,month):
    path=partition_path(root,interval,symbol,year,month); os.makedirs(os.path.dirname(path),exist_ok=True)
    try: df.to_parquet(path,index=False)
    except Exception:
        path=path.replace(".parquet",".csv"); df.to_csv(path,index=False)
    return path
def available():
    for m in ("pyarrow","fastparquet"):
        try: __import__(m); return True
        except Exception: pass
    return False
