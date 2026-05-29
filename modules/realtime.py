"""
Real-time multi-feed data engine.

Runs background threads collecting live data from:
  1. Finnhub WebSocket  — real-time trades (free key: finnhub.io)
  2. SEC EDGAR RSS      — Form 4 insider filings, no key required
  3. Unusual Whales     — options flow alerts, public endpoints
  4. StockTwits         — active trader sentiment, no key required
  5. yfinance 1-min     — price velocity, volume surge, order flow
  6. Polygon.io         — news headlines + quotes (free key)

All data flows into a shared RealtimeCache that the scoring engine reads.
"""

import threading
import time
import json
import requests
import yfinance as yf
from datetime import datetime
from collections import defaultdict, deque
import xml.etree.ElementTree as ET

try:
    import websocket
    HAS_WEBSOCKET = True
except ImportError:
    HAS_WEBSOCKET = False


# ── Shared Cache ──────────────────────────────────────────────────────────────

class RealtimeCache:
    """Thread-safe store for all live data signals."""

    def __init__(self):
        self._lock = threading.Lock()
        # price_ticks[symbol] = deque of (timestamp, price, volume) last 60 ticks
        self.price_ticks = defaultdict(lambda: deque(maxlen=60))
        # velocity[symbol] = {pct_1m, pct_5m, pct_15m, volume_ratio, ofi, price}
        self.velocity = {}
        # insider_alerts[symbol] = list of recent Form 4 events
        self.insider_alerts = defaultdict(list)
        # options_flow[symbol] = list of unusual options events
        self.options_flow = defaultdict(list)
        # twits[symbol] = {bulls, bears, messages, score}
        self.twits = {}
        # news[symbol] = list of {headline, sentiment_score, time}
        self.news = defaultdict(list)
        # last_update[symbol] = datetime
        self.last_update = {}
        # composite[symbol] = full scored signal dict
        self.composite = {}
        # price_callbacks: called as fn(symbol, price, volume) on every tick
        self._price_callbacks = []

    def register_price_callback(self, fn):
        """Register fn(symbol, price, volume) — called on every price tick from any stream."""
        self._price_callbacks.append(fn)

    def add_tick(self, symbol, price, volume):
        with self._lock:
            self.price_ticks[symbol].append((time.time(), price, volume))
            self.last_update[symbol] = datetime.now()
        for fn in self._price_callbacks:
            try:
                fn(symbol, price, volume)
            except Exception:
                pass

    def get_snapshot(self, symbol):
        with self._lock:
            return {
                'ticks':    list(self.price_ticks.get(symbol, [])),
                'velocity': self.velocity.get(symbol, {}),
                'insider':  self.insider_alerts.get(symbol, [])[-3:],
                'options':  self.options_flow.get(symbol, [])[-5:],
                'twits':    self.twits.get(symbol, {}),
                'news':     self.news.get(symbol, [])[-5:],
                'composite':self.composite.get(symbol, {}),
            }

    def set(self, key, symbol, value):
        with self._lock:
            getattr(self, key)[symbol] = value


# ── Price Velocity (yfinance 1-min bars) ─────────────────────────────────────

class VelocityTracker:
    """Computes price velocity and volume surge from 1-minute yfinance bars."""

    def __init__(self, cache, symbols):
        self.cache = cache
        self.symbols = symbols
        self._thread = None
        self.running = False

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            for symbol in list(self.symbols):
                try:
                    self._update(symbol)
                except Exception:
                    pass
            time.sleep(60)

    def _update(self, symbol):
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period='1d', interval='1m')
        if hist.empty or len(hist) < 5:
            return

        closes = hist['Close'].values
        volumes = hist['Volume'].values
        now_price = float(closes[-1])

        def chg(n):
            if len(closes) > n:
                p = float(closes[-n-1])
                return ((now_price - p) / p * 100) if p > 0 else 0.0
            return 0.0

        avg_vol = float(volumes[:-1].mean()) if len(volumes) > 1 else 1
        vol_ratio = float(volumes[-1]) / avg_vol if avg_vol > 0 else 1.0

        # Order flow imbalance: volume-weighted up vs down pressure last 10 bars
        up_vol = sum(volumes[-i] for i in range(1, min(10, len(closes))) if closes[-i] > closes[-i-1])
        dn_vol = sum(volumes[-i] for i in range(1, min(10, len(closes))) if closes[-i] < closes[-i-1])
        total_vol = up_vol + dn_vol
        ofi = round((up_vol - dn_vol) / total_vol, 3) if total_vol > 0 else 0.0  # -1 to +1

        self.cache.set('velocity', symbol, {
            'price':    round(now_price, 4),
            'pct_1m':   round(chg(1), 3),
            'pct_5m':   round(chg(5), 3),
            'pct_15m':  round(chg(15), 3),
            'vol_ratio': round(vol_ratio, 2),
            'ofi':       round(ofi, 2),  # order flow imbalance
            'updated':   datetime.now().strftime('%H:%M:%S'),
        })

        self.cache.add_tick(symbol, now_price, int(volumes[-1]))


