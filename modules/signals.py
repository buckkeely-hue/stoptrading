"""
Multi-source signal aggregator — the "secret sauce" stock picker.

Sources (all free/public, no ToS violations):
  1. SEC EDGAR Form 4  — insider buy/sell filings (real money on the line)
  2. FINRA Short Interest — squeeze potential
  3. StockTwits API    — active trader sentiment (no key required for public feed)
  4. yfinance          — institutional holders, analyst upgrades, options activity
  5. Reddit PRAW       — mention velocity r/pennystocks, r/wallstreetbets
  6. Finviz public     — news headline scrape (public, no login)
  7. OpenInsider       — insider transaction aggregator (public)
"""

import requests
import yfinance as yf
import threading
import time
import json
import os
from datetime import datetime, timedelta
from html.parser import HTMLParser

# Module-level VADER/TextBlob setup — loaded once
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as _VaderCls
    _VADER = _VaderCls()
    HAS_VADER = True
except ImportError:
    HAS_VADER = False

HEADERS = {
    'User-Agent': 'StopTrading Research Tool buckkeely@gmail.com',  # SEC requires this
    'Accept': 'application/json',
}

# ── SEC EDGAR Form 4 (Insider Transactions) ───────────────────────────────────

def get_insider_signal(symbol):
    """
    Pull recent Form 4 filings from SEC EDGAR company search + OpenInsider.
    Returns score 0-30: high if recent insider BUYS, negative if sells.
    """
    try:
        buys = 0
        sells = 0

        # OpenInsider — parses buy vs sell direction (primary source)
        oi_ok = False
        try:
            oi_url = (
                'https://openinsider.com/screener?s={}&o=&pl=&ph=&ll=&lh=&fd=30'
                '&fdr=&td=0&tdr=&fdlyl=&fdlyh=&daysago=&xp=1&xs=1&vl=&vh='
                '&ocl=&och=&sic1=-1&sicl=100&sich=9999&grp=0&nfl=&nfh='
                '&nil=&nih=&nol=&noh=&v2l=&v2h=&oc2l=&oc2h=&sortcol=0&cnt=10&page=1'
            ).format(symbol)
            oi_r = requests.get(oi_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=6)
            if oi_r.status_code == 200:
                oi_buys  = oi_r.text.count('Purchase')
                oi_sells = oi_r.text.count('Sale')
                if oi_buys or oi_sells:
                    buys  = oi_buys
                    sells = oi_sells
                    oi_ok = True
        except Exception:
            pass

        # EDGAR fallback — parse transaction code from XML to get actual direction
        if not oi_ok:
            try:
                import xml.etree.ElementTree as ET
                import re as _re
                edgar_url = (
                    'https://www.sec.gov/cgi-bin/browse-edgar'
                    '?action=getcompany&company=&CIK={}&type=4'
                    '&dateb=&owner=include&count=10&search_text=&output=atom'
                ).format(symbol)
                r = requests.get(edgar_url, headers=HEADERS, timeout=8)
                if r.status_code == 200 and r.content:
                    root = ET.fromstring(r.content)
                    ns = {'atom': 'http://www.w3.org/2005/Atom'}
                    entries = root.findall('atom:entry', ns)
                    for entry in entries[:5]:
                        # Look for transaction description in entry content
                        content_el = entry.find('atom:content', ns)
                        content = ET.tostring(content_el, encoding='unicode') if content_el is not None else ''
                        title_el = entry.find('atom:title', ns)
                        title = title_el.text.upper() if title_el is not None else ''
                        if any(w in title for w in ['PURCHASE', 'ACQUISITION', 'BOUGHT']):
                            buys += 1
                        elif any(w in title for w in ['SALE', 'SOLD', 'DISPOSITION']):
                            sells += 1
                        # P = purchase, S = sale, D = disposition by gift in transaction code
                        if _re.search(r'transactionCode[^>]*>P<', content):
                            buys += 1
                        elif _re.search(r'transactionCode[^>]*>[SD]<', content):
                            sells += 1
            except Exception:
                pass

        net = buys - sells
        score = min(max(net * 5, -15), 30)
        note = '{} insider buys, {} sells (30d)'.format(buys, sells)
        return {'score': score, 'buys': buys, 'sells': sells, 'note': note}
    except Exception as e:
        return {'score': 0, 'buys': 0, 'sells': 0, 'note': str(e)}


