"""
SocialFlowAgent — crowd-flow signal for penny stocks.

Penny names move on attention, not fundamentals. This agent measures the SECOND-ORDER
properties of social chatter (not raw counts): mention velocity, acceleration, sentiment
tilt and SHIFT, crowd breadth, and a crude bot/astroturf ratio — then derives both a
buy-side conviction input and an EXHAUSTION (exit/avoid) signal.

Sources (free, pluggable; degrades gracefully):
  • StockTwits per-symbol stream  — free, no key, retail-equity focused (primary)
  • Reddit (PRAW)                 — free, needs a Reddit app id/secret (optional)
  • X/Twitter                     — PAID API; left as a disabled adapter (see _fetch_x)

Design note: the project's own barrier data shows entries tend to BUY TOPS, and buzz spikes
usually mark tops. So velocity is treated as a feature to be validated, and the high-value
output is `get_exhaustion()` (buzz peaking + sentiment rolling over) as a SELL/AVOID signal.
Demand-driven + cached + rate-limited: only symbols the bot is actually weighing get polled.
"""
import json
import time
import threading
import urllib.request
from datetime import datetime, timezone

_ST_URL  = 'https://api.stocktwits.com/api/2/streams/symbol/{}.json'
_HDRS    = {'User-Agent': 'Mozilla/5.0 (StopTrading research)'}
_CACHE_TTL   = 180.0     # seconds — per-symbol fetch cache
_RL_MAX      = 120       # max StockTwits requests / rolling hour (stay under free limit)


def _parse_ts(s):
    try:
        return datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except Exception:
        return None