# ── Finnhub WebSocket (real-time trades) ─────────────────────────────────────

class FinnhubStream:
    """
    Finnhub real-time trade WebSocket.
    Free key at https://finnhub.io — add to Settings as 'finnhub_key'.
    Provides sub-second trade data: price, volume, timestamp.
    """

    def __init__(self, cache, config, symbols):
        self.cache = cache
        self.config = config
        self.symbols = symbols
        self._ws = None
        self.running = False
        self._reconnect_count = 0

    def start(self):
        key = self.config.get('finnhub_key', '')
        if not key or not HAS_WEBSOCKET:
            return
        self.running = True
        self._reconnect_count = 0
        t = threading.Thread(target=self._connect, args=(key,), daemon=True)
        t.start()

    def stop(self):
        self.running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def _connect(self, key):
        url = 'wss://ws.finnhub.io?token=' + key

        def on_open(ws):
            for symbol in self.symbols:
                ws.send(json.dumps({'type': 'subscribe', 'symbol': symbol}))

        def on_message(ws, message):
            try:
                data = json.loads(message)
                if data.get('type') == 'trade':
                    for trade in data.get('data', []):
                        symbol = trade.get('s', '').replace('/', '')
                        price = float(trade.get('p', 0))
                        volume = int(trade.get('v', 0))
                        if symbol and price > 0:
                            self.cache.add_tick(symbol, price, volume)
            except Exception:
                pass

        def on_error(ws, error):
            pass

        def on_close(ws, *args):
            if self.running and self._reconnect_count < 3:
                self._reconnect_count += 1
                def _reconnect():
                    time.sleep(min(30, 5 * self._reconnect_count))
                    if self.running:
                        self._connect(key)
                threading.Thread(target=_reconnect, daemon=True).start()

        try:
            self._ws = websocket.WebSocketApp(
                url, on_open=on_open, on_message=on_message,
                on_error=on_error, on_close=on_close
            )
            self._ws.run_forever()
        except Exception:
            pass


# ── SEC EDGAR Form 4 RSS (insider filing alerts) ──────────────────────────────

class SECInsiderFeed:
    """
    Polls SEC EDGAR RSS every 10 minutes for Form 4 insider transaction filings.
    No API key required — public government data.
    Detects: officer/director purchases of company stock.
    """

    def __init__(self, cache, symbols):
        self.cache = cache
        self.symbols = {s.upper() for s in symbols}
        self.running = False
        self._seen = set()

    def start(self):
        self.running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.daemon = True
        t.start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                self._fetch()
            except Exception:
                pass
            time.sleep(600)

    def _fetch(self):
        # SEC EDGAR latest Form 4 filings RSS
        url = 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&dateb=&owner=include&count=40&search_text=&output=atom'
        r = requests.get(url, headers={'User-Agent': 'StopTrading buckkeely@gmail.com'}, timeout=10)
        if r.status_code != 200:
            return

        root = ET.fromstring(r.content)
        ns = {'atom': 'http://www.w3.org/2005/Atom'}

        for entry in root.findall('atom:entry', ns):
            entry_id = entry.findtext('atom:id', '', ns)
            if entry_id in self._seen:
                continue
            self._seen.add(entry_id)
            if len(self._seen) > 500:
                self._seen = set(list(self._seen)[-200:])

            title = entry.findtext('atom:title', '', ns)
            updated = entry.findtext('atom:updated', '', ns)
            link = entry.findtext('atom:link', '', ns)

            # Try to match a symbol from our watchlist in the filing
            for symbol in self.symbols:
                if symbol in title.upper():
                    with self.cache._lock:
                        self.cache.insider_alerts[symbol].append({
                            'title': title,
                            'time': updated[:16],
                            'link': link,
                            'type': 'Form 4',
                        })
                        # Keep last 10 alerts per symbol
                        self.cache.insider_alerts[symbol] = self.cache.insider_alerts[symbol][-10:]


# ── Unusual Whales Options Flow ───────────────────────────────────────────────