# ── FINRA Short Interest ───────────────────────────────────────────────────────

def get_short_squeeze_signal(symbol, price, ibkr_feed=None):
    """
    Short squeeze potential: short interest + Cost-to-Borrow rate.
    CTB (borrow rate) from IBKR is the strongest real-time squeeze signal.
    Returns score 0-40.
    """
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info

        short_pct   = info.get('shortPercentOfFloat', 0) or 0
        short_ratio = info.get('shortRatio', 0) or 0

        squeeze_score = 0
        if short_pct > 0.30:
            squeeze_score += 15
        elif short_pct > 0.20:
            squeeze_score += 10
        elif short_pct > 0.10:
            squeeze_score += 5

        if short_ratio > 5:
            squeeze_score += 10
        elif short_ratio > 2:
            squeeze_score += 5

        note = 'Short float: {:.1f}% | Days to cover: {:.1f}'.format(short_pct * 100, short_ratio)

        # Cost-to-Borrow from IBKR (free with account) — strongest squeeze signal
        ctb_score = 0
        ctb_note  = ''
        if ibkr_feed and ibkr_feed.connected:
            ctb = ibkr_feed.get_borrow_rate(symbol)
            ctb_score = ibkr_feed.borrow_score(symbol)
            if ctb > 0:
                ctb_note = ' | CTB: {:.1f}%/yr'.format(ctb)
                if ctb >= 100:
                    ctb_note += ' ⚡EXTREME — shorts trapped'
                elif ctb >= 50:
                    ctb_note += ' 🔥HIGH — squeeze fuel'
                elif ctb >= 20:
                    ctb_note += ' ↑elevated'
        else:
            # Fintel public data fallback (scrape) for rough CTB estimate
            try:
                r = requests.get(
                    'https://fintel.io/ss/us/{}'.format(symbol.lower()),
                    headers={'User-Agent': 'Mozilla/5.0'}, timeout=6)
                if r.status_code == 200:
                    import re as _re
                    m = _re.search(r'Borrow\s+Fee[^%]*?([\d.]+)\s*%', r.text, _re.IGNORECASE)
                    if m:
                        ctb = float(m.group(1))
                        if ctb >= 100:   ctb_score = 25
                        elif ctb >= 50:  ctb_score = 15
                        elif ctb >= 20:  ctb_score = 8
                        ctb_note = ' | CTB~{:.0f}% (Fintel)'.format(ctb)
            except Exception:
                pass

        note += ctb_note
        return {'score': min(squeeze_score + ctb_score, 40),
                'short_pct': round(short_pct * 100, 1),
                'short_ratio': round(short_ratio, 1),
                'ctb_score': ctb_score,
                'note': note}
    except Exception:
        return {'score': 0, 'short_pct': 0, 'short_ratio': 0, 'ctb_score': 0, 'note': 'No short data'}


# ── StockTwits Sentiment ───────────────────────────────────────────────────────

def get_stocktwits_signal(symbol):
    """
    StockTwits public API — no key required for basic stream.
    Returns score -15 to +20 based on bull/bear ratio from active traders.
    """
    try:
        url = 'https://api.stocktwits.com/api/2/streams/symbol/{}.json'.format(symbol)
        r = requests.get(url, timeout=6)
        if r.status_code != 200:
            return {'score': 0, 'bulls': 0, 'bears': 0, 'messages': 0, 'note': 'No StockTwits data'}

        data = r.json()
        messages = data.get('messages', [])
        bulls = sum(1 for m in messages if m.get('entities', {}).get('sentiment', {}) and
                    m['entities']['sentiment'].get('basic') == 'Bullish')
        bears = sum(1 for m in messages if m.get('entities', {}).get('sentiment', {}) and
                    m['entities']['sentiment'].get('basic') == 'Bearish')
        total = bulls + bears

        ratio = bulls / total if total > 0 else 0.5
        # Score: 0 if 50/50, +20 if all bullish, -15 if all bearish
        if ratio > 0.5:
            score = int((ratio - 0.5) * 40)   # 0 to +20
        else:
            score = int((ratio - 0.5) * 30)   # -15 to 0

        note = '{} bulls / {} bears on StockTwits'.format(bulls, bears)
        return {'score': max(-15, min(20, score)), 'bulls': bulls, 'bears': bears,
                'messages': len(messages), 'note': note}
    except Exception as e:
        return {'score': 0, 'bulls': 0, 'bears': 0, 'messages': 0, 'note': str(e)}


