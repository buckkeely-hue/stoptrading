"""
StopTrading Predictive Model
────────────────────────────
Turns the ~20 hand-weighted agent/stream signals into a CALIBRATED probability of a
winning trade AND an expected-return (edge) estimate, used for EV-based position sizing
and a smarter exit. Two online linear heads share standardized features:

  • Classifier  → P(win)            (logistic regression, SGD)
  • Regressor   → expected return % (linear regression, SGD)

Labels come from TRIPLE-BARRIER outcomes, not the bot's own exit: from each entry we
watch the price path until it hits +X% (target), −Y% (stop), or a time horizon — the
first barrier touched defines win/loss and the realized return. This decouples model
quality from exit-logic quirks and measures *setup* quality directly (López de Prado).

Cold-start: before enough labeled trades, both heads blend toward the existing heuristic
(P from combined_score; edge from a heuristic-scaled prior). Trust grows α = n/(n+K).

Pure numpy — trains incrementally from the bot's own matured outcomes. Persists atomically.
"""
import os
import math
import threading

import numpy as np

from modules.io_safe import atomic_write_json, append_jsonl

_DIR        = os.path.dirname(os.path.abspath(__file__))
MODEL_FILE     = os.path.join(_DIR, '..', 'predictor_model.json')
DATA_FILE      = os.path.join(_DIR, '..', 'predictor_data.jsonl')
EXIT_DATA_FILE = os.path.join(_DIR, '..', 'predictor_exit_data.jsonl')