class OptionsFlowFeed:
    """
    Polls Unusual Whales public flow endpoint every 5 minutes.
    Detects: large unusual options activity (smart money bets).
    Big call buying BEFORE a stock moves = most powerful predictive signal.
    """

    def __init__(self, cache, symbols):
        self.cache = cache
        self.symbols = {s.upper() for s in symbols}
        self.running = False

    def start(self):
        self.running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.daemon = True
        t.start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                self._fetch()
            except Exception:
                pass
            time.sleep(300)

    def _fetch(self):
        # Unusual Whales public flow (no key, limited)
        url = 'https://phx.unusualwhales.com/api/option_activity?limit=100&order=created_at&orderBy=desc'
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json',
            'Referer': 'https://unusualwhales.com',
        }
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return

        try:
            data = r.json()
            items = data.get('data', data) if isinstance(data, dict) else data
        except Exception:
            return

        for item in (items or []):
            try:
                symbol = str(item.get('ticker', item.get('symbol', ''))).upper().strip()
                if symbol not in self.symbols:
                    continue

                contract_type = str(item.get('option_type', item.get('put_call', ''))).upper()
                premium = float(item.get('premium', item.get('total_premium', 0)) or 0)
                sentiment = 'BULLISH' if contract_type == 'CALL' else 'BEARISH'

                event = {
                    'time': item.get('created_at', datetime.now().isoformat())[:16],
                    'type': contract_type,
                    'premium': round(premium / 1000, 1),  # in thousands
                    'sentiment': sentiment,
                    'strike': item.get('strike', ''),
                    'expiry': item.get('expiry', item.get('expiration_date', '')),
                }

                with self.cache._lock:
                    self.cache.options_flow[symbol].append(event)
                    self.cache.options_flow[symbol] = self.cache.options_flow[symbol][-20:]
            except Exception:
                continue


# ── StockTwits Sentiment Poller ───────────────────────────────────────────────

class SentimentPoller:
    """
    Polls StockTwits public API every 3 minutes per symbol.
    Tracks bull/bear ratio shift — divergence from price is a leading indicator.
    """

    def __init__(self, cache, symbols):
        self.cache = cache
        self.symbols = list(symbols)
        self.running = False

    def start(self):
        self.running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.daemon = True
        t.start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            for symbol in self.symbols:
                if not self.running:
                    break
                try:
                    self._update(symbol)
                except Exception:
                    pass
                time.sleep(2)
            time.sleep(180)

    def _update(self, symbol):
        url = 'https://api.stocktwits.com/api/2/streams/symbol/{}.json'.format(symbol)
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            return

        data = r.json()
        messages = data.get('messages', [])
        bulls = sum(1 for m in messages
                    if m.get('entities', {}).get('sentiment', {}) and
                    m['entities']['sentiment'].get('basic') == 'Bullish')
        bears = sum(1 for m in messages
                    if m.get('entities', {}).get('sentiment', {}) and
                    m['entities']['sentiment'].get('basic') == 'Bearish')
        total = bulls + bears
        ratio = bulls / total if total > 0 else 0.5
        score = round((ratio - 0.5) * 2, 2)  # -1 to +1

        # Track velocity: is sentiment improving?
        prev = self.cache.twits.get(symbol, {})
        prev_score = prev.get('score', score)
        velocity = round(score - prev_score, 3)

        self.cache.set('twits', symbol, {
            'bulls': bulls,
            'bears': bears,
            'messages': len(messages),
            'score': score,
            'velocity': velocity,   # rising sentiment = early signal
            'ratio': round(ratio, 2),
            'updated': datetime.now().strftime('%H:%M:%S'),
        })


# ── Polygon News Feed ─────────────────────────────────────────────────────────

