"""
AdaptivePolicy — turns the banked barrier-outcome data into REAL-TIME buy/sell decisions for
profitability and risk exposure, with hard guardrails.

The bot banks every setup's triple-barrier outcome (delay-immune). This reads that ledger and
answers one governing question each cycle: "given how our trades are ACTUALLY resolving, should
we be pressing or protecting right now?" It expresses the answer as a single bounded
exposure_scale that the sizer multiplies in live — so when realized win rate sits below the
breakeven the target/stop/cost demand, the bot automatically trades SMALLER (risk down); when it
is comfortably above, it sizes back up (profit up). It never flips the strategy, never moves a
stop on an open trade, and never leaves a safe band.

Guardrails (why this can't run away):
  • Neutral (scale=1.0) until min_n outcomes exist — no acting on noise.
  • Rolling window — responds to the CURRENT regime, not ancient history.
  • exposure_scale clamped to [floor, cap] (default 0.5–1.15) — can't zero-out or over-lever.
  • Only SIZE is altered live. Target/stop changes are RECOMMENDATIONS surfaced for the nightly
    controller / human — never applied to positions already open.
  • Cached refresh on a cadence — cheap, and decisions don't thrash tick-to-tick.
"""
import os
import json
import time

from modules.tx_cost import transaction_cost_pct

_DIR      = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(_DIR, '..', 'predictor_data.jsonl')


def breakeven_win_rate(target, stop, cost):
    """p where expectancy=0:  p(T−c)=(1−p)(S+c)  ⇒  p=(S+c)/(T+S)."""
    denom = (target + stop)
    return (stop + cost) / denom if denom > 0 else 1.0


def expectancy(p, target, stop, cost):
    return p * (target - cost) - (1.0 - p) * (stop + cost)