# ── Institutional Holdings ─────────────────────────────────────────────────────

def get_institutional_signal(symbol):
    """
    Increasing institutional ownership = smart money accumulating.
    Returns score 0-20.
    """
    try:
        ticker = yf.Ticker(symbol)
        inst = ticker.institutional_holders
        if inst is None or inst.empty:
            return {'score': 0, 'holders': 0, 'pct_held': 0, 'note': 'No institutional data'}

        holders = len(inst)
        pct_held = float(inst['% Out'].sum()) if '% Out' in inst.columns else 0

        score = 0
        if holders > 10:
            score += 10
        elif holders > 5:
            score += 5

        if pct_held > 0.10:
            score += 10
        elif pct_held > 0.05:
            score += 5

        note = '{} institutions hold {:.1f}%'.format(holders, pct_held * 100)
        return {'score': min(score, 20), 'holders': holders,
                'pct_held': round(pct_held * 100, 1), 'note': note}
    except Exception:
        return {'score': 0, 'holders': 0, 'pct_held': 0, 'note': 'No institutional data'}


# ── Analyst Upgrades ───────────────────────────────────────────────────────────

def get_analyst_signal(symbol):
    """
    Recent analyst upgrades/recommendations.
    Returns score -10 to +15.
    """
    try:
        ticker = yf.Ticker(symbol)
        rec = ticker.recommendations
        if rec is None or rec.empty:
            return {'score': 0, 'rating': 'N/A', 'note': 'No analyst data'}

        recent = rec.tail(5)
        buy_words = ['buy', 'strong buy', 'outperform', 'overweight', 'positive']
        sell_words = ['sell', 'underperform', 'underweight', 'negative', 'reduce']

        buys = 0
        sells = 0
        for _, row in recent.iterrows():
            action = str(row.get('To Grade', row.get('Action', ''))).lower()
            if any(w in action for w in buy_words):
                buys += 1
            elif any(w in action for w in sell_words):
                sells += 1

        score = buys * 5 - sells * 3
        note = '{} buy / {} sell ratings (recent)'.format(buys, sells)
        return {'score': max(-10, min(15, score)), 'buys': buys, 'sells': sells, 'note': note}
    except Exception:
        return {'score': 0, 'buys': 0, 'sells': 0, 'note': 'No analyst data'}


# ── Reddit Velocity ────────────────────────────────────────────────────────────

def get_reddit_signal(symbol, reddit_client=None):
    """
    Mention velocity on penny stock subreddits.
    Falls back to public JSON API when PRAW not configured.
    Returns score 0-15.
    """
    try:
        count = 0

        if reddit_client is not None:
            for sub in ['pennystocks', 'RobinHoodPennyStocks', 'wallstreetbets', 'stocks']:
                try:
                    results = reddit_client.subreddit(sub).search(symbol, limit=10, time_filter='day')
                    count += sum(1 for _ in results)
                except Exception:
                    pass
        else:
            # No PRAW — scrape public JSON for penny-specific subreddits
            import re
            headers = {'User-Agent': 'StopTrading/1.0'}
            for sub in ['pennystocks', 'RobinHoodPennyStocks']:
                try:
                    r = requests.get(
                        'https://www.reddit.com/r/{}/hot.json?limit=50'.format(sub),
                        headers=headers, timeout=6)
                    if r.status_code == 200:
                        for post in r.json().get('data', {}).get('children', []):
                            title = post['data'].get('title', '').upper()
                            ups   = post['data'].get('ups', 0)
                            if symbol in re.findall(r'\b[A-Z]{1,5}\b', title):
                                count += 1 + ups // 100  # upvote-weighted
                except Exception:
                    pass

        score = min(count * 2, 15)
        note = '{} Reddit penny mentions today'.format(count)
        return {'score': score, 'mentions': count, 'note': note}
    except Exception:
        return {'score': 0, 'mentions': 0, 'note': 'Reddit error'}


