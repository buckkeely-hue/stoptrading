"""
Daily trading scheduler — "set it and forget it" automation.

On server start: if autopilot.json shows running=True, auto-resume immediately.
At 9:30 AM ET Mon–Fri: if auto_start_enabled in config, start/resume AutoPilot.
At 4:05 PM ET: log end-of-day snapshot.
"""
import threading
import time
import json
import os
from datetime import date, datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    _SCHED_ET = ZoneInfo('America/New_York')
except Exception:
    _SCHED_ET = None

SCHED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scheduler.json')

# ── US NYSE Holiday Calendar ───────────────────────────────────────────────

_holiday_cache = {}   # year -> set of 'YYYY-MM-DD' strings


def _easter(year):
    """Anonymous Gregorian algorithm — returns Easter Sunday as a date."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19*a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2*e + 2*i - h - k) % 7
    m = (a + 11*h + 22*l) // 451
    month = (h + l - 7*m + 114) // 31
    day   = (h + l - 7*m + 114) % 31 + 1
    return date(year, month, day)


def _nth_weekday(year, month, weekday, n):
    """nth occurrence of weekday (Mon=0) in month; negative n counts from end."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        d += timedelta(weeks=n - 1)
    else:
        import calendar
        last = calendar.monthrange(year, month)[1]
        d = date(year, month, last)
        d -= timedelta(days=(d.weekday() - weekday) % 7)
    return d