class SocialFlowAgent:
    def __init__(self, config):
        self.config = config
        self._lock  = threading.Lock()
        self._cache = {}     # sym -> (features dict, ts)
        self._prev  = {}     # sym -> {velocity, bull, ts}  (for accel / sentiment-shift)
        self._req_times = []  # rolling request timestamps for rate limiting

    # ── rate limit ────────────────────────────────────────────────────────────
    def _can_request(self):
        now = time.time()
        self._req_times = [t for t in self._req_times if now - t < 3600]
        return len(self._req_times) < _RL_MAX

    # ── source: StockTwits (free) ───────────────────────────────────────────────
    def _fetch_stocktwits(self, sym):
        if not self._can_request():
            return None
        try:
            self._req_times.append(time.time())
            req = urllib.request.Request(_ST_URL.format(sym), headers=_HDRS)
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            return data.get('messages', []) or []
        except Exception:
            return None

    # ── source: X/Twitter (PAID — disabled adapter) ──────────────────────────────
    def _fetch_x(self, sym):
        """X/Twitter API is paid (Basic ~$100-200/mo, low read limits). Enable by setting
        x_bearer_token in config; left off so no paid dependency is incurred."""
        token = self.config.get('x_bearer_token', '')
        if not token:
            return None
        return None   # adapter stub — implement against the tier you subscribe to

    # ── feature computation ──────────────────────────────────────────────────────
    def _compute(self, sym, msgs):
        n = len(msgs)
        if n < 3:
            return self._empty()
        # velocity: messages per hour, from the span of the returned recent messages
        times = [t for t in (_parse_ts(m.get('created_at', '')) for m in msgs) if t]
        if len(times) >= 2:
            span_min = max((max(times) - min(times)).total_seconds() / 60.0, 1.0)
            velocity = min(len(times) / span_min * 60.0, 600.0)   # msgs/hr, capped
        else:
            velocity = 0.0
        # sentiment from StockTwits tags
        bull = sum(1 for m in msgs if (m.get('entities', {}).get('sentiment') or {}).get('basic') == 'Bullish')
        bear = sum(1 for m in msgs if (m.get('entities', {}).get('sentiment') or {}).get('basic') == 'Bearish')
        bull_ratio = bull / (bull + bear) if (bull + bear) else 0.5
        # crowd breadth — distinct authors
        users = [str((m.get('user') or {}).get('id', '')) for m in msgs]
        breadth = len(set(u for u in users if u))
        # bot / astroturf heuristics: duplicate bodies + brand-new accounts
        bodies = [' '.join((m.get('body', '') or '').lower().split()) for m in msgs]
        dupe_ratio = 1.0 - (len(set(bodies)) / max(len(bodies), 1))
        now = datetime.now(timezone.utc)
        new_acct = 0
        for m in msgs:
            jd = _parse_ts(((m.get('user') or {}).get('join_date', '') or '') + 'T00:00:00Z'
                           if (m.get('user') or {}).get('join_date') else '')
            if jd and (now - jd).days < 30:
                new_acct += 1
        new_ratio = new_acct / n
        bot_ratio = min(1.0, 0.6 * dupe_ratio + 0.4 * new_ratio)

        # accel + sentiment-shift vs previous observation
        with self._lock:
            prev = self._prev.get(sym)
            self._prev[sym] = {'velocity': velocity, 'bull': bull_ratio, 'ts': time.time()}
        accel = (velocity - prev['velocity']) if prev else 0.0
        sent_shift = (bull_ratio - prev['bull']) if prev else 0.0

        # exhaustion: loud crowd + (sentiment rolling over OR buzz decelerating) → likely top
        loud = velocity >= 40.0
        exhaustion = 0.0
        if loud:
            if sent_shift < -0.05:   exhaustion += 0.45
            if accel < 0:            exhaustion += 0.30
            if bull_ratio > 0.85:    exhaustion += 0.15   # euphoric / one-sided = late
            if bot_ratio > 0.4:      exhaustion += 0.10
        exhaustion = min(exhaustion, 1.0)
        novelty = 1.0 if (prev is None and velocity >= 30.0) else 0.0

        return {
            'social_velocity':  round(velocity, 1),
            'social_accel':     round(accel, 1),
            'social_bull':      round(bull_ratio - 0.5, 3),   # tilt, -0.5..+0.5
            'social_shift':     round(sent_shift, 3),
            'social_breadth':   breadth,
            'social_bot_ratio': round(bot_ratio, 3),
            'social_exhaustion': round(exhaustion, 3),
            'social_novelty':   novelty,
        }

    def _empty(self):
        return {'social_velocity': 0.0, 'social_accel': 0.0, 'social_bull': 0.0,
                'social_shift': 0.0, 'social_breadth': 0, 'social_bot_ratio': 0.0,
                'social_exhaustion': 0.0, 'social_novelty': 0.0}

    # ── public API ────────────────────────────────────────────────────────────────
    def get_features(self, sym):
        sym = (sym or '').upper().strip()
        if not sym:
            return self._empty()
        now = time.time()
        with self._lock:
            c = self._cache.get(sym)
            if c and now - c[1] < _CACHE_TTL:
                return c[0]
        msgs = self._fetch_stocktwits(sym)
        if msgs is None:                      # rate-limited or error → last cache or empty
            with self._lock:
                c = self._cache.get(sym)
            return c[0] if c else self._empty()
        feats = self._compute(sym, msgs)
        with self._lock:
            self._cache[sym] = (feats, now)
            if len(self._cache) > 300:
                self._cache.pop(next(iter(self._cache)))
        return feats

    def get_social_score(self, sym):
        """Buy-side conviction bonus, bot-discounted. Capped. Rewards rising bullish buzz
        with breadth; penalizes exhaustion and bearish tilt. (Validated as a feature — the
        predictor decides how much to trust it.)"""
        f = self.get_features(sym)
        if f['social_velocity'] < 10.0:
            return 0
        quality = 1.0 - f['social_bot_ratio']
        raw = (min(f['social_velocity'] / 100.0, 1.0) * 10.0          # buzz level
               + max(0.0, f['social_accel']) / 50.0 * 8.0              # rising buzz
               + f['social_bull'] * 16.0                               # bullish tilt
               + min(f['social_breadth'] / 20.0, 1.0) * 4.0)           # crowd breadth
        raw *= quality
        raw -= f['social_exhaustion'] * 15.0                           # peaking = penalty
        return int(max(-15, min(20, raw)))

    def get_exhaustion(self, sym):
        """0..1 — crowd buzz peaking / sentiment rolling over → exit/avoid."""
        return float(self.get_features(sym).get('social_exhaustion', 0.0))
