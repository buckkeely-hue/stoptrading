"""
IBKR TWS Real-Time Feed — Interactive Brokers market data via TWS API.

Provides:
  - Real-time Level 1 quotes (last price, bid, ask)
  - Level 2 market depth (order book)
  - Short loan borrow rate (Cost-to-Borrow) — free with IBKR account
  - Halt/resume events via streaming market data

Setup (one-time):
  1. Open a paper or funded account at interactivebrokers.com
  2. Download Trader Workstation (TWS) or IB Gateway
  3. In TWS: File → Global Configuration → API → Settings
     - Enable ActiveX and Socket Clients
     - Socket port: 7497 (paper) or 7496 (live)
     - Uncheck "Read-Only API"
  4. pip install ib_insync
  5. Set in StopTrading settings: use_ibkr_feed = true, ibkr_port = 7497

Exchange data subscriptions (in TWS Account Management):
  - "Nasdaq Basic" (~$5/month) — real-time L1 for all Nasdaq-listed
  - "FINRA/OTC" (~$10/month) — real-time OTC penny stock quotes
  - Both required for full penny stock coverage

Cost-to-Borrow (borrow rate) is FREE with any IBKR account via the
Securities Lending Borrow (SLB) data — no subscription required.
"""

import threading
import time
from datetime import datetime

_IB_AVAILABLE = False
try:
    from ib_insync import IB, Stock, IBTimeBarList
    _IB_AVAILABLE = True
except ImportError:
    pass


class IBKRFeed:
    """
    Manages a single persistent TWS connection in a background thread.
    All price/borrow data is stored in a thread-safe cache so other modules
    can read without blocking on network calls.
    """

    def __init__(self, config):
        self.config  = config
        self._lock   = threading.Lock()
        self._prices = {}       # symbol → {'last', 'bid', 'ask', 'ts'}
        self._borrow = {}       # symbol → {'rate_pct', 'available', 'ts'}
        self._depth  = {}       # symbol → {'bids': [...], 'asks': [...]}
        self._ib     = None
        self._req_ids = {}      # symbol → reqId
        self.connected = False
        self.running   = False
        self.error_log = []
        self._symbols  = set()

    def start(self, symbols=None):
        if not _IB_AVAILABLE:
            self._log('ib_insync not installed — run: pip install ib_insync')
            return False
        if not self.config.get('use_ibkr_feed', False):
            return False
        self.running = True
        if symbols:
            self._symbols.update(s.upper() for s in symbols)
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        return True

    def stop(self):
        self.running = False
        try:
            if self._ib and self._ib.isConnected():
                self._ib.disconnect()
        except Exception:
            pass

    def add_symbol(self, symbol):
        sym = symbol.upper()
        self._symbols.add(sym)
        if self.connected and self._ib:
            self._subscribe(sym)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _run(self):
        host      = self.config.get('ibkr_host', '127.0.0.1')
        port      = int(self.config.get('ibkr_port', 7497))
        client_id = int(self.config.get('ibkr_client_id', 10))

        while self.running:
            try:
                self._ib = IB()
                self._ib.connect(host, port, clientId=client_id, timeout=10, readonly=True)
                self.connected = True
                self._log('Connected to TWS {}:{}'.format(host, port))

                for sym in list(self._symbols):
                    self._subscribe(sym)

                # Run the event loop until disconnected or stopped
                while self.running and self._ib.isConnected():
                    self._ib.sleep(1)

            except Exception as e:
                self._log('Connection error: {}'.format(str(e)))
                self.connected = False
            finally:
                try:
                    if self._ib:
                        self._ib.disconnect()
                except Exception:
                    pass
                self.connected = False

            if self.running:
                time.sleep(30)   # retry after 30s

    def _subscribe(self, symbol):
        if not self._ib or not self._ib.isConnected():
            return
        try:
            contract = Stock(symbol, 'SMART', 'USD')
            # genericTickList='236' = shortable shares; '233' = short rate
            ticker = self._ib.reqMktData(contract, genericTickList='233,236', snapshot=False)

            def on_tick(ticker):
                price = ticker.last or ticker.close or 0
                with self._lock:
                    self._prices[symbol] = {
                        'last':  float(price),
                        'bid':   float(ticker.bid or 0),
                        'ask':   float(ticker.ask or 0),
                        'ts':    time.time(),
                    }
                    # Tick 233 = short borrow fee rate (annualized %)
                    # Tick 236 = number of shares available to short
                    rate = getattr(ticker, 'shortTermBorrowRate', None)
                    avail = getattr(ticker, 'shortableShares', None)
                    if rate is not None:
                        self._borrow[symbol] = {
                            'rate_pct':  round(float(rate), 2),
                            'available': int(avail) if avail else 0,
                            'ts':        time.time(),
                        }

            ticker.updateEvent += on_tick
            self._req_ids[symbol] = ticker.reqId
        except Exception as e:
            self._log('Subscribe {} failed: {}'.format(symbol, str(e)))

    def _log(self, msg):
        ts = datetime.now().strftime('%H:%M:%S')
        self.error_log.append('[{}] {}'.format(ts, msg))
        self.error_log = self.error_log[-20:]

    # ── Public read API ───────────────────────────────────────────────────────

    def get_price(self, symbol):
        """Last traded price. Returns 0 if not subscribed or not connected."""
        with self._lock:
            d = self._prices.get(symbol.upper(), {})
            return d.get('last', 0)

    def get_quote(self, symbol):
        """Full quote dict: {last, bid, ask, ts}."""
        with self._lock:
            return dict(self._prices.get(symbol.upper(), {}))

    def get_borrow_rate(self, symbol):
        """
        Cost-to-Borrow rate (annualized %). Free with IBKR account.
        Returns 0 if unavailable.
        >50% CTB = shorts are trapped, squeeze candidate.
        """
        with self._lock:
            d = self._borrow.get(symbol.upper(), {})
            return d.get('rate_pct', 0)

    def borrow_score(self, symbol):
        """
        Scoring bonus from CTB rate:
          >100%:  +25  (shorts desperate — explosive squeeze fuel)
          50–100%: +15 (shorts uncomfortable)
          20–50%:  +8  (elevated)
          <20%:    0
        """
        rate = self.get_borrow_rate(symbol)
        if rate >= 100:
            return 25
        if rate >= 50:
            return 15
        if rate >= 20:
            return 8
        return 0

    def status(self):
        return {
            'available':    _IB_AVAILABLE,
            'connected':    self.connected,
            'enabled':      self.config.get('use_ibkr_feed', False),
            'symbols':      len(self._symbols),
            'prices_cached': len(self._prices),
            'borrow_cached': len(self._borrow),
            'errors':       self.error_log[-5:],
        }
