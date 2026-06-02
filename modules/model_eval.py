"""
Shared out-of-sample evaluation for the predictive model.

Prequential (predict-then-train) walk-forward over collected barrier outcomes: every
prediction is made before the model has seen that sample, so all metrics are genuinely
out-of-sample. Used by validate_predictor.py (CLI) and refine_model.py (auto-controller).
"""
import json
import os

import numpy as np

from modules.predictor import Predictor, FEATURES, _N

_DIR           = os.path.dirname(os.path.abspath(__file__))
DATA_FILE      = os.path.join(_DIR, '..', 'predictor_data.jsonl')
EXIT_DATA_FILE = os.path.join(_DIR, '..', 'predictor_exit_data.jsonl')


def load_rows(path=DATA_FILE, include_live=False):
    """Load barrier-labeled rows (those carrying a realized return) in time order.

    By default excludes 'live'-tagged rows: while the feed is delayed, live outcomes are
    biased/timing-corrupt, so the model trains and is evaluated only on delay-immune 'replay'
    data (legacy untagged rows are treated as replay)."""
    if not os.path.exists(path):
        return []
    rows = []
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get('ret') is None or 'f' not in r:
            continue
        if not include_live and r.get('src') == 'live':
            continue
        rows.append(r)
    return rows


def _fresh(cfg):
    p = Predictor(cfg)
    p._save = lambda: None
    p._append_data = lambda *a, **k: None
    p.w = np.zeros(_N); p.b = 0.0; p.wr = np.zeros(_N); p.br = 0.0
    p.mean = np.zeros(_N); p.M2 = np.zeros(_N); p.count = 0
    p.n_trained = 0; p.n_reg = 0; p.wins = 0; p._watch = {}; p._pending = {}
    return p


def prequential(cfg, rows):
    p = _fresh(cfg)
    out = []
    for r in rows:
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
    if not pos or not neg:
        return float('nan')
    wins = sum((1.0 if a > b else 0.5 if a == b else 0.0) for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps) if ps else float('nan')


def pearson(a, b):
    a = np.array(a, dtype=float); b = np.array(b, dtype=float)
    if len(a) < 3 or a.std() < 1e-9 or b.std() < 1e-9:
        return float('nan')
    return float(np.corrcoef(a, b)[0, 1])


def summarize(cfg, rows):
    """Full out-of-sample metric bundle for a given config."""
    res = prequential(cfg, rows)
    n = len(res)
    if n == 0:
        return {'n': 0}
    ps = [r['p'] for r in res]; ys = [r['y'] for r in res]
    edge = [r['edge'] for r in res]; rets = [r['ret'] for r in res]
    cs = [r['cs'] for r in res]; sz = [r['size'] for r in res]
    base = sum(ys) / n
    flat = sum(rets) / n
    evw  = (sum(s * r for s, r in zip(sz, rets)) / sum(sz)) if sum(sz) else flat
    return {
        'n':            n,
        'base_rate':    round(base, 4),
        'brier':        round(brier(ps, ys), 4),
        'base_brier':   round(brier([base] * n, ys), 4),
        'auc_model':    round(auc(ps, ys), 4),
        'auc_heuristic': round(auc(cs, ys), 4),
        'edge_corr':    round(pearson(edge, rets), 4),
        'edge_sign_acc': round(sum(1 for e, r in zip(edge, rets) if (e > 0) == (r > 0)) / n, 4),
        'ev_flat':      round(flat, 4),
        'ev_sized':     round(evw, 4),
    }


def load_exit_rows(path=EXIT_DATA_FILE):
    """Load exit-outcome rows: reversal score at exit + forward return after exit."""
    if not os.path.exists(path):
        return []
    rows = []
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if 'rs' in r and 'fwd' in r:
            rows.append(r)
    return rows


def summarize_exits(rows):
    """Validate the exit (reversal) ladder: does a higher reversal score predict the price
    FALLING after exit (i.e., exiting was right)? ladder_corr should be negative."""
    n = len(rows)
    if n == 0:
        return {'n': 0}
    rs   = [r['rs'] for r in rows]
    fwd  = [r['fwd'] for r in rows]
    good = [r.get('good', 1 if r['fwd'] <= 0 else 0) for r in rows]
    return {
        'n':           n,
        'ladder_corr': round(pearson(rs, fwd), 4),   # negative ⇒ ladder predicts reversal
        'good_rate':   round(sum(good) / n, 4),       # share of exits where price fell after
        'mean_fwd':    round(sum(fwd) / n, 4),        # avg post-exit return (want ≤ 0)
    }


def tune_exit_thresh(rows, candidates=(0.40, 0.50, 0.55, 0.65, 0.75), min_support=8):
    """Pick the reversal threshold whose would-fire exits had the most-negative mean forward
    return (exiting avoided the most drop), requiring minimum support. Returns dict or None."""
    best = None
    for t in candidates:
        sub = [r['fwd'] for r in rows if r['rs'] >= t]
        if len(sub) >= min_support:
            mf = sum(sub) / len(sub)
            if best is None or (-mf) > best['benefit']:
                best = {'thresh': t, 'benefit': round(-mf, 4), 'support': len(sub),
                        'mean_fwd': round(mf, 4)}
    return best


def tune_blend_k(cfg, rows, candidates=(40, 60, 90, 120)):
    """Pick the blend_k with best out-of-sample AUC (tie-break: lower Brier)."""
    table = []
    for bk in candidates:
        c = dict(cfg); c['predictor_blend_k'] = float(bk)
        m = summarize(c, rows)
        table.append({'blend_k': bk, 'auc': m.get('auc_model', float('nan')),
                      'brier': m.get('brier', float('nan'))})
    ranked = sorted(table, key=lambda t: (-(t['auc'] if t['auc'] == t['auc'] else -1),
                                          t['brier'] if t['brier'] == t['brier'] else 9))
    return ranked[0]['blend_k'], table