def _observed(d):
    """Shift weekend holidays to the nearest weekday (Sat→Fri, Sun→Mon)."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _nyse_holidays(year):
    """Return set of NYSE market holiday dates for *year* as 'YYYY-MM-DD' strings."""
    if year in _holiday_cache:
        return _holiday_cache[year]

    holidays = {
        _observed(date(year, 1,  1)),                       # New Year's Day
        _nth_weekday(year, 1,  0, 3),                       # MLK Day (3rd Mon Jan)
        _nth_weekday(year, 2,  0, 3),                       # Presidents' Day (3rd Mon Feb)
        _easter(year) - timedelta(days=2),                  # Good Friday
        _nth_weekday(year, 5,  0, -1),                      # Memorial Day (last Mon May)
        _observed(date(year, 6, 19)),                       # Juneteenth
        _observed(date(year, 7,  4)),                       # Independence Day
        _nth_weekday(year, 9,  0,  1),                      # Labor Day (1st Mon Sep)
        _nth_weekday(year, 11, 3,  4),                      # Thanksgiving (4th Thu Nov)
        _observed(date(year, 12, 25)),                      # Christmas
    }
    result = {d.strftime('%Y-%m-%d') for d in holidays}
    _holiday_cache[year] = result
    return result


def _is_trading_day(dt):
    """True only on NYSE trading days — excludes weekends and all US market holidays."""
    if dt.weekday() >= 5:
        return False
    date_str = dt.strftime('%Y-%m-%d')
    return date_str not in _nyse_holidays(dt.year)


def _next_trading_day(from_dt):
    """Return the next NYSE trading day after from_dt (ET date)."""
    d = from_dt.date() + timedelta(days=1)
    for _ in range(14):   # safety: never more than 2 weeks of holidays in a row
        dt = datetime(d.year, d.month, d.day, tzinfo=from_dt.tzinfo)
        if _is_trading_day(dt):
            return d
        d += timedelta(days=1)
    return d


def _now_et():
    if _SCHED_ET:
        return datetime.now(_SCHED_ET)
    return datetime.now(timezone.utc) + timedelta(hours=-4)


class DailyScheduler:
    def __init__(self, autopilot, paper_trader, load_config):
        self.autopilot     = autopilot
        self.paper         = paper_trader
        self._load_config  = load_config
        self._thread       = None
        self._started_dates = set()
        self._eod_dates     = set()
        self.last_action    = None
        self.next_action    = 'Scheduler starting...'
        self._load()

    def _load(self):
        try:
            if os.path.exists(SCHED_FILE):
                with open(SCHED_FILE) as f:
                    d = json.load(f)
                self._started_dates = set(d.get('started_dates', []))
                self._eod_dates     = set(d.get('eod_dates', []))
                self.last_action    = d.get('last_action')
        except Exception:
            pass

        # Auto-resume autopilot if it was running when server last died
        try:
            from modules.harvester import AUTOPILOT_FILE
            if os.path.exists(AUTOPILOT_FILE) and not self.autopilot.running:
                with open(AUTOPILOT_FILE) as f:
                    saved = json.load(f)
                if saved.get('running', False):
                    result = self.autopilot.resume()
                    if result.get('ok'):
                        self.last_action = 'Auto-resumed after restart at {}'.format(
                            _now_et().strftime('%Y-%m-%d %H:%M ET'))
        except Exception:
            pass

    def _save(self):
        try:
            with open(SCHED_FILE, 'w') as f:
                json.dump({
                    'started_dates': list(self._started_dates)[-60:],
                    'eod_dates':     list(self._eod_dates)[-60:],
                    'last_action':   self.last_action,
                }, f)
        except Exception:
            pass

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(30)

    def _tick(self):
        now      = _now_et()
        date_str = now.strftime('%Y-%m-%d')
        m        = now.hour * 60 + now.minute

        cfg           = self._load_config()
        auto_enabled  = cfg.get('auto_start_enabled', False)
        daily_balance = float(cfg.get('daily_balance', 500.0))

        market_open = 9 * 60 + 30   # 9:30 AM ET
        eod_time    = 16 * 60 + 5   # 4:05 PM ET

        if not _is_trading_day(now):
            next_day = _next_trading_day(now)
            label = next_day.strftime('%A %b %-d')
            self.next_action = 'Market closed — next trading day: {} at 9:30 AM ET'.format(label)
            return

        # Auto-start at market open on a trading day
        if auto_enabled and market_open <= m < 16 * 60 and date_str not in self._started_dates:
            if not self.autopilot.running:
                if self.autopilot.stats.get('started') is None:
                    result = self.autopilot.start(daily_balance)
                    verb = 'Auto-started (fresh $%.0f balance)' % daily_balance
                else:
                    result = self.autopilot.resume()
                    verb = 'Auto-resumed at market open'
                if result.get('ok'):
                    self._started_dates.add(date_str)
                    self.last_action = '{} — {}'.format(now.strftime('%Y-%m-%d %H:%M'), verb)
                    self._save()
            else:
                self._started_dates.add(date_str)

        # EOD snapshot at 4:05 PM
        if m >= eod_time and date_str not in self._eod_dates:
            self._eod_snapshot()
            self._eod_dates.add(date_str)
            self._save()

        # Update next-action label
        if auto_enabled:
            if m < market_open:
                mins = market_open - m
                h, mn = divmod(mins, 60)
                self.next_action = 'Auto-start in {}h {}m at 9:30 AM ET'.format(h, mn) if h else \
                                   'Auto-start in {}m at 9:30 AM ET'.format(mn)
            elif m < 16 * 60:
                self.next_action = 'Running — EOD snapshot at 4:05 PM ET'
            else:
                next_day = _next_trading_day(now)
                self.next_action = 'Market closed — next trading day: {} at 9:30 AM ET'.format(
                    next_day.strftime('%A %b %-d'))
        else:
            self.next_action = 'Auto-start disabled — click Launch to start manually'

    def _eod_snapshot(self):
        state   = self.paper.get_state()
        balance = state.get('balance', 0)
        ap      = self.autopilot.stats
        pnl     = ap.get('total_profit', 0)
        trades  = ap.get('total_trades', 0)
        wins    = ap.get('wins', 0)
        losses  = ap.get('losses', 0)
        wr      = wins / max(wins + losses, 1) * 100

        self.autopilot._entry('EOD',
            'Market close — Balance: ${:.2f} | Realized P&L: {:+.2f} | {} trades | {:.0f}% win rate'.format(
                balance, pnl, trades, wr))
        self.last_action = 'EOD snapshot {}'.format(_now_et().strftime('%Y-%m-%d %H:%M ET'))

        # Send daily summary notification
        try:
            from modules.notify import msg_eod
            self.autopilot._notify(msg_eod(pnl, trades, wins, losses))
        except Exception:
            pass

    def get_status(self):
        now = _now_et()
        cfg = self._load_config()
        return {
            'auto_start_enabled': cfg.get('auto_start_enabled', False),
            'daily_balance':      float(cfg.get('daily_balance', 500.0)),
            'daily_limit':        float(cfg.get('daily_spend_limit', 100.0)),
            'next_action':        self.next_action,
            'last_action':        self.last_action,
            'is_trading_day':     _is_trading_day(now),
            'et_time':            now.strftime('%I:%M %p'),
        }
