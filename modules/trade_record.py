"""
TradeRecorder — the immutable trade-lifecycle ledger (trades.jsonl).

Every executed position's COMPLETE story in one record: entry context (score, spread, cost,
P(win), regime, exposure scale) → every add / harvest / ratchet → exit → realized P&L and
return. This is the durable research asset the paper phase is meant to bank — distinct from the
scattered, capped, rotating sources (paper_trades.history caps at 50; the autopilot log rotates
at 1000; accounting tracks money, not context).

Fed centrally by the paper-trader callback (same mechanism as accounting / budget recycle), so
it captures EVERY buy and sell with no per-exit-site wiring. Entry context is attached by the
harvester at entry. Finalize is DEFERRED one tick: when a position's shares reach zero the record
moves to a holding area, and flush_closed() (called each tick, after the harvester has logged the
exit reason) infers the reason from the live log and writes the immutable record.

All best-effort and exception-swallowing — recording must never break trading.
"""
import threading
from datetime import datetime

from modules.io_safe import append_jsonl

import os
TRADES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'trades.jsonl')

# autopilot log actions that denote an exit/harvest, newest-first inference at finalize
_EXIT_ACTIONS = ('STOP-LOSS', 'PREDICT-EXIT', 'FINAL-EXIT', 'TRAIL-STOP', 'EVASIVE',
                 'EOD-CLOSE', 'GAP-EXIT', 'TIME-EXIT', 'TIME-HARVEST', 'HARVEST', 'EARNINGS-EXIT')


class TradeRecorder:
    def __init__(self, autopilot):
        self.ap      = autopilot
        self._lock   = threading.Lock()
        self._active = {}    # symbol -> live lifecycle record
        self._ctx    = {}    # symbol -> pending entry context (set just before/after the buy)
        self._closing = []   # records whose shares hit 0, awaiting reason + write next tick

    # ── entry context (called by the harvester at entry) ─────────────────────────
    def set_context(self, symbol, ctx):
        try:
            with self._lock:
                if symbol in self._active:
                    self._active[symbol].setdefault('context', {}).update(ctx)
                else:
                    self._ctx[symbol] = dict(ctx)
        except Exception:
            pass

    # ── central paper-trade callback (every BUY / SELL) ──────────────────────────
    def on_trade(self, event):
        try:
            if not self.ap.config.get('trade_record_enabled', True):
                return   # disabled (e.g. backtest sweeps) so replays don't append to the live ledger
            typ = event.get('type'); sym = event.get('symbol')
            if not sym:
                return
            shares = int(event.get('shares', 0) or 0)
            price  = float(event.get('price', 0) or 0)
            total  = float(event.get('total', 0) or 0)
            now    = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with self._lock:
                if typ == 'BUY':
                    rec = self._active.get(sym)
                    if rec is None:
                        rec = {
                            'symbol': sym,
                            'src': getattr(getattr(self.ap, 'predictor', None), '_src', 'live'),
                            'opened': now, 'entry_price': price,
                            'shares': 0, 'peak_shares': 0, 'cost_basis': 0.0,
                            'context': self._ctx.pop(sym, {}),
                            'events': [], 'harvests': 0, 'adds': 0,
                            'total_harvested': 0.0, 'realized_pnl': 0.0,
                        }
                        self._active[sym] = rec
                    else:
                        rec['adds'] += 1
                    rec['shares'] += shares
                    rec['peak_shares'] = max(rec['peak_shares'], rec['shares'])
                    rec['cost_basis'] += total
                    rec['events'].append({'t': now, 'type': 'ADD' if rec['events'] else 'ENTRY',
                                          'shares': shares, 'price': round(price, 4)})

                elif typ == 'SELL':
                    rec = self._active.get(sym)
                    if rec is None:
                        return   # sell of a position opened before the recorder existed — skip
                    pnl = float(event.get('pnl', 0) or 0)
                    rec['shares'] -= shares
                    rec['realized_pnl'] += pnl
                    partial = rec['shares'] > 0
                    rec['events'].append({'t': now, 'type': 'HARVEST' if partial else 'EXIT',
                                          'shares': shares, 'price': round(price, 4),
                                          'pnl': round(pnl, 2)})
                    if partial:
                        rec['harvests'] += 1
                        rec['total_harvested'] += total
                    else:
                        rec['closed'] = now
                        self._active.pop(sym, None)
                        self._closing.append(rec)
        except Exception:
            pass

    # ── deferred finalize (called once per tick, after exit reason is logged) ─────
    def flush_closed(self):
        try:
            with self._lock:
                pending = self._closing
                self._closing = []
            for rec in pending:
                rec['exit_reason'] = self._infer_reason(rec['symbol'])
                cost = rec.get('cost_basis', 0.0)
                rec['realized_pnl']    = round(rec['realized_pnl'], 2)
                rec['total_harvested'] = round(rec.get('total_harvested', 0.0), 2)
                rec['cost_basis']      = round(cost, 2)
                rec['return_pct'] = round(rec['realized_pnl'] / cost * 100.0, 3) if cost > 0 else 0.0
                try:
                    o = datetime.strptime(rec['opened'], '%Y-%m-%d %H:%M:%S')
                    c = datetime.strptime(rec.get('closed', rec['opened']), '%Y-%m-%d %H:%M:%S')
                    rec['duration_min'] = round((c - o).total_seconds() / 60.0, 1)
                except Exception:
                    rec['duration_min'] = None
                rec.pop('shares', None)   # always 0 at close
                append_jsonl(TRADES_FILE, rec, max_bytes=16_000_000)
        except Exception:
            pass

    def _infer_reason(self, symbol):
        """Most-recent exit/harvest action for this symbol from the live autopilot log."""
        try:
            log = getattr(self.ap, 'log', [])
            for e in reversed(log[-60:]):
                if e.get('action') in _EXIT_ACTIONS and symbol in str(e.get('note', '')):
                    return e.get('action')
        except Exception:
            pass
        return 'CLOSE'

    # ── dashboard / analysis accessor ────────────────────────────────────────────
    def recent(self, n=50):
        out = []
        try:
            if os.path.exists(TRADES_FILE):
                import json
                for line in open(TRADES_FILE):
                    line = line.strip()
                    if line:
                        try: out.append(json.loads(line))
                        except Exception: pass
        except Exception:
            pass
        return out[-n:]