# ── Price Momentum (Technical) ─────────────────────────────────────────────────

def get_technical_signal(symbol):
    """
    Multi-timeframe momentum: 1h, 5h, 1d, 5d trend confluence.
    Returns score -15 to +25.
    """
    try:
        ticker = yf.Ticker(symbol)
        hist_1h = ticker.history(period='5d', interval='1h')
        hist_1d = ticker.history(period='30d', interval='1d')

        if hist_1h.empty or hist_1d.empty:
            return {'score': 0, 'note': 'No technical data'}

        price = float(hist_1h['Close'].iloc[-1])

        # 1h change
        change_1h = (price - float(hist_1h['Close'].iloc[-2])) / float(hist_1h['Close'].iloc[-2]) * 100 if len(hist_1h) >= 2 else 0

        # 5h change
        change_5h = (price - float(hist_1h['Close'].iloc[-6])) / float(hist_1h['Close'].iloc[-6]) * 100 if len(hist_1h) >= 6 else 0

        # 1d change
        change_1d = (price - float(hist_1d['Close'].iloc[-2])) / float(hist_1d['Close'].iloc[-2]) * 100 if len(hist_1d) >= 2 else 0

        # MA trend: price above 20d MA = bullish
        ma20 = float(hist_1d['Close'].tail(20).mean()) if len(hist_1d) >= 5 else price
        above_ma = price > ma20

        # Volume spike
        vol = int(hist_1h['Volume'].iloc[-1])
        avg_vol = int(hist_1h['Volume'].mean())
        vol_ratio = vol / avg_vol if avg_vol > 0 else 1

        score = 0
        # Confluence: all timeframes pointing same direction
        if change_1h > 0 and change_5h > 0 and change_1d > 0:
            score += 15   # all green = strong buy signal
        elif change_1h > 0 and change_5h > 0:
            score += 8
        elif change_1h < 0 and change_5h < 0 and change_1d < 0:
            score -= 15

        if above_ma:
            score += 5
        if vol_ratio > 2:
            score += 5

        note = '1h:{:.1f}% 5h:{:.1f}% 1d:{:.1f}% vol:{:.1f}x'.format(change_1h, change_5h, change_1d, vol_ratio)
        return {'score': max(-15, min(25, score)), 'change_1h': round(change_1h, 2),
                'change_5h': round(change_5h, 2), 'change_1d': round(change_1d, 2),
                'vol_ratio': round(vol_ratio, 2), 'note': note}
    except Exception as e:
        return {'score': 0, 'note': str(e)}


# ── Master Signal Aggregator ───────────────────────────────────────────────────

