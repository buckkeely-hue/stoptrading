"""
StopTrading Predictive Model
────────────────────────────
Turns the ~20 hand-weighted agent/stream signals into a CALIBRATED probability of a
winning trade, and a separate reversal/exhaustion score for exit timing.

Why this exists: the previous decision logic SUMMED signals with fixed weights
(score = Σ wᵢ·xᵢ) and gated on a raw score that is not a probability. This module is the
same linear form but (1) the weights are LEARNED online from the bot's own closed-trade
outcomes, and (2) the output is squashed to a true P(win) ∈ [0,1] — which is what makes
conviction-based / fractional-Kelly sizing valid.

Design (pure numpy, no sklearn):
  • L2-regularized logistic regression, trained incrementally (SGD) — one update per
    closed trade. Features standardized with a running Welford mean/variance.
  • Cold-start: before enough labeled trades exist, blend toward the existing heuristic
    (combined_score → logistic). Trust in the learned model grows as α = n/(n+K).
  • Separate transparent reversal ladder for exits (order-flow flip, VWAP stretch,
    momentum roll-over, volume decay, time-in-trade) → P(reversal soon).

Everything persists atomically so learning survives restarts.
"""
import os
import math
import threading

import numpy as np

from modules.io_safe import atomic_write_json

_DIR        = os.path.dirname(os.path.abspath(__file__))
MODEL_FILE  = os.path.join(_DIR, '..', 'predictor_model.json')
DATA_FILE   = os.path.join(_DIR, '..', 'predictor_data.jsonl')

# Ordered feature names — the weight vector is aligned to this list. Append-only.
FEATURES = [
    'change_1h', 'change_5d', 'slope_pct', 'green_streak', 'volatility', 'vol_ratio',
    'rvol', 'float_rotation', 'ofi', 'vwap_dev_pct', 'pct_1m', 'pct_5m', 'pct_15m',
    'tech_score', 'rt_score', 'combined_score', 'log_price',
]
_N = len(FEATURES)


def _sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