class AdaptivePolicy:
    def __init__(self, config):
        self.config = config
        self._last_refresh = 0.0
        self._cache = self._neutral()

    def _neutral(self):
        return {'n': 0, 'win_rate': None, 'breakeven': None, 'expectancy': None,
                'exposure_scale': 1.0, 'rec_target': None, 'rec_stop': None,
                'cost': None, 'dist': {}, 'note': 'cold — neutral until data accrues'}

    # ── data ────────────────────────────────────────────────────────────────────
    def _load_outcomes(self):
        """Delay-immune barrier returns (excludes 'live'-tagged rows), most recent `window`."""
        if not os.path.exists(DATA_FILE):
            return []
        rows = []
        for line in open(DATA_FILE):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get('ret') is None:
                continue
            if r.get('src') == 'live':       # quarantine delayed-live from the governor
                continue
            rows.append(r)
        window = int(self.config.get('adaptive_window', 200))
        return rows[-window:]

    # ── refresh / compute ─────────────────────────────────────────────────────────
    def refresh(self, force=False):
        now = time.time()
        if not force and (now - self._last_refresh) < float(self.config.get('adaptive_refresh_sec', 300)):
            return self._cache
        self._last_refresh = now
        if not self.config.get('adaptive_policy_enabled', True):
            self._cache = self._neutral(); return self._cache

        rows = self._load_outcomes()
        rets = [float(r['ret']) for r in rows if r.get('ret') is not None]
        n = len(rets)
        T = float(self.config.get('harvest_trigger_pct', 4.0))
        S = float(self.config.get('max_single_loss_pct', 3.0))

        # observed cost from median logged spread (tight-spread selection → low cost)
        spreads = sorted(float(r['f'].get('spread_pct', 0)) for r in rows if r.get('f'))
        med_sp = next((s for s in spreads if s > 0), 0.0)
        if med_sp:
            pos = [s for s in spreads if s > 0]
            med_sp = pos[len(pos) // 2]
        cost = transaction_cost_pct(med_sp, None, None, self.config) if med_sp else float(self.config.get('tx_cost_pct', 1.5))

        min_n = int(self.config.get('adaptive_min_n', 30))
        if n < min_n:
            self._cache = {**self._neutral(), 'n': n, 'cost': round(cost, 3),
                           'note': 'building sample (%d/%d) — neutral' % (n, min_n)}
            return self._cache

        win_rate = sum(1 for r in rets if r >= T) / n
        be = breakeven_win_rate(T, S, cost)
        ev = expectancy(win_rate, T, S, cost)

        # exposure governor: edge above/below breakeven → press/protect, bounded
        edge = win_rate - be
        sens  = float(self.config.get('adaptive_sensitivity', 3.0))
        floor = float(self.config.get('adaptive_exposure_floor', 0.5))
        cap   = float(self.config.get('adaptive_exposure_cap', 1.15))
        scale = max(floor, min(cap, 1.0 + edge * sens))

        self._cache = {
            'n': n, 'win_rate': round(win_rate, 4), 'breakeven': round(be, 4),
            'expectancy': round(ev, 4), 'exposure_scale': round(scale, 3),
            'cost': round(cost, 3),
            'rec_target': self._recommend_target(rets, S, cost),
            'rec_stop':   self._recommend_stop(rets),
            'dist': self._dist(rets),
            'note': ('pressing (win%% %.0f ≥ breakeven %.0f)' % (win_rate * 100, be * 100)) if edge >= 0
                    else ('protecting (win%% %.0f < breakeven %.0f → size ×%.2f)' % (win_rate * 100, be * 100, scale)),
        }
        return self._cache

    def _recommend_target(self, rets, S, cost):
        """Expectancy-maximizing harvest target over the empirical distribution (bounded). If this
        comes back HIGH, it is a signal the names have no harvestable middle (ride-the-rip), i.e.
        the universe — not the exit — is the problem."""
        n = len(rets); best = None
        for T in (2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0):
            ev = sum((T - cost) if r >= T else (max(-S, r) - cost) for r in rets) / n
            if best is None or ev > best[1]:
                best = (T, ev)
        return {'target': best[0], 'ev': round(best[1], 4)}

    def _recommend_stop(self, rets):
        """Stop near the downside the names actually reach (5th percentile of losers), bounded."""
        losers = sorted(r for r in rets if r < 0)
        if not losers:
            return {'stop': float(self.config.get('max_single_loss_pct', 3.0)), 'basis': 'no losers in window'}
        p5 = losers[max(0, int(len(losers) * 0.05))]
        return {'stop': round(min(5.0, max(2.0, abs(p5))), 2), 'basis': '5th-pct loss %.1f%%' % p5}

    def _dist(self, rets):
        b = {'<-3': 0, '-3..0': 0, '0..2': 0, '2..4': 0, '4..6': 0, '>=6': 0}
        for r in rets:
            if r < -3: b['<-3'] += 1
            elif r < 0: b['-3..0'] += 1
            elif r < 2: b['0..2'] += 1
            elif r < 4: b['2..4'] += 1
            elif r < 6: b['4..6'] += 1
            else: b['>=6'] += 1
        return b

    # ── live accessor ──────────────────────────────────────────────────────────────
    def exposure_scale(self):
        """The single real-time lever the sizer multiplies. Cheap (cached)."""
        try:
            return float(self.refresh().get('exposure_scale', 1.0))
        except Exception:
            return 1.0

    def stats(self):
        return self.refresh()

    def harvest_report(self):
        """Full North-Star report for the dashboard tab: the harvesting thesis at a glance —
        aggregated outcome data, the middle-ground trades, potential profit, and the data-drawn
        guardrails (windows of profitability) that automate buy / harvest / sell."""
        s = self.refresh(force=True)
        cfg = self.config
        rows = self._load_outcomes()
        T = float(cfg.get('harvest_trigger_pct', 4.0))
        S = float(cfg.get('max_single_loss_pct', 3.0))
        c = s.get('cost') or float(cfg.get('tx_cost_pct', 1.5))

        # The middle-ground trades: outcomes that landed in the harvestable band [0, target).
        middle = []
        for r in rows:
            ret = r.get('ret'); f = r.get('f') or {}
            if ret is None or not (0.0 <= float(ret) < T):
                continue
            middle.append({'ret': round(float(ret), 2),
                           'change_1h': round(float(f.get('change_1h', 0)), 1),
                           'rvol': round(float(f.get('rvol', 0)), 1),
                           'spread': round(float(f.get('spread_pct', 0)), 2)})
        n = max(1, len(rows))
        bands = {
            'middle_count': len(middle),
            'middle_pct':   round(len(middle) / n * 100, 1),
            'fail_pct':     round(sum(1 for r in rows if (r.get('ret') or 0) < 0) / n * 100, 1),
            'rip_pct':      round(sum(1 for r in rows if (r.get('ret') or 0) >= 6) / n * 100, 1),
            'samples':      middle[-15:],
        }

        # Potential profit: expectancy now vs at a target win rate (breakeven + 5pts), projected.
        be = s.get('breakeven')
        def project(p):
            e = expectancy(p, T, S, c) / 100.0
            daily = (1 + e) ** 3 - 1                     # ~3 round-trips/day illustrative
            annual = (1 + daily) ** 252 - 1
            return {'win_rate': round(p, 3), 'ev_trade': round(e * 100, 3),
                    'ev_day': round(daily * 100, 3),
                    'annual': round(annual * 100, 1) if abs(annual) < 100 else None,
                    'annual_x': round(1 + annual, 1) if annual >= 1 else None}
        profit = {'current': project(s['win_rate']) if s.get('win_rate') is not None else None,
                  'target':  project(min(0.95, (be or 0.6) + 0.05)) if be else None}

        # Guardrails — the windows of profitability the data draws, and what's automated.
        spread_ceiling = round(T * 0.40, 2)
        guardrails = {
            'buy': {
                'change_1h': [cfg.get('harvest_min_change_1h'), cfg.get('harvest_max_change_1h')],
                'rvol':      [cfg.get('harvest_min_rvol'), cfg.get('harvest_max_rvol')],
                'price_min': cfg.get('harvest_min_price'),
                'spread_max': spread_ceiling,
                'note': 'moderate movers, tight spread, liquid — the predictable middle',
            },
            'harvest': {
                'target_now': T,
                'target_rec': (s.get('rec_target') or {}).get('target'),
                'rec_ev':     (s.get('rec_target') or {}).get('ev'),
                'note': 'harvest a slice + reinvest each time the target is cleared',
            },
            'sell': {
                'stop_now': S,
                'stop_rec': (s.get('rec_stop') or {}).get('stop'),
                'stop_basis': (s.get('rec_stop') or {}).get('basis'),
                'exit_trigger': cfg.get('exit_trigger_pct'),
                'note': 'fast loss-cut + margin-compression exit',
            },
            'automation': {
                'exposure_scale': s.get('exposure_scale'),
                'state': s.get('note'),
                'enabled': bool(cfg.get('adaptive_policy_enabled', True)),
                'min_n': int(cfg.get('adaptive_min_n', 30)),
                'floor': cfg.get('adaptive_exposure_floor'),
                'cap': cfg.get('adaptive_exposure_cap'),
            },
        }
        return {
            'thesis':     s,
            'bands':      bands,
            'profit':     profit,
            'guardrails': guardrails,
            'regime_on':  bool(cfg.get('harvest_regime', True)),
            'paper_safe': not (cfg.get('live_mode') or cfg.get('broker_execution')),
            'status':     self._status(),
        }

    def _status(self):
        """Definitive at-a-glance state: real-money vs paper, and whether the market is open."""
        cfg = self.config
        live = bool(cfg.get('live_mode') or cfg.get('broker_execution'))
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo('America/New_York'))
            mins = now.hour * 60 + now.minute
            mkt_open = (now.weekday() < 5 and (9 * 60 + 30) <= mins < (16 * 60))
            et = now.strftime('%a %H:%M ET')
        except Exception:
            mkt_open, et = None, ''
        return {
            'mode':        'LIVE' if live else 'PAPER',
            'real_money':  live,
            'realtime':    bool(cfg.get('realtime_subscribed')),
            'market_open': mkt_open,
            'et':          et,
        }