class SignalAggregator:
    def __init__(self, config=None, reddit_client=None):
        self.config = config or {}
        self.reddit = reddit_client
        self._cache = {}
        self._cache_lock = threading.Lock()

    def score_symbol(self, symbol, price):
        """
        Aggregate all signals into one composite score (0-100+).
        Weights reflect reliability and edge value of each signal.
        Results cached for 300 seconds — signal data doesn't change intraday.
        """
        cache_key = symbol.upper()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and (time.time() - cached.get('ts', 0)) < 300:
                return cached['result']

        signals = {}

        # Run signal fetches in parallel
        results = {}
        threads = [
            threading.Thread(target=lambda: results.__setitem__('insider', get_insider_signal(symbol))),
            threading.Thread(target=lambda: results.__setitem__('squeeze', get_short_squeeze_signal(symbol, price))),
            threading.Thread(target=lambda: results.__setitem__('stocktwits', get_stocktwits_signal(symbol))),
            threading.Thread(target=lambda: results.__setitem__('institutional', get_institutional_signal(symbol))),
            threading.Thread(target=lambda: results.__setitem__('analyst', get_analyst_signal(symbol))),
            threading.Thread(target=lambda: results.__setitem__('reddit', get_reddit_signal(symbol, self.reddit))),
            threading.Thread(target=lambda: results.__setitem__('technical', get_technical_signal(symbol))),
        ]
        for t in threads:
            t.daemon = True
            t.start()
        for t in threads:
            t.join(timeout=12)

        insider = results.get('insider', {'score': 0})
        squeeze = results.get('squeeze', {'score': 0})
        stocktwits = results.get('stocktwits', {'score': 0})
        institutional = results.get('institutional', {'score': 0})
        analyst = results.get('analyst', {'score': 0})
        reddit = results.get('reddit', {'score': 0})
        technical = results.get('technical', {'score': 0})

        total = (
            insider['score']       +   # 0-30  insider buying
            squeeze['score']       +   # 0-25  short squeeze setup
            stocktwits['score']    +   # -15 to +20  active trader sentiment
            institutional['score'] +   # 0-20  smart money accumulation
            analyst['score']       +   # -10 to +15  analyst upgrades
            reddit['score']        +   # 0-15  retail momentum
            technical['score']         # -15 to +25  price confluence
        )

        # Count how many independent signal sources are positive (consensus)
        all_signals = [insider, squeeze, stocktwits, institutional, analyst, reddit, technical]
        positive_count = sum(1 for s in all_signals if s.get('score', 0) > 0)
        has_data_count = sum(1 for s in all_signals if 'No ' not in s.get('note', 'No '))

        # Determine signal strength label — require 3+ positive sources for BUY
        if total >= 60 and positive_count >= 4:
            label = 'STRONG BUY'
            color = '#00d4aa'
        elif total >= 35 and positive_count >= 3:
            label = 'BUY'
            color = '#22c55e'
        elif total >= 15 or positive_count >= 2:
            label = 'WATCH'
            color = '#60a5fa'
        elif total >= 0:
            label = 'NEUTRAL'
            color = '#94a3b8'
        else:
            label = 'AVOID'
            color = '#ff4757'

        result = {
            'symbol': symbol,
            'price': price,
            'composite_score': round(total, 1),
            'signal': label,
            'signal_color': color,
            'consensus': positive_count,      # how many of 7 sources are positive
            'positive_count': positive_count, # alias used by buy gate
            'data_sources': has_data_count,   # how many sources returned real data
            'breakdown': {
                'insider':       {'score': insider['score'],       'note': insider.get('note', '')},
                'squeeze':       {'score': squeeze['score'],       'note': squeeze.get('note', ''), 'short_pct': squeeze.get('short_pct', 0)},
                'stocktwits':    {'score': stocktwits['score'],    'note': stocktwits.get('note', ''), 'bulls': stocktwits.get('bulls', 0), 'bears': stocktwits.get('bears', 0)},
                'institutional': {'score': institutional['score'], 'note': institutional.get('note', '')},
                'analyst':       {'score': analyst['score'],       'note': analyst.get('note', '')},
                'reddit':        {'score': reddit['score'],        'note': reddit.get('note', ''), 'mentions': reddit.get('mentions', 0)},
                'technical':     {'score': technical['score'],     'note': technical.get('note', '')},
            }
        }
        with self._cache_lock:
            self._cache[cache_key] = {'ts': time.time(), 'result': result}
        return result

    def scan_with_signals(self, symbols_with_prices):
        """
        Score a list of (symbol, price) tuples using all signals.
        Returns sorted list by composite score.
        """
        results = []
        threads = []
        lock = threading.Lock()

        def score_one(symbol, price):
            result = self.score_symbol(symbol, price)
            with lock:
                results.append(result)

        for symbol, price in symbols_with_prices:
            t = threading.Thread(target=score_one, args=(symbol, price))
            t.daemon = True
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=20)

        results.sort(key=lambda x: x['composite_score'], reverse=True)
        return results