class Predictor:
    def __init__(self, config):
        self.config   = config
        self._lock    = threading.Lock()
        self.w        = np.zeros(_N)
        self.b        = 0.0
        self.mean     = np.zeros(_N)
        self.M2       = np.zeros(_N)     # Welford sum of squared deviations
        self.count    = 0                # samples seen for standardization
        self.n_trained = 0               # labeled trades trained on
        self.wins     = 0                # for base-rate / audit
        self.lr       = float(config.get('predictor_lr', 0.05))
        self.l2       = float(config.get('predictor_l2', 0.002))
        self.blend_k  = float(config.get('predictor_blend_k', 60.0))
        self._pending = {}               # entry_key -> feature dict, awaiting outcome
        self._persist = True             # replay sets this False to avoid touching the live model
        self._load()

    # ── Feature extraction ──────────────────────────────────────────────────────
    def extract(self, candidate, velocity, rvol):
        """Assemble the feature dict from values already computed at the entry decision.
        Nothing here re-calls an agent or hits the network."""
        c = candidate or {}
        v = velocity or {}
        price = float(c.get('price', 1.0)) or 1.0
        return {
            'change_1h':      float(c.get('change_1h', 0.0)),
            'change_5d':      float(c.get('change_5d', 0.0)),
            'slope_pct':      float(c.get('slope_pct', 0.0)) * 100.0,
            'green_streak':   float(c.get('green_streak', 0)),
            'volatility':     float(c.get('volatility', 0.0)),
            'vol_ratio':      float(c.get('vol_ratio', 1.0)),
            'rvol':           float(rvol or 0.0),
            'float_rotation': float(c.get('float_rotation', 0.0)),
            'ofi':            float(v.get('ofi', 0.0)),
            'vwap_dev_pct':   float(v.get('vwap_dev_pct', 0.0)),
            'pct_1m':         float(v.get('pct_1m', 0.0)),
            'pct_5m':         float(v.get('pct_5m', 0.0)),
            'pct_15m':        float(v.get('pct_15m', 0.0)),
            'tech_score':     float(c.get('tech_score', c.get('score', 0.0))),
            'rt_score':       float(c.get('rt_score', 0.0)),
            'combined_score': float(c.get('combined_score', c.get('score', 0.0))),
            'log_price':      math.log(max(price, 0.01)),
        }

    def _vec(self, f):
        return np.array([float(f.get(k, 0.0)) for k in FEATURES], dtype=float)

    def _std(self):
        if self.count > 1:
            var = self.M2 / (self.count - 1)
            s = np.sqrt(np.maximum(var, 0.0))
            return np.where(s < 1e-6, 1.0, s)
        return np.ones(_N)

    def _standardize(self, x):
        return (x - self.mean) / self._std()

    def heuristic_prob(self, f):
        """Map the legacy additive combined_score to a probability. This is the cold-start
        prior and the anchor the learned model blends with."""
        cs = float(f.get('combined_score', 0.0))
        return _sigmoid((cs - 50.0) / 18.0)

    # ── Prediction ──────────────────────────────────────────────────────────────
    def predict(self, candidate, velocity, rvol):
        f = self.extract(candidate, velocity, rvol)
        x = self._vec(f)
        with self._lock:
            warm = self.count > 5
            xs = self._standardize(x) if warm else np.zeros(_N)
            z = float(self.w @ xs + self.b)
            n = self.n_trained
        p_model = _sigmoid(z)
        p_heur  = self.heuristic_prob(f)
        alpha   = n / (n + self.blend_k)        # 0 at cold-start → grows with data
        p = alpha * p_model + (1.0 - alpha) * p_heur
        return {
            'p_win':  round(p, 4),
            'p_model': round(p_model, 4),
            'p_heur': round(p_heur, 4),
            'alpha':  round(alpha, 3),
            'n_trained': n,
            'features': f,
        }

    def size_multiplier(self, p_win):
        """Conviction multiplier for position sizing from a calibrated probability.
        Centered at 1.0 around p=0.5, clamped to a moderate band."""
        return max(0.5, min(1.35, 0.5 + 1.7 * (p_win - 0.5) + 0.5))

    # ── Exit / reversal ladder ──────────────────────────────────────────────────
    def reversal_score(self, velocity, gain_pct, mins_held):
        """Transparent P(reversal soon) ∈ [0,1] from live microstructure. Higher → leave.
        Kept rule-based (auditable) for v1; can be learned later from exit outcomes."""
        v = velocity or {}
        s = 0.0
        ofi = float(v.get('ofi', 0.0))
        if ofi < -0.30:                                   s += 0.30   # sell-side order flow
        p1, p5 = float(v.get('pct_1m', 0.0)), float(v.get('pct_5m', 0.0))
        if p1 < 0 and p5 > 0:                             s += 0.20   # momentum rolling over
        if float(v.get('vwap_dev_pct', 0.0)) > 4.0:       s += 0.15   # stretched above VWAP
        if float(v.get('vol_ratio', 1.0)) < 1.0:          s += 0.15   # current volume fading out
        if mins_held > 120 and gain_pct > 0:              s += 0.10   # aged, in profit — bank it
        if p1 < -0.5 and ofi < 0:                         s += 0.10   # active fade
        return min(s, 1.0)

    # ── Online training ──────────────────────────────────────────────────────────
    def remember(self, key, features):
        with self._lock:
            self._pending[key] = features
            if len(self._pending) > 500:
                self._pending.pop(next(iter(self._pending)))

    def record_outcome(self, key, won):
        with self._lock:
            f = self._pending.pop(key, None)
        if f is not None:
            self.train(f, 1.0 if won else 0.0)

    def train(self, features, y):
        x = self._vec(features)
        with self._lock:
            # Welford running mean/variance for standardization
            self.count += 1
            delta = x - self.mean
            self.mean += delta / self.count
            self.M2 += delta * (x - self.mean)
            xs = self._standardize(x)
            p = _sigmoid(float(self.w @ xs + self.b))
            g = p - y                                     # logistic gradient
            self.w -= self.lr * (g * xs + self.l2 * self.w)
            self.b -= self.lr * g
            self.n_trained += 1
            self.wins += int(y > 0.5)
            self._save()
        self._append_data(features, y)

    # ── Persistence ───────────────────────────────────────────────────────────────
    def _load(self):
        try:
            if os.path.exists(MODEL_FILE):
                import json
                with open(MODEL_FILE) as fh:
                    d = json.load(fh)
                if d.get('features') == FEATURES:        # only load if schema matches
                    self.w    = np.array(d.get('w', [0.0] * _N), dtype=float)
                    self.b    = float(d.get('b', 0.0))
                    self.mean = np.array(d.get('mean', [0.0] * _N), dtype=float)
                    self.M2   = np.array(d.get('M2', [0.0] * _N), dtype=float)
                    self.count     = int(d.get('count', 0))
                    self.n_trained = int(d.get('n_trained', 0))
                    self.wins      = int(d.get('wins', 0))
        except Exception:
            pass

    def _save(self):
        if not self._persist:
            return
        try:
            atomic_write_json(MODEL_FILE, {
                'features': FEATURES,
                'w': [round(x, 6) for x in self.w.tolist()],
                'b': round(self.b, 6),
                'mean': [round(x, 6) for x in self.mean.tolist()],
                'M2': [round(x, 6) for x in self.M2.tolist()],
                'count': self.count,
                'n_trained': self.n_trained,
                'wins': self.wins,
            })
        except Exception:
            pass

    def _append_data(self, features, y):
        if not self._persist:
            return
        try:
            import json
            with open(DATA_FILE, 'a') as fh:
                fh.write(json.dumps({'y': y, 'f': {k: round(features.get(k, 0.0), 5) for k in FEATURES}}) + '\n')
        except Exception:
            pass

    def stats(self):
        with self._lock:
            n = self.n_trained
            base = (self.wins / n) if n else 0.0
            top = sorted(zip(FEATURES, self.w.tolist()), key=lambda kv: -abs(kv[1]))[:6]
            return {
                'n_trained':     n,
                'wins':          self.wins,
                'losses':        n - self.wins,
                'base_win_rate': round(base, 3),
                'trust_alpha':   round(n / (n + self.blend_k), 3) if (n + self.blend_k) else 0.0,
                'blend_k':       self.blend_k,
                'pending':       len(self._pending),
                'top_features':  [{'name': k, 'weight': round(w, 3)} for k, w in top],
            }