class NewsPoller:
    """
    Polls Polygon.io for latest news (free key, 15-min delayed is fine for news).
    Scores headlines for positive/negative sentiment using keyword matching.
    """

    BULLISH_WORDS = ['surge', 'rally', 'jump', 'gain', 'beat', 'upgrade', 'buy',
                     'record', 'positive', 'growth', 'profit', 'revenue', 'milestone']
    BEARISH_WORDS = ['drop', 'fall', 'miss', 'loss', 'downgrade', 'sell', 'lawsuit',
                     'decline', 'warning', 'risk', 'fraud', 'dilut', 'offering']

    def __init__(self, cache, symbols, config):
        self.cache = cache
        self.symbols = list(symbols)
        self.config = config
        self.running = False

    def start(self):
        self.running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.daemon = True
        t.start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            for symbol in self.symbols:
                if not self.running:
                    break
                try:
                    self._update(symbol)
                except Exception:
                    pass
                time.sleep(3)
            time.sleep(300)

    def _update(self, symbol):
        key = self.config.get('polygon_key', '')
        headlines = []

        benzinga_key = self.config.get('benzinga_key', '')
        if benzinga_key:
            try:
                url = ('https://api.benzinga.com/api/v2/news?token={}'
                       '&tickers={}&pageSize=5&displayOutput=headline').format(
                    benzinga_key, symbol)
                r = requests.get(url, timeout=8)
                if r.status_code == 200:
                    arts = r.json() if isinstance(r.json(), list) else r.json().get('news', [])
                    for article in arts[:5]:
                        title = article.get('title', '')
                        score = self._score(title)
                        headlines.append({
                            'title':  title[:80],
                            'score':  score,
                            'time':   article.get('created', '')[:16],
                            'source': 'Benzinga',
                        })
            except Exception:
                pass

        if not headlines and key:
            url = 'https://api.polygon.io/v2/reference/news?ticker={}&limit=10&apiKey={}'.format(symbol, key)
            r = requests.get(url, timeout=8)
            if r.status_code == 200:
                for article in r.json().get('results', []):
                    title = article.get('title', '')
                    score = self._score(title)
                    headlines.append({
                        'title': title[:80],
                        'score': score,
                        'time': article.get('published_utc', '')[:16],
                        'source': 'Polygon',
                    })

        # Fallback: yfinance news
        if not headlines:
            try:
                news_items = yf.Ticker(symbol).news or []
                for item in news_items[:5]:
                    title = item.get('title', '')
                    headlines.append({
                        'title': title[:80],
                        'score': self._score(title),
                        'time': datetime.fromtimestamp(
                            item.get('providerPublishTime', time.time())
                        ).strftime('%Y-%m-%d %H:%M'),
                        'source': item.get('publisher', 'Yahoo'),
                    })
            except Exception:
                pass

        with self.cache._lock:
            self.cache.news[symbol] = headlines

    def _score(self, text):
        text_lower = text.lower()
        score = sum(1 for w in self.BULLISH_WORDS if w in text_lower)
        score -= sum(1 for w in self.BEARISH_WORDS if w in text_lower)
        return max(-3, min(3, score))


# ── Master Real-Time Engine ───────────────────────────────────────────────────

