"""rr_features.py — shared train==infer features (unchanged from repo baseline)."""
from __future__ import annotations
from typing import Dict, Optional
import numpy as np, pandas as pd
BASE_FEATURES=["atr_frac","rsi","vwap_dist","ema_dist","adx","vol_ratio","ret1","ret3","range_pct","gap_pct","tod","body_frac"]
FEATURES=BASE_FEATURES+["sl_mult","tgt_mult"]
def wilder_atr(h,l,c,period=14):
    prev=np.concatenate([[c[0]],c[:-1]]); tr=np.maximum.reduce([h-l,np.abs(h-prev),np.abs(l-prev)]); atr=np.zeros_like(tr)
    if len(tr)<period: return atr
    atr[period-1]=tr[:period].mean()
    for i in range(period,len(tr)): atr[i]=(atr[i-1]*(period-1)+tr[i])/period
    return atr
def rsi(c,period=14):
    d=np.diff(c,prepend=c[0]); up=np.where(d>0,d,0.0); dn=np.where(d<0,-d,0.0); ru=np.zeros_like(c); rd=np.zeros_like(c)
    if len(c)<=period: return np.full_like(c,50.0)
    ru[period]=up[1:period+1].mean(); rd[period]=dn[1:period+1].mean()
    for i in range(period+1,len(c)): ru[i]=(ru[i-1]*(period-1)+up[i])/period; rd[i]=(rd[i-1]*(period-1)+dn[i])/period
    rs=np.divide(ru,np.where(rd==0,1e-9,rd)); return 100.0-100.0/(1.0+rs)
def ema(c,span):
    a=2.0/(span+1); out=np.zeros_like(c); out[0]=c[0]
    for i in range(1,len(c)): out[i]=a*c[i]+(1-a)*out[i-1]
    return out
def adx(h,l,c,period=14):
    if len(c)<period*2: return np.full_like(c,20.0)
    ph=np.concatenate([[h[0]],h[:-1]]); pl=np.concatenate([[l[0]],l[:-1]]); pc=np.concatenate([[c[0]],c[:-1]])
    tr=np.maximum.reduce([h-l,np.abs(h-pc),np.abs(l-pc)]); pdm=np.where((h-ph)>(pl-l),np.maximum(h-ph,0),0.0); ndm=np.where((pl-l)>(h-ph),np.maximum(pl-l,0),0.0)
    atr=np.zeros_like(tr); pd_=np.zeros_like(tr); nd_=np.zeros_like(tr)
    atr[period-1]=tr[:period].mean(); pd_[period-1]=pdm[:period].mean(); nd_[period-1]=ndm[:period].mean()
    for i in range(period,len(tr)):
        atr[i]=(atr[i-1]*(period-1)+tr[i])/period; pd_[i]=(pd_[i-1]*(period-1)+pdm[i])/period; nd_[i]=(nd_[i-1]*(period-1)+ndm[i])/period
    with np.errstate(divide="ignore",invalid="ignore"):
        pdi=100*pd_/np.where(atr>0,atr,1e-9); ndi=100*nd_/np.where(atr>0,atr,1e-9); dx=100*np.abs(pdi-ndi)/np.where(pdi+ndi>0,pdi+ndi,1e-9)
    ax=np.zeros_like(dx); start=2*period-1
    if start>=len(dx): return np.full_like(c,20.0)
    ax[start]=dx[period:start+1].mean()
    for i in range(start+1,len(dx)): ax[i]=(ax[i-1]*(period-1)+dx[i])/period
    return ax
def _tod(df):
    c={col.lower():col for col in df.columns}; tc=next((c[k] for k in ("timestamp","ts","datetime","date","time") if k in c),None)
    if tc is not None:
        t=pd.to_datetime(df[tc],errors="coerce",utc=True)
        try: tk=t.dt.tz_convert("Asia/Kolkata"); return (tk.dt.hour+tk.dt.minute/60.0).values
        except Exception: pass
    n=len(df); return 9.25+((np.arange(n)%75)*5)/60.0
def feature_frame(df):
    c={col.lower():col for col in df.columns}
    h=df[c["high"]].values.astype(float); l=df[c["low"]].values.astype(float); cl=df[c["close"]].values.astype(float); o=df[c["open"]].values.astype(float)
    v=df[c["volume"]].values.astype(float) if "volume" in c else np.zeros_like(cl)
    _atr=wilder_atr(h,l,cl); _rsi=rsi(cl); _ema=ema(cl,20); _adx=adx(h,l,cl); tp=(h+l+cl)/3.0
    cv=np.cumsum(v); ctpv=np.cumsum(tp*v); vwap=np.divide(ctpv,np.where(cv==0,1e-9,cv)); vma=pd.Series(v).rolling(20).mean().values
    f=pd.DataFrame(index=df.index); f["atr"]=_atr; f["atr_frac"]=_atr/np.where(cl>0,cl,1e-9); f["rsi"]=_rsi
    f["vwap_dist"]=(cl-vwap)/np.where(cl>0,cl,1e-9); f["ema_dist"]=(cl-_ema)/np.where(cl>0,cl,1e-9); f["adx"]=_adx
    f["vol_ratio"]=v/np.where(vma>0,vma,np.nan); f["ret1"]=pd.Series(cl).pct_change(1).values; f["ret3"]=pd.Series(cl).pct_change(3).values
    f["range_pct"]=(h-l)/np.where(cl>0,cl,1e-9); f["gap_pct"]=(o-np.concatenate([[cl[0]],cl[:-1]]))/np.where(cl>0,cl,1e-9)
    f["tod"]=_tod(df); f["body_frac"]=np.abs(cl-o)/np.where((h-l)>0,(h-l),1e-9); f["close"]=cl; return f
def latest_features(df):
    if df is None or len(df)<20: return None
    f=feature_frame(df); row=f.iloc[-1]; out={k:float(row[k]) for k in BASE_FEATURES}
    for k,x in out.items():
        if not np.isfinite(x): out[k]=0.0
    return out
