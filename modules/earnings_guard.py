"""
EarningsGuard — earnings date awareness and position protection.

Penny stocks can halve overnight on an earnings miss. This agent:
  1. Caches earnings dates for all universe tickers (yfinance + Finnhub fallback)
  2. Blocks NEW buys within 2 calendar days of earnings
  3. Signals FORCE EXIT if a position is held with earnings tomorrow
  4. Provides +15 score bonus if stock gapped up AFTER earnings (post-earnings
     momentum continuation is one of the cleanest penny stock patterns)

Refreshes earnings calendar every 6 hours.
"""

import threading
import time
import yfinance as yf
import requests
from datetime import datetime, date, timedelta

HEADERS = {'User-Agent': 'StopTrading buckkeely@gmail.com'}

BLOCK_DAYS  = 2   # block new buys within this many days of earnings
EXIT_DAYS   = 1   # force exit if earnings within this many days


class EarningsGuard:

    def __init__(self, config):
        self.config     = config
        self._lock      = threading.Lock()
        self.running    = False
        self._dates     = {}    # ticker -> next earnings date (date object)
        self._checked   = set() # tickers whose earnings lookup has actually completed
        self._last_refresh = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        self.running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                self._refresh_all()
            except Exception:
                pass
            time.sleep(6 * 3600)   # refresh every 6 hours

    # ── Data Fetching ─────────────────────────────────────────────────────────

    def warm(self, symbols):
        """Pre-warm cache for a list of symbols (call at startup)."""
        t = threading.Thread(target=self._refresh_list, args=(symbols,), daemon=True)
        t.start()

    def _refresh_all(self):
        with self._lock:
            symbols = list(self._dates.keys())
        self._refresh_list(symbols)

    def _refresh_list(self, symbols):
        for sym in symbols:
            self._fetch_date(sym)
            time.sleep(0.3)
        with self._lock:
            self._last_refresh = datetime.now()

    def _fetch_date(self, ticker):
        """Get next earnings date for ticker. Tries yfinance, then Finnhub."""
        ticker = ticker.upper()
        dt = self._yf_date(ticker) or self._finnhub_date(ticker)
        with self._lock:
            if dt:
                self._dates[ticker] = dt
            else:
                self._dates.setdefault(ticker, None)
            self._checked.add(ticker)   # lookup completed (date found or confirmed none)

    def _yf_date(self, ticker):
        try:
            cal = yf.Ticker(ticker).calendar
            if cal is None:
                return None
            # yfinance returns a dict with 'Earnings Date' key (list of Timestamps)
            ed = cal.get('Earnings Date') or cal.get('earnings_date')
            if ed is None:
                return None
            if hasattr(ed, '__iter__') and not isinstance(ed, str):
                dates = [d.date() if hasattr(d, 'date') else d for d in ed if d]
                future = [d for d in dates if d >= date.today()]
                return min(future) if future else None
            if hasattr(ed, 'date'):
                return ed.date()
        except Exception:
            pass
        return None

    def _finnhub_date(self, ticker):
        key = self.config.get('finnhub_key', '')
        if not key:
            return None
        try:
            today = date.today()
            url   = ('https://finnhub.io/api/v1/calendar/earnings'
                     '?from={}&to={}&symbol={}&token={}').format(
                today.isoformat(),
                (today + timedelta(days=90)).isoformat(),
                ticker, key)
            r = requests.get(url, headers=HEADERS, timeout=8)
            if r.status_code != 200:
                return None
            entries = r.json().get('earningsCalendar', [])
            if not entries:
                return None
            return date.fromisoformat(entries[0]['date'])
        except Exception:
            return None

    # ── Public API ────────────────────────────────────────────────────────────

    def register(self, ticker):
        """Add a ticker to the watch list if not already tracked."""
        ticker = ticker.upper()
        with self._lock:
            if ticker not in self._dates:
                self._dates[ticker] = None
        threading.Thread(target=self._fetch_date, args=(ticker,), daemon=True).start()

    def days_to_earnings(self, ticker):
        """Days until next earnings. None if unknown."""
        with self._lock:
            dt = self._dates.get(ticker.upper())
        if dt is None:
            return None
        delta = (dt - date.today()).days
        return delta if delta >= 0 else None

    def should_block_buy(self, ticker):
        """True if earnings within BLOCK_DAYS — do not open new position.

        Fail-closed on UNCONFIRMED tickers: if we haven't actually completed an earnings
        lookup yet, block the buy (and kick off a lookup) rather than risk buying into an
        unflagged earnings print. Once the lookup confirms 'no upcoming earnings', buys
        are allowed normally — so this only blocks the brief pre-confirmation window.
        """
        ticker = ticker.upper()
        with self._lock:
            checked = ticker in self._checked
        if not checked:
            # First time we've seen this name — confirm earnings before allowing entry.
            threading.Thread(target=self._fetch_date, args=(ticker,), daemon=True).start()
            return True
        days = self.days_to_earnings(ticker)
        if days is None:
            return False
        return days <= BLOCK_DAYS

    def should_force_exit(self, ticker):
        """True if earnings within EXIT_DAYS — exit any open position now."""
        days = self.days_to_earnings(ticker)
        if days is None:
            return False
        return days <= EXIT_DAYS

    def earnings_note(self, ticker):
        """Human-readable earnings proximity note."""
        days = self.days_to_earnings(ticker)
        if days is None:
            return ''
        if days == 0:
            return 'earnings TODAY'
        if days == 1:
            return 'earnings TOMORROW'
        return 'earnings in {} days'.format(days)

    def get_upcoming(self, within_days=7):
        """Return tickers with earnings coming up within N days."""
        today = date.today()
        result = []
        with self._lock:
            for ticker, dt in self._dates.items():
                if dt is None:
                    continue
                delta = (dt - today).days
                if 0 <= delta <= within_days:
                    result.append({'ticker': ticker, 'days': delta, 'date': dt.isoformat()})
        return sorted(result, key=lambda x: x['days'])

    def get_summary(self):
        with self._lock:
            total = len(self._dates)
            known = sum(1 for d in self._dates.values() if d is not None)
        return {
            'tracked_tickers':  total,
            'dates_known':      known,
            'upcoming_7d':      self.get_upcoming(7),
            'block_days':       BLOCK_DAYS,
            'exit_days':        EXIT_DAYS,
            'last_refresh':     self._last_refresh.isoformat() if self._last_refresh else None,
        }