FEATURES = [
    'change_1h', 'change_5d', 'slope_pct', 'green_streak', 'volatility', 'vol_ratio',
    'rvol', 'float_rotation', 'ofi', 'vwap_dev_pct', 'pct_1m', 'pct_5m', 'pct_15m',
    'tech_score', 'rt_score', 'combined_score', 'log_price',
    'social_velocity', 'social_bull', 'social_exhaustion',
    'spread_pct',
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
        # Classification head (P win)
        self.w        = np.zeros(_N)
        self.b        = 0.0
        # Regression head (expected return %)
        self.wr       = np.zeros(_N)
        self.br       = 0.0
        # Shared running standardization (Welford)
        self.mean     = np.zeros(_N)
        self.M2       = np.zeros(_N)
        self.count    = 0
        self.n_trained = 0     # labeled trades (classifier)
        self.n_reg     = 0     # labeled trades with a return (regressor)
        self.wins      = 0
        self.lr       = float(config.get('predictor_lr', 0.05))
        self.lr_r     = float(config.get('predictor_lr_reg', 0.02))
        self.l2       = float(config.get('predictor_l2', 0.002))
        self.blend_k  = float(config.get('predictor_blend_k', 60.0))
        self._horizon = float(config.get('predictor_horizon_min', 120.0))
        self._pending = {}     # legacy close-based label staging (unused by barrier path)
        self._watch   = {}     # active triple-barrier observations (entries): key -> dict
        self._exit_watch = {}  # active exit-outcome observations: key -> dict
        self._persist = True
        self._src     = 'live' # data provenance: 'live' (delayed feed) | 'replay' (delay-immune)
        self._load()

    # ── Feature extraction ──────────────────────────────────────────────────────
    def extract(self, candidate, velocity, rvol):
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
            # Social-flow features (0 when the agent is off / no chatter); attached to the
            # candidate dict by the harvester before predict().
            'social_velocity':   min(float(c.get('social_velocity', 0.0)) / 100.0, 6.0),
            'social_bull':       float(c.get('social_bull', 0.0)),
            'social_exhaustion': float(c.get('social_exhaustion', 0.0)),
            # Observed bid-ask spread % at decision — both a predictive feature (wide spread
            # ⇒ lower realized edge) and the per-trade cost-calibration target.
            'spread_pct':        float(c.get('spread_pct', 0.0)),
        }

    def _vec(self, f):
        return np.array([float(f.get(k, 0.0)) for k in FEATURES], dtype=float)

    def _std(self):
        if self.count > 1:
            s = np.sqrt(np.maximum(self.M2 / (self.count - 1), 0.0))
            return np.where(s < 1e-6, 1.0, s)
        return np.ones(_N)

    def _standardize(self, x):
        return (x - self.mean) / self._std()

    def heuristic_prob(self, f):
        """Cold-start P(win) prior, anchored to the EMPIRICAL base win rate rather than the
        old combined_score→0.88 mapping, which triple-barrier labeling proved badly
        overconfident (true base rate ≈ the observed wins/n). Only a gentle score tilt is
        applied since the learned model — not this prior — should carry the conditional signal."""
        base = (self.wins / self.n_trained) if self.n_trained >= 10 else 0.35
        tilt = (float(f.get('combined_score', 50.0)) - 50.0) / 50.0 * 0.05
        return max(0.03, min(0.97, base + tilt))

    def edge_prior(self, f):
        """Cold-start expected return %: neutral (0). The old prior scaled positive with the
        overconfident heuristic, which inflated EV sizing; the regressor supplies the real
        signed edge once warm."""
        return 0.0

    # ── Prediction ──────────────────────────────────────────────────────────────
    def predict(self, candidate, velocity, rvol):
        return self.predict_features(self.extract(candidate, velocity, rvol))

    def predict_features(self, f):
        x = self._vec(f)
        with self._lock:
            warm = self.count > 5
            xs = self._standardize(x) if warm else np.zeros(_N)
            z   = float(self.w @ xs + self.b)
            er_model = float(self.wr @ xs + self.br) if warm else 0.0
            n   = self.n_trained
            nr  = self.n_reg
        p_model = _sigmoid(z)
        p_heur  = self.heuristic_prob(f)
        alpha   = n / (n + self.blend_k)
        p       = alpha * p_model + (1.0 - alpha) * p_heur
        alpha_r = nr / (nr + self.blend_k)
        edge    = alpha_r * er_model + (1.0 - alpha_r) * self.edge_prior(f)
        return {
            'p_win':         round(p, 4),
            'p_model':       round(p_model, 4),
            'p_heur':        round(p_heur, 4),
            'expected_edge': round(edge, 3),
            'alpha':         round(alpha, 3),
            'n_trained':     n,
            'features':      f,
        }

    def size_multiplier(self, p_win, expected_edge=None, tx_cost=None):
        """Conviction multiplier, sized by the chosen objective (config predictor_objective):

          'consistency' (harvesting north star): size on P(modest win) confidence ALONE. The
            expected edge is used only as a viability CHECK — if the predicted move doesn't clear
            cost, shrink hard; otherwise the upper clamp is kept tight so every bet stays small
            and uniform (many-small-positions harvesting, low single-name exposure). Edge
            MAGNITUDE never up-sizes, so the model can't be lured into big variance/lottery bets.

          'edge' (legacy momentum): EV-aware — edge above cost up-sizes, below it down-sizes.

        tx_cost is the spread-aware per-trade hurdle (modules.tx_cost); falls back to flat config."""
        base = max(0.5, min(1.35, 0.5 + 1.7 * (p_win - 0.5) + 0.5))
        if expected_edge is None:
            return round(base, 3)
        tx = float(tx_cost if tx_cost is not None else self.config.get('tx_cost_pct', 1.5))
        if self.config.get('predictor_objective', 'consistency') == 'consistency':
            cap = float(self.config.get('predictor_size_cap', 1.15))
            # The edge acts as a viability BRAKE only once the regressor is warm — a cold edge
            # estimate is ~0 and would otherwise shrink every cold-start trade, starving
            # deployment. Cold: size purely on P(win)/heuristic (tightly capped).
            warm_edge = self.n_reg >= int(self.config.get('predictor_min_n_edge', 30))
            if warm_edge and float(expected_edge) <= tx:
                return round(max(0.40, base * 0.6), 3)        # warm + won't clear cost → shrink
            return round(min(base, cap), 3)                   # else: P(win)-only, tightly capped
        tilt = 1.0 + max(-0.45, min(0.45, (float(expected_edge) - tx) * 0.10))
        return round(max(0.40, min(1.45, base * tilt)), 3)

    # ── Exit / reversal ladder ──────────────────────────────────────────────────
    def reversal_score(self, velocity, gain_pct, mins_held):
        v = velocity or {}
        s = 0.0
        ofi = float(v.get('ofi', 0.0))
        if ofi < -0.30:                                   s += 0.30
        p1, p5 = float(v.get('pct_1m', 0.0)), float(v.get('pct_5m', 0.0))
        if p1 < 0 and p5 > 0:                             s += 0.20
        if float(v.get('vwap_dev_pct', 0.0)) > 4.0:       s += 0.15
        if float(v.get('vol_ratio', 1.0)) < 1.0:          s += 0.15
        if mins_held > 120 and gain_pct > 0:              s += 0.10
        if p1 < -0.5 and ofi < 0:                         s += 0.10
        return min(s, 1.0)

    # ── Triple-barrier labeling ───────────────────────────────────────────────────
    def open_barrier(self, symbol, features, entry_price, ts, upper_pct, lower_pct, horizon_min=None):
        """Begin watching a price path for its first-touch barrier outcome."""
        entry_price = float(entry_price)
        if entry_price <= 0:
            return
        h   = float(horizon_min if horizon_min is not None else self._horizon)
        key = '{}:{:.0f}'.format(symbol, ts)
        with self._lock:
            self._watch[key] = {
                'sym':    symbol,
                'f':      features,
                'entry':  entry_price,
                'hi':     entry_price * (1.0 + upper_pct / 100.0),
                'lo':     entry_price * (1.0 - lower_pct / 100.0),
                'expiry': ts + h * 60.0,
                'last_ret': 0.0,
            }
            if len(self._watch) > 300:
                self._watch.pop(next(iter(self._watch)))

    def update_barriers(self, price_fn, now_ts):
        """Advance every open barrier; resolve+train any that touched a barrier or expired.
        price_fn(symbol) -> current price (0/None if unavailable). Called once per tick."""
        with self._lock:
            items = list(self._watch.items())
        for key, w in items:
            try:
                px = price_fn(w['sym']) or 0.0
            except Exception:
                px = 0.0
            won = ret = None
            if px and px > 0:
                r = (px - w['entry']) / w['entry'] * 100.0
                w['last_ret'] = r
                if px >= w['hi']:
                    won, ret = True, r
                elif px <= w['lo']:
                    won, ret = False, r
            if won is None and now_ts >= w['expiry']:
                r = w['last_ret']
                won, ret = (r > 0), r            # vertical barrier: label by sign at horizon
            if won is not None:
                with self._lock:
                    self._watch.pop(key, None)
                self.train(w['f'], 1.0 if won else 0.0, ret)

    def open_barrier_count(self):
        with self._lock:
            return len(self._watch)

    # ── Exit-outcome watching (was leaving early good or bad?) ────────────────────
    def open_exit_watch(self, symbol, reversal_score, gain_pct, mins_held, reason, exit_price, ts, horizon_min=None):
        """After an exit, watch the price for `horizon` minutes to judge the decision:
        if price falls (forward return ≤ 0) the exit was GOOD; if it keeps rising we left
        money on the table. Logs (reversal_score, context, forward return) for the nightly
        controller to validate the reversal ladder and tune the exit threshold."""
        exit_price = float(exit_price)
        if exit_price <= 0:
            return
        h = float(horizon_min if horizon_min is not None else
                  self.config.get('predictor_exit_horizon_min', 45.0))
        key = 'X:{}:{:.0f}'.format(symbol, ts)
        with self._lock:
            self._exit_watch[key] = {
                'sym': symbol, 'rs': round(float(reversal_score), 4),
                'gain': round(float(gain_pct), 4), 'mins': round(float(mins_held), 1),
                'reason': reason, 'exit_price': exit_price, 'expiry': ts + h * 60.0,
                'last_px': exit_price,
            }
            if len(self._exit_watch) > 300:
                self._exit_watch.pop(next(iter(self._exit_watch)))

    def update_exit_watches(self, price_fn, now_ts):
        with self._lock:
            items = list(self._exit_watch.items())
        for key, w in items:
            try:
                px = price_fn(w['sym']) or 0.0
            except Exception:
                px = 0.0
            if px and px > 0:
                w['last_px'] = px
            if now_ts >= w['expiry']:
                fwd = (w['last_px'] - w['exit_price']) / w['exit_price'] * 100.0
                with self._lock:
                    self._exit_watch.pop(key, None)
                self._append_exit(w, fwd)

    def _append_exit(self, w, fwd):
        if not self._persist:
            return
        # Crash-safe append; NOT rotated — this is training corpus the evaluator reads in full.
        append_jsonl(EXIT_DATA_FILE, {
            'src': getattr(self, '_src', 'live'),
            'rs': w['rs'], 'gain': w['gain'], 'mins': w['mins'], 'reason': w['reason'],
            'fwd': round(float(fwd), 4), 'good': int(fwd <= 0.0),
        })

    # ── Online training ──────────────────────────────────────────────────────────
    def train(self, features, y, ret=None):
        src = getattr(self, '_src', 'live')
        # DELAYED-DATA INTEGRITY: while the live feed is delayed/biased, live outcomes are
        # LOGGED (tagged) for ops analysis but do NOT update the model — only delay-immune
        # replay data trains it. Flip train_on_live=True once the feed is real-time.
        trainable = (src == 'replay') or bool(self.config.get('train_on_live', False))
        if trainable:
            x = self._vec(features)
            with self._lock:
                self.count += 1
                delta = x - self.mean
                self.mean += delta / self.count
                self.M2 += delta * (x - self.mean)
                xs = self._standardize(x)
                p = _sigmoid(float(self.w @ xs + self.b))
                g = p - y
                self.w -= self.lr * (g * xs + self.l2 * self.w)
                self.b -= self.lr * g
                self.n_trained += 1
                self.wins += int(y > 0.5)
                if ret is not None:
                    r = max(-50.0, min(50.0, float(ret)))   # clip pathological prints
                    e = float(self.wr @ xs + self.br) - r
                    self.wr -= self.lr_r * (e * xs + self.l2 * self.wr)
                    self.br -= self.lr_r * e
                    self.n_reg += 1
                self._save()
        self._append_data(features, y, ret, src)

    # legacy close-based labeling (kept for compatibility; barrier path is primary)
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

    # ── Persistence ───────────────────────────────────────────────────────────────
    def _load(self):
        try:
            if os.path.exists(MODEL_FILE):
                import json
                with open(MODEL_FILE) as fh:
                    d = json.load(fh)
                if d.get('features') == FEATURES:
                    self.w    = np.array(d.get('w', [0.0] * _N), dtype=float)
                    self.b    = float(d.get('b', 0.0))
                    self.wr   = np.array(d.get('wr', [0.0] * _N), dtype=float)
                    self.br   = float(d.get('br', 0.0))
                    self.mean = np.array(d.get('mean', [0.0] * _N), dtype=float)
                    self.M2   = np.array(d.get('M2', [0.0] * _N), dtype=float)
                    self.count     = int(d.get('count', 0))
                    self.n_trained = int(d.get('n_trained', 0))
                    self.n_reg     = int(d.get('n_reg', 0))
                    self.wins      = int(d.get('wins', 0))
        except Exception:
            pass

    def _save(self):
        if not self._persist:
            return
        try:
            atomic_write_json(MODEL_FILE, {
                'features': FEATURES,
                'w':  [round(x, 6) for x in self.w.tolist()],
                'b':  round(self.b, 6),
                'wr': [round(x, 6) for x in self.wr.tolist()],
                'br': round(self.br, 6),
                'mean': [round(x, 6) for x in self.mean.tolist()],
                'M2':   [round(x, 6) for x in self.M2.tolist()],
                'count': self.count,
                'n_trained': self.n_trained,
                'n_reg': self.n_reg,
                'wins': self.wins,
            })
        except Exception:
            pass

    def _append_data(self, features, y, ret=None, src='live'):
        if not self._persist:
            return
        # Crash-safe append; NOT rotated — this is the training corpus the evaluator reads in full.
        append_jsonl(DATA_FILE, {'src': src, 'y': y,
                                 'ret': (None if ret is None else round(float(ret), 4)),
                                 'f': {k: round(features.get(k, 0.0), 5) for k in FEATURES}})

    def stats(self):
        with self._lock:
            n = self.n_trained
            base = (self.wins / n) if n else 0.0
            top = sorted(zip(FEATURES, self.w.tolist()), key=lambda kv: -abs(kv[1]))[:6]
            return {
                'n_trained':     n,
                'n_reg':         self.n_reg,
                'wins':          self.wins,
                'losses':        n - self.wins,
                'base_win_rate': round(base, 3),
                'trust_alpha':   round(n / (n + self.blend_k), 3) if (n + self.blend_k) else 0.0,
                'blend_k':       self.blend_k,
                'watching':      len(self._watch),
                'top_features':  [{'name': k, 'weight': round(w, 3)} for k, w in top],
            }
