"""
DecisionRecorder — full decision-context capture for the paper-trading data phase.

Each scan cycle we discard almost everything we computed: the candidates we DIDN'T buy, the
individual agent contributions (only the sum survives), and the market regime. This records
all of it so future paradigms can learn from the ROADS NOT TAKEN — counterfactual learning,
agent pruning, regime-conditioned models — instead of only from the one trade taken.

Per cycle → one record: regime + top-N movers, each with {features, per-agent attribution,
passed-filter, was-top-pick}. A deduped forward-outcome watcher then stamps each candidate
symbol's subsequent return, so rejected near-misses get labeled too.

Append-only, paper-phase-only, behind decision_log_enabled. All best-effort (never breaks trading).
"""
import os
import json
import time
import threading
from datetime import datetime

_DIR           = os.path.dirname(os.path.abspath(__file__))
DECISIONS_FILE = os.path.join(_DIR, '..', 'decision_log.jsonl')
OUTCOMES_FILE  = os.path.join(_DIR, '..', 'decision_outcomes.jsonl')
_HORIZON_MIN   = 60.0
_TOP_N         = 8


class DecisionRecorder:
    def __init__(self, autopilot):
        self.ap    = autopilot          # for agent access (cached score methods)
        self._lock = threading.Lock()
        self._watch = {}                # sym -> {price0, ts}  (deduped forward-outcome watch)
        self._src  = 'live'             # provenance: 'live' (delayed feed) | 'replay'

    def _agent_scores(self, sym, c):
        ap = self.ap
        def g(obj, meth, *a):
            try:
                return obj and round(float(getattr(obj, meth)(*a)), 2)
            except Exception:
                return 0
        try:
            return {
                'insider':    g(ap.insider, 'get_score', sym),
                'congress':   g(ap.congress, 'get_score', sym),
                'news':       g(ap.news, 'get_news_score', sym),
                'squeeze':    g(ap.short_squeeze, 'get_squeeze_score', sym),
                'float_rot':  g(ap.float_rotation, 'get_rotation_score', sym),
                'sector':     g(ap.sector, 'get_sector_score', sym),
                'social':     g(ap.social, 'get_social_score', sym),
                'behavioral': round(float(c.get('behavioral_score', 0)), 2),
                'rt_score':   round(float(c.get('rt_score', 0)), 2),
            }
        except Exception:
            return {}

    def record(self, movers, candidate_syms, chosen_sym, regime, ts):
        """movers: full scan output (pre-filter). candidate_syms: set that passed the filter."""
        if not movers:
            return
        try:
            top = sorted(movers, key=lambda m: m.get('combined_score', m.get('score', 0)), reverse=True)[:_TOP_N]
            recs = []
            for c in top:
                sym = c.get('symbol')
                recs.append({
                    'sym': sym,
                    'score':          round(float(c.get('combined_score', c.get('score', 0))), 1),
                    'change_1h':      c.get('change_1h'),
                    'change_5d':      c.get('change_5d'),
                    'vol_ratio':      c.get('vol_ratio'),
                    'price':          c.get('price'),
                    'green_streak':   c.get('green_streak'),
                    'float_rotation': c.get('float_rotation'),
                    'rt_signal':      c.get('rt_signal', ''),
                    'agents':         self._agent_scores(sym, c),
                    'passed_filter':  sym in candidate_syms,
                    'top_pick':       sym == chosen_sym,
                })
                with self._lock:
                    if sym not in self._watch and float(c.get('price', 0) or 0) > 0:
                        self._watch[sym] = {'price0': float(c['price']), 'ts': ts}
                        if len(self._watch) > 250:
                            self._watch.pop(next(iter(self._watch)))
            self._append(DECISIONS_FILE, {
                'src':   self._src,
                'ts':    round(ts, 0),
                'time':  datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'regime': regime,
                'chosen': chosen_sym,
                'n_movers': len(movers),
                'n_passed': len(candidate_syms),
                'candidates': recs,
            })
        except Exception:
            pass

    def update(self, price_fn, now_ts):
        """Resolve forward-outcome watches at the 60-min horizon → label every candidate symbol."""
        with self._lock:
            items = list(self._watch.items())
        for sym, w in items:
            if now_ts - w['ts'] >= _HORIZON_MIN * 60:
                try:
                    px = price_fn(sym) or 0.0
                except Exception:
                    px = 0.0
                with self._lock:
                    self._watch.pop(sym, None)
                if px > 0 and w['price0'] > 0:
                    ret = (px - w['price0']) / w['price0'] * 100.0
                    self._append(OUTCOMES_FILE, {'sym': sym, 'ts': round(w['ts'], 0),
                                                 'ret_60m': round(ret, 3)})

    def _append(self, path, obj):
        try:
            with open(path, 'a') as f:
                f.write(json.dumps(obj) + '\n')
        except Exception:
            pass
