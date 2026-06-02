#!/usr/bin/env python3
"""
Out-of-sample validation of the predictive model against collected training data.

Method: PREQUENTIAL (predict-then-train) walk-forward over predictor_data.jsonl in time
order. Every prediction is made BEFORE the model has seen that sample, so all metrics are
genuinely out-of-sample. Reports calibration, discrimination (vs the raw combined_score
heuristic), regressor accuracy, EV-sizing efficiency, a gate sweep, and a parameter sweep.
"""
import json, math, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load_config
from modules.predictor import Predictor, FEATURES, _N

ROWS = [json.loads(l) for l in open('predictor_data.jsonl')]
ROWS = [r for r in ROWS if r.get('ret') is not None]   # barrier rows (have realized return)
N = len(ROWS)
base_rate = sum(r['y'] for r in ROWS) / N

def fresh(cfg):
    p = Predictor(cfg); p._persist = True   # persist flag irrelevant; we monkeypatch save
    p._save = lambda: None; p._append_data = lambda *a, **k: None
    p.w=np.zeros(_N); p.b=0.0; p.wr=np.zeros(_N); p.br=0.0
    p.mean=np.zeros(_N); p.M2=np.zeros(_N); p.count=0
    p.n_trained=0; p.n_reg=0; p.wins=0; p._watch={}; p._pending={}
    return p

def prequential(cfg):
    """Return per-sample out-of-sample predictions."""
    p = fresh(cfg)
    out = []
    for r in ROWS:
        f = r['f']; y = float(r['y']); ret = float(r['ret'])
        pr = p.predict_features(dict(f))
        out.append({'p': pr['p_win'], 'edge': pr['expected_edge'], 'y': y, 'ret': ret,
                    'cs': f.get('combined_score', 50.0),
                    'size': p.size_multiplier(pr['p_win'], pr['expected_edge'])})
        p.train(f, y, ret)
    return out

def auc(scores, ys):
    pos = [s for s, y in zip(scores, ys) if y > 0.5]
    neg = [s for s, y in zip(scores, ys) if y <= 0.5]
    if not pos or not neg: return float('nan')
    wins = sum((1.0 if a > b else 0.5 if a == b else 0.0) for a in pos for b in neg)
    return wins / (len(pos) * len(neg))

def brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)

def pearson(a, b):
    a = np.array(a); b = np.array(b)
    if a.std() < 1e-9 or b.std() < 1e-9: return float('nan')
    return float(np.corrcoef(a, b)[0, 1])

cfg = load_config()
res = prequential(cfg)
ps   = [r['p'] for r in res]; ys = [r['y'] for r in res]
edge = [r['edge'] for r in res]; rets = [r['ret'] for r in res]
cs   = [r['cs'] for r in res]; sz = [r['size'] for r in res]

print("="*64)
print("  PREDICTIVE MODEL — OUT-OF-SAMPLE VALIDATION")
print("  samples=%d (barrier-labeled) | base win rate=%.1f%%" % (N, 100*base_rate))
print("  params: blend_k=%.0f l2=%.3f lr=%.3f lr_reg=%.3f" % (
    cfg.get('predictor_blend_k',120), cfg.get('predictor_l2',0.008),
    cfg.get('predictor_lr',0.05), cfg.get('predictor_lr_reg',0.02)))
print("="*64)

print("\n[1] CALIBRATION (lower Brier = better; baseline = always predict base rate)")
print("    model Brier         : %.4f" % brier(ps, ys))
print("    base-rate baseline  : %.4f" % brier([base_rate]*N, ys))
# reliability bins
print("    reliability (pred bucket -> actual win rate):")
for lo,hi in [(0,0.2),(0.2,0.3),(0.3,0.5),(0.5,1.01)]:
    idx=[i for i in range(N) if lo<=ps[i]<hi]
    if idx: print("      pred[%.2f-%.2f]: n=%2d  actual win=%.0f%%  mean pred=%.2f"%(
        lo,hi,len(idx),100*sum(ys[i] for i in idx)/len(idx),sum(ps[i] for i in idx)/len(idx)))

print("\n[2] DISCRIMINATION — does it rank winners above losers? (AUC; 0.5=coin flip)")
print("    model P(win) AUC          : %.3f" % auc(ps, ys))
print("    raw combined_score AUC    : %.3f   <- the old heuristic ranker" % auc(cs, ys))
print("    (model > 0.5 and > heuristic ⇒ the learned model adds real ranking signal)")

print("\n[3] REGRESSOR — predicted edge vs realized return")
print("    corr(pred edge, realized ret) : %+.3f" % pearson(edge, rets))
sign_acc = sum(1 for e,r in zip(edge,rets) if (e>0)==(r>0))/N
print("    edge sign accuracy            : %.0f%%" % (100*sign_acc))

print("\n[4] EV-SIZING — return on deployed capital (flat vs model-sized)")
flat = sum(rets)/N
evw  = sum(s*r for s,r in zip(sz,rets))/sum(sz)
print("    flat (equal size)        : %+.2f%% avg return/trade" % flat)
print("    EV-sized (capital-wtd)   : %+.2f%% return/deployed-$  (%s)" % (
    evw, "BETTER" if evw>flat else "worse"))

print("\n[5] GATE SWEEP — if we only took trades with P(win) >= threshold")
for th in [0.0,0.20,0.25,0.30,0.35]:
    idx=[i for i in range(N) if ps[i]>=th]
    if idx:
        wr=100*sum(ys[i] for i in idx)/len(idx); mr=sum(rets[i] for i in idx)/len(idx)
        print("    p>=%.2f : kept %2d/%d  win=%.0f%%  mean ret=%+.2f%%"%(th,len(idx),N,wr,mr))

print("\n[6] PARAMETER SWEEP (out-of-sample Brier / AUC)")
print("    blend_k   l2      Brier    AUC")
for bk in [60,120,200]:
    for l2 in [0.004,0.008,0.02]:
        c=dict(cfg); c['predictor_blend_k']=bk; c['predictor_l2']=l2
        rr=prequential(c)
        pp=[x['p'] for x in rr]; yy=[x['y'] for x in rr]
        print("    %-8d  %-6.3f  %.4f   %.3f"%(bk,l2,brier(pp,yy),auc(pp,yy)))
print("="*64)