class RealtimeEngine:
    """
    Orchestrates all data feeds. Call start(symbols) to begin streaming.
    Read data via engine.cache.get_snapshot(symbol) or engine.score(symbol).
    """

    def __init__(self, config):
        self.config = config
        self.cache = RealtimeCache()
        self._feeds = []
        self.symbols = []
        self.running = False
        self._score_thread = None

    def start(self, symbols):
        self.symbols = [s.upper() for s in symbols]
        self.running = True

        # Start all feeds
        feeds = [
            VelocityTracker(self.cache, self.symbols),
            SECInsiderFeed(self.cache, self.symbols),
            OptionsFlowFeed(self.cache, self.symbols),
            SentimentPoller(self.cache, self.symbols),
            NewsPoller(self.cache, self.symbols, self.config),
        ]

        # Add Finnhub WebSocket if key configured
        finnhub_key = self.config.get('finnhub_key', '')
        if finnhub_key and HAS_WEBSOCKET:
            feeds.append(FinnhubStream(self.cache, self.config, self.symbols))

        for feed in feeds:
            feed.start()
            self._feeds.append(feed)

        # Start composite scorer
        self._score_thread = threading.Thread(target=self._score_loop, daemon=True)
        self._score_thread.start()

    def stop(self):
        self.running = False
        for feed in self._feeds:
            try:
                feed.stop()
            except Exception:
                pass
        self._feeds = []

    def _score_loop(self):
        while self.running:
            for symbol in self.symbols:
                try:
                    scored = self.score(symbol)
                    with self.cache._lock:
                        self.cache.composite[symbol] = scored
                except Exception:
                    pass
            time.sleep(30)

    def score(self, symbol):
        """
        Composite secret-sauce score from all live feeds.
        Returns dict with signal, score 0-100, and per-source breakdown.
        """
        snap = self.cache.get_snapshot(symbol)
        vel  = snap.get('velocity', {})
        twit = snap.get('twits', {})
        news = snap.get('news', [])
        opts = snap.get('options', [])
        ins  = snap.get('insider', [])

        scores = {}

        # 1. Price velocity & order flow (30 pts max)
        pct_1m  = vel.get('pct_1m', 0)
        pct_5m  = vel.get('pct_5m', 0)
        pct_15m = vel.get('pct_15m', 0)
        vol_r   = vel.get('vol_ratio', 1)
        ofi     = vel.get('ofi', 0)   # -1 to +1 order flow imbalance

        vel_score = 0
        if pct_1m > 0 and pct_5m > 0 and pct_15m > 0:
            vel_score += 15   # all timeframes green = strong momentum
        elif pct_1m > 0 and pct_5m > 0:
            vel_score += 8
        elif pct_1m < 0 and pct_5m < 0:
            vel_score -= 10

        vol_score = min((vol_r - 1) / 4 * 10, 10) if vol_r > 1 else 0
        ofi_score = ofi * 5   # -5 to +5

        scores['velocity'] = {
            'score': round(vel_score + vol_score + ofi_score, 1),
            'detail': '{}%/1m {}%/5m vol:{:.1f}x ofi:{}'.format(
                pct_1m, pct_5m, vol_r, ofi),
            'max': 30,
        }

        # 2. Options flow (25 pts max) — call buying is smartest signal
        call_premium = sum(e.get('premium', 0) for e in opts if e.get('type') == 'CALL')
        put_premium  = sum(e.get('premium', 0) for e in opts if e.get('type') == 'PUT')
        opt_score = min(call_premium / 100 * 15, 15) - min(put_premium / 100 * 10, 10)
        unusual = len([e for e in opts if e.get('premium', 0) > 50])  # >$50K premium

        scores['options'] = {
            'score': round(opt_score + unusual * 5, 1),
            'detail': '{}K calls, {}K puts, {} unusual'.format(
                int(call_premium), int(put_premium), unusual),
            'max': 25,
        }

        # 3. Insider buying (20 pts max) — real money on the line
        buy_alerts = len([i for i in ins if 'Purchase' in i.get('title', '') or 'buy' in i.get('title', '').lower()])
        ins_score = min(buy_alerts * 10, 20)
        scores['insider'] = {
            'score': ins_score,
            'detail': '{} recent Form 4 buy filings'.format(buy_alerts),
            'max': 20,
        }

        # 4. StockTwits sentiment + velocity (15 pts max)
        twit_score = twit.get('score', 0) * 10       # -10 to +10
        twit_vel   = twit.get('velocity', 0) * 25    # sentiment rising?
        twit_total = round(twit_score + twit_vel, 1)
        scores['stocktwits'] = {
            'score': max(-10, min(15, twit_total)),
            'detail': '{} bulls {} bears velocity:{}'.format(
                twit.get('bulls', 0), twit.get('bears', 0),
                '+' if twit.get('velocity', 0) > 0 else ''),
            'max': 15,
        }

        # 5. News sentiment (10 pts max)
        news_score = sum(n.get('score', 0) for n in news)
        news_score = max(-8, min(10, news_score))
        scores['news'] = {
            'score': news_score,
            'detail': '{} articles | avg sentiment: {:+.1f}'.format(
                len(news), news_score / max(len(news), 1)),
            'max': 10,
        }

        # Total composite
        total = sum(s.get('score', 0) for s in scores.values())
        total = round(total, 1)

        # Signal label
        if total >= 50:
            signal, color = 'STRONG BUY', '#00d4aa'
        elif total >= 25:
            signal, color = 'BUY', '#22c55e'
        elif total >= 5:
            signal, color = 'WATCH', '#60a5fa'
        elif total >= -10:
            signal, color = 'NEUTRAL', '#94a3b8'
        else:
            signal, color = 'AVOID', '#ff4757'

        # Buy/sell execution recommendation
        price = vel.get('price', 0)
        recommendation = None
        if signal in ('STRONG BUY', 'BUY') and pct_1m > 0 and vol_r > 1.5 and ofi > 0:
            recommendation = {
                'action': 'BUY NOW',
                'reason': 'Momentum + volume + order flow confluence',
                'price': price,
                'urgency': 'HIGH' if signal == 'STRONG BUY' else 'MEDIUM',
            }
        elif pct_1m < -1 and ofi < -0.3:
            recommendation = {
                'action': 'SELL / AVOID',
                'reason': 'Selling pressure detected in order flow',
                'price': price,
                'urgency': 'HIGH',
            }

        return {
            'symbol': symbol,
            'price': price,
            'signal': signal,
            'signal_color': color,
            'composite_score': total,
            'recommendation': recommendation,
            'scores': scores,
            'updated': datetime.now().strftime('%H:%M:%S'),
        }

    def get_all_scores(self):
        with self.cache._lock:
            return dict(self.cache.composite)

    def add_symbol(self, symbol):
        symbol = symbol.upper()
        if symbol not in self.symbols:
            self.symbols.append(symbol)

    def remove_symbol(self, symbol):
        symbol = symbol.upper()
        if symbol in self.symbols:
            self.symbols.remove(symbol)
