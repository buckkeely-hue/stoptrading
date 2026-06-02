"""
MarketDataFeed — swappable data abstraction with an ENFORCED mode contract.

The data layer was silently degrading to 15-minute-delayed yfinance with no signal. This makes
the feed a first-class, swappable interface that KNOWS and reports its own state:

    REAL_TIME  — full real-time SIP (suitable for day-trading pennies)
    DELAYED    — delayed snapshot, or IEX-only (partial volume → blind to most penny flow)
    DOWN       — not configured / unreachable

Entitlement-probed at startup and re-probed periodically, so "are we real-time?" is never a
mystery, the dashboard always shows the truth, and timing-sensitive trades can refuse a
degraded feed (block_trades_on_delayed). Implementations in priority order: Alpaca
(broker-aligned real-time SIP), Polygon (real-time IF entitled), yfinance (always delayed).
Flip on a real-time subscription and the manager upgrades automatically — no code change.
"""
import time
import threading
import requests

REAL_TIME, DELAYED, DOWN = 'REAL_TIME', 'DELAYED', 'DOWN'
_PROBE_SYM = 'AAPL'   # liquid symbol that always has a recent trade


class MarketDataFeed:
    name = 'base'
    def __init__(self, config=None):  self.config = config
    def probe(self):   return DOWN
    def quote(self, symbol):  return None          # {price, ts_epoch, source}


class AlpacaFeed(MarketDataFeed):
    """Broker-aligned real-time SIP (the recommended long-term source)."""
    name = 'alpaca'

    def __init__(self, cfg):
        live = bool(cfg.get('live_mode'))
        self.key    = (cfg.get('alpaca_live_key') if live else cfg.get('alpaca_paper_key')) or ''
        self.secret = (cfg.get('alpaca_live_secret') if live else cfg.get('alpaca_paper_secret')) or ''
        self.base   = 'https://data.alpaca.markets'
        self._feed  = None   # 'sip' | 'iex' once probed

    def _hdr(self):
        return {'APCA-API-KEY-ID': self.key, 'APCA-API-SECRET-KEY': self.secret}

    def probe(self):
        if not (self.key and self.secret):
            return DOWN
        # Full SIP entitlement is what matters for pennies; IEX-only sees ~2-3% of volume.
        for feed, mode in (('sip', REAL_TIME), ('iex', DELAYED)):
            try:
                r = requests.get('%s/v2/stocks/%s/trades/latest?feed=%s' % (self.base, _PROBE_SYM, feed),
                                 headers=self._hdr(), timeout=8)
                if r.status_code == 200 and r.json().get('trade'):
                    self._feed = feed
                    return mode
            except Exception:
                pass
        return DOWN

    def quote(self, symbol):
        if not (self.key and self.secret):
            return None
        try:
            feed = self._feed or 'iex'
            r = requests.get('%s/v2/stocks/%s/trades/latest?feed=%s' % (self.base, symbol.upper(), feed),
                             headers=self._hdr(), timeout=8)
            if r.status_code == 200:
                tr = r.json().get('trade', {})
                ts = tr.get('t', '')
                return {'price': float(tr.get('p', 0)), 'ts_epoch': _rfc3339(ts), 'source': 'alpaca-' + feed}
        except Exception:
            pass
        return None


class PolygonFeed(MarketDataFeed):
    name = 'polygon'

    def __init__(self, cfg):
        self.key = cfg.get('polygon_key', '') or ''

    def probe(self):
        if not self.key:
            return DOWN
        try:
            # /v2/last/trade is real-time-only; 200 => entitled real-time, 403 => delayed tier.
            r = requests.get('https://api.polygon.io/v2/last/trade/%s?apiKey=%s' % (_PROBE_SYM, self.key), timeout=8)
            if r.status_code == 200:
                return REAL_TIME
            if r.status_code == 403:
                return DELAYED          # entitled to delayed snapshot/aggregates only
            return DOWN
        except Exception:
            return DOWN

    def quote(self, symbol):
        if not self.key:
            return None
        try:
            r = requests.get('https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/%s?apiKey=%s'
                             % (symbol.upper(), self.key), timeout=8)
            if r.status_code == 200:
                snap = r.json().get('ticker', {})
                lt = snap.get('lastTrade', {})
                p = lt.get('p') or snap.get('day', {}).get('c') or 0
                return {'price': float(p or 0), 'ts_epoch': (lt.get('t', 0) / 1e9) if lt.get('t') else None,
                        'source': 'polygon-snapshot'}
        except Exception:
            pass
        return None


class YFinanceFeed(MarketDataFeed):
    name = 'yfinance'

    def probe(self):
        return DELAYED          # yfinance is structurally ~15-min delayed

    def quote(self, symbol):
        try:
            import yfinance as yf
            info = yf.Ticker(symbol).fast_info
            return {'price': float(info.last_price or 0), 'ts_epoch': None, 'source': 'yfinance'}
        except Exception:
            return None


def _rfc3339(s):
    try:
        from datetime import datetime
        s = s.replace('Z', '+00:00')
        if '.' in s:                       # trim nanoseconds to microseconds
            head, tail = s.split('.', 1)
            frac = ''.join(ch for ch in tail if ch.isdigit())[:6]
            tz = tail[len(''.join(ch for ch in tail if ch.isdigit())):]
            s = '%s.%s%s' % (head, frac, tz)
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


class FeedManager:
    """Holds feeds in priority order; reports the authoritative real-time/delayed/down state."""
    def __init__(self, config):
        self.config = config
        self.feeds  = [AlpacaFeed(config), PolygonFeed(config), YFinanceFeed(config)]
        self._lock  = threading.Lock()
        self._status = {}
        self._active = None
        self._mode   = DOWN
        self._last_probe = 0.0
        self.probe_all()
        threading.Thread(target=self._reprobe_loop, daemon=True, name='feed-probe').start()

    def probe_all(self):
        status = {}
        for f in self.feeds:
            try:
                status[f.name] = f.probe()
            except Exception:
                status[f.name] = DOWN
        active = (next((f for f in self.feeds if status.get(f.name) == REAL_TIME), None)
                  or next((f for f in self.feeds if status.get(f.name) == DELAYED), None))
        with self._lock:
            self._status = status
            self._active = active
            self._mode   = status.get(active.name, DOWN) if active else DOWN
            self._last_probe = time.time()

    def _reprobe_loop(self):
        while True:
            time.sleep(600)              # re-probe every 10 min — catches subscription changes
            try:
                self.probe_all()
            except Exception:
                pass

    def mode(self):
        with self._lock:
            return self._mode

    def is_real_time(self):
        return self.mode() == REAL_TIME

    def status(self):
        with self._lock:
            return {
                'mode':       self._mode,
                'real_time':  self._mode == REAL_TIME,
                'active':     self._active.name if self._active else None,
                'feeds':      dict(self._status),
                'last_probe': time.strftime('%H:%M:%S', time.localtime(self._last_probe)) if self._last_probe else None,
                'note': ('Full real-time SIP — suitable for day trading.' if self._mode == REAL_TIME
                         else 'DELAYED data (~15 min or IEX-only). Decisions are timing-compromised; '
                              'add a real-time SIP subscription (Alpaca/IBKR/Polygon) before live money.'
                         if self._mode == DELAYED else 'No market-data feed reachable.'),
            }
