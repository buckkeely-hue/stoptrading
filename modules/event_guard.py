"""
EventGuard — binary-event avoidance (loss-avoidance edge).

Two kinds of scheduled binary risk that wreck a steady-harvest record:
  1. MARKET-WIDE macro events (FOMC decision days, CPI releases) — the whole tape gaps.
       block_new_buys_today()  -> True on those days; don't initiate new entries.
  2. PER-TICKER binary catalysts (FDA PDUFA / AdCom for biotech pennies, etc.).
       should_block_buy(sym)   -> like EarningsGuard, but for a known catalyst date.
       should_force_exit(sym)  -> shed the position the day before the event.

FOMC/CPI dates are publicly scheduled and seeded below (override via config
`fomc_dates` / `cpi_dates`). Per-ticker dates come from config `binary_event_dates`
(a dict SYMBOL -> 'YYYY-MM-DD'), so a known PDUFA date can be added in seconds.

Everything is config-gated (`event_guard_enabled`, default True) and fails safe to
"don't block" on any error.
"""

import threading
from datetime import datetime, date

# Publicly-scheduled FOMC decision (announcement) days. Update yearly / override in config.
_FOMC = ['2026-01-28', '2026-03-18', '2026-04-29', '2026-06-17',
         '2026-07-29', '2026-09-16', '2026-10-28', '2026-12-09']
# CPI release days (best-effort ~mid-month, 08:30 ET) — override in config if a date shifts.
_CPI  = ['2026-01-13', '2026-02-11', '2026-03-11', '2026-04-10', '2026-05-12', '2026-06-10',
         '2026-07-14', '2026-08-12', '2026-09-11', '2026-10-13', '2026-11-12', '2026-12-10']


class EventGuard:
    def __init__(self, config):
        self.config  = config
        self._lock   = threading.Lock()
        self.running = False

    def start(self):
        self.running = True   # dates are static / config-driven; no background thread needed

    def stop(self):
        self.running = False

    def _enabled(self):
        return bool(self.config.get('event_guard_enabled', True))

    def _today(self):
        return date.today().isoformat()

    def _fomc(self):  return set(self.config.get('fomc_dates', _FOMC))
    def _cpi(self):   return set(self.config.get('cpi_dates', _CPI))
    def _extra(self): return set(self.config.get('extra_macro_event_dates', []))

    def macro_event_today(self):
        d = self._today()
        if d in self._fomc():  return 'FOMC'
        if d in self._cpi():   return 'CPI'
        if d in self._extra(): return 'MACRO'
        return None

    def block_new_buys_today(self):
        """True on a scheduled market-wide binary day (FOMC / CPI) — skip new entries."""
        return self._enabled() and self.macro_event_today() is not None

    # ── Per-ticker binary catalysts (FDA, etc.) ──────────────────────────────
    def _ticker_date(self, sym):
        v = (self.config.get('binary_event_dates', {}) or {}).get(sym.upper())
        if not v:
            return None
        try:
            return datetime.strptime(v, '%Y-%m-%d').date()
        except Exception:
            return None

    def days_to_event(self, sym):
        d = self._ticker_date(sym)
        return None if not d else (d - date.today()).days

    def should_block_buy(self, sym):
        if not self._enabled():
            return False
        n = self.days_to_event(sym)
        return n is not None and 0 <= n <= int(self.config.get('binary_block_days', 3))

    def should_force_exit(self, sym):
        if not self._enabled():
            return False
        n = self.days_to_event(sym)
        return n is not None and 0 <= n <= int(self.config.get('binary_exit_days', 1))

    def event_note(self, sym):
        n = self.days_to_event(sym)
        if n is None:
            return ''
        return 'binary catalyst in %d day(s)' % n

    def get_summary(self):
        return {'running': self.running, 'macro_today': self.macro_event_today(),
                'block_new_today': self.block_new_buys_today(),
                'ticker_events': len(self.config.get('binary_event_dates', {}) or {})}
