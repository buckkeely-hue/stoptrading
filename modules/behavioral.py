"""
BehavioralAgent — non-financial behavioral signals for penny stock timing.

Sources:
  1. Intraday sentiment velocity — rate of change of Reddit mention volume.
                    Accelerating crowd = +4, decelerating = -4.

  2. Reddit r/pennystocks + r/wallstreetbets
                  — mention velocity (count × log upvotes) per ticker.
                    Accelerating mentions = crowd momentum building.
                    VADER NLP sentiment analysis (more accurate than TextBlob for financial text).

  3. StockTwits trending
                  — real-time social pulse; complements Reddit (different user base).

  4. Time-of-day behavioral patterns
                  — Opening 30 min: high volatility, aggressive moves.
                    Lunch (12-2 ET): dead zone, thin books.
                    Power Hour (3-4 ET): volume surge, momentum continuation.

  5. Intraday clock patterns
                  — First/last trading day of month, OpEx weeks, holiday-week thin tape.

Scoring:
  Sentiment velocity: -4 to +4 (crowd momentum change rate)
  Reddit:      0 to +20 per ticker (velocity + sentiment)
  StockTwits:  0 to +10 per ticker (volume of mentions)
  Time-of-day:-8 to +7
"""

import threading
import time
import math
import re
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo('America/New_York')
except Exception:
    _ET = None


HEADERS = {'User-Agent': 'StopTrading:BehavioralAgent:1.0 (buckkeely@gmail.com)'}

# Tickers that appear in financial text but are NOT stocks
_STOPWORDS = {
    # Common 2-letter words
    'TO','IN','AT','ON','BE','DO','GO','IT','IS','AS','AN','OR','IF','BY','UP',
    'SO','NO','MY','WE','ME','HE','HI','OK','US','VS','AM','PM',
    # Finance jargon / abbreviations
    'DD','USA','CEO','CFO','IPO','SEC','FDA','USD','ETF','NYSE','OTC','ATH','ATL',
    'RSI','EPS','DRS','IMO','YOLO','LOL','FOR','THE','AND','ARE','NOT','BUY','SELL',
    'NEWS','THIS','THAT','WITH','FROM','BEEN','WILL','HAVE','WHEN','WHAT','YOUR',
    'MORE','THAN','THEY','JUST','LIKE','THEN','SOME','ALSO','INTO','OVER',
    'SUCH','ONLY','BOTH','BACK','VERY','WELL','MOST','EVEN','HERE','SAME',
    'GOOD','MUCH','LONG','HIGH','HOLD','NEXT','LAST','NEAR','TAKE','KNOW','DOES',
    'EDIT','FWIW','TLDR','HODL','FOMO','REKT','MOON','PUMP','DUMP','APES','WIFE',
    'SPAC','PIPE','CASH','DEBT','RISK','LOSS','GAIN','CALL','PUT','PUTS','PUTS',
    'OPEN','CLOSE','STOCK','TRADE','SHORT','SHARE','PRICE','CHART','DOWN','MOVE',
    'AFTER','ABOUT','WOULD','COULD','SHOULD','THERE','THESE','THOSE','THEIR',
    'SAID','SAYS','SAYS','GETS','WENT','COME','CAME','WENT','SAID','MADE',
    'HELP','NEED','WANT','GIVE','LOOK','FEEL','THINK','SEEM','WORK','MAKE',
    'TIME','YEAR','WEEK','DAYS','MONTH','TODAY','USED','ABLE','EACH',
    'HAS','HAD','WAS','DID','CAN','MAY','TRY','WAY','HOW','WHO','WHY',
    'LOT','ALL','ANY','FEW','NEW','OLD','BIG','LOW','TOP','SET','GET',
    'RUN','USE','LET','SAY','SEE','PER','NOW','OUT','OFF','OUT','TOO',
    'ABOVE','BELOW','SINCE','WHILE','STILL','MIGHT','WHICH','WHERE','OFTEN',
}

REDDIT_SUBS = ['pennystocks', 'wallstreetbets', 'stocks', 'investing', 'smallstreetbets']

# VADER for financial-text sentiment (more accurate than TextBlob for social media)
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as _VaderCls
    _VADER = _VaderCls()
    HAS_VADER = True
except ImportError:
    HAS_VADER = False


class BehavioralAgent:

    def __init__(self, config):
        self.config  = config
        self._lock   = threading.Lock()
        self.running = False

        # Intraday sentiment velocity (replaces lunar phase)
        self._sentiment_velocity = 0   # -4 to +4: crowd momentum change rate
        self._prev_total_velocity = 0.0

        # Social state
        self._reddit_mentions  = {}   # ticker -> {count, upvotes, velocity, sentiment}
        self._stocktwits_hot   = {}   # ticker -> mention_count
        self._last_reddit      = None
        self._last_stocktwits  = None

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
                self._fetch_reddit()
                self._fetch_stocktwits()
                self._calc_sentiment_velocity()
            except Exception:
                pass
            time.sleep(900)         # refresh every 15 minutes

    # ── Intraday Sentiment Velocity ───────────────────────────────────────────

    def _calc_sentiment_velocity(self):
        """
        Replace lunar phase with intraday social sentiment velocity.
        Measures rate-of-change of crowd momentum — accelerating crowd = +4, fading = -4.
        """
        with self._lock:
            total_vel = sum(d.get('velocity', 0) for d in self._reddit_mentions.values())
        prev = self._prev_total_velocity
        if prev > 0:
            pct_change = (total_vel - prev) / prev * 100
            if pct_change > 20:
                vel_score = 4
            elif pct_change > 5:
                vel_score = 2
            elif pct_change < -20:
                vel_score = -4
            elif pct_change < -5:
                vel_score = -2
            else:
                vel_score = 0
        else:
            vel_score = 0
        self._prev_total_velocity = total_vel
        with self._lock:
            self._sentiment_velocity = vel_score

    # ── Reddit Scraper ────────────────────────────────────────────────────────

    def _fetch_reddit(self):
        """
        Pull hot posts from penny stock subreddits via Reddit's public JSON API.
        No auth required for read-only public access.
        Velocity = mentions × log(upvotes+1) — rewards posts getting attention.
        """
        counts     = defaultdict(int)
        upvotes    = defaultdict(int)
        polarities = defaultdict(list)   # TextBlob NLP polarity per ticker
        # Only match $TICKER format — Reddit convention; avoids English word false positives
        ticker_re = re.compile(r'\$([A-Z]{1,5})\b')

        for sub in REDDIT_SUBS[:3]:
            try:
                url = 'https://www.reddit.com/r/{}/hot.json?limit=100'.format(sub)
                r   = requests.get(url, headers=HEADERS, timeout=8)
                if r.status_code != 200:
                    continue
                posts = r.json().get('data', {}).get('children', [])
                for post in posts:
                    d        = post.get('data', {})
                    raw_text = d.get('title', '') + ' ' + d.get('selftext', '')
                    text     = raw_text.upper()
                    score    = int(d.get('score', 0))
                    found    = set()
                    for m in ticker_re.finditer(text):
                        t = m.group(1).strip('$')
                        if t and t not in _STOPWORDS and 1 < len(t) <= 5:
                            found.add(t)
                    if found:
                        try:
                            if HAS_VADER:
                                polarity = _VADER.polarity_scores(raw_text)['compound']
                            else:
                                from textblob import TextBlob
                                polarity = TextBlob(raw_text).sentiment.polarity
                        except Exception:
                            polarity = 0.0
                        for t in found:
                            counts[t]    += 1
                            upvotes[t]   += max(score, 0)
                            polarities[t].append(polarity)
            except Exception:
                continue

        mentions = {}
        for ticker, count in counts.items():
            if count < 2:
                continue
            velocity     = round(count * math.log(upvotes[ticker] + 2), 1)
            pols         = polarities.get(ticker, [])
            avg_polarity = round(sum(pols) / len(pols), 3) if pols else 0.0
            if avg_polarity > 0.1:
                sentiment = 'bullish'
            elif avg_polarity < -0.1:
                sentiment = 'bearish'    # crowd is negative — velocity alone would mislead
            else:
                sentiment = 'neutral'
            mentions[ticker] = {
                'count':     count,
                'upvotes':   upvotes[ticker],
                'velocity':  velocity,
                'sentiment': sentiment,
                'polarity':  avg_polarity,
            }

        with self._lock:
            self._reddit_mentions = mentions
            self._last_reddit     = datetime.now()

    # ── StockTwits ────────────────────────────────────────────────────────────

    def _fetch_stocktwits(self):
        """
        StockTwits trending — different user base than Reddit (more active traders).
        Public endpoint, no API key required.
        """
        try:
            r = requests.get('https://api.stocktwits.com/api/2/trending/symbols.json',
                             headers=HEADERS, timeout=8)
            if r.status_code != 200:
                return
            symbols = r.json().get('symbols', [])
            hot = {}
            for s in symbols:
                ticker = s.get('symbol', '')
                watchlist_count = s.get('watchlist_count', 0)
                if ticker:
                    hot[ticker] = watchlist_count
            with self._lock:
                self._stocktwits_hot  = hot
                self._last_stocktwits = datetime.now()
        except Exception:
            pass

    # ── Time-of-Day Scoring ───────────────────────────────────────────────────

    def _time_of_day(self):
        now_et = datetime.now(_ET) if _ET else datetime.now(timezone.utc) + timedelta(hours=-4)
        m = now_et.hour * 60 + now_et.minute
        if 570 <= m < 600:   return  7, 'Opening momentum window (9:30-10:00 ET)'
        elif 600 <= m < 690: return  3, 'Standard morning session'
        elif 720 <= m < 840: return -8, 'Lunch dead zone (12:00-14:00 ET) — thin books'
        elif 840 <= m < 900: return  5, 'Afternoon momentum session'
        elif 900 <= m < 960: return  8, 'Power hour (15:00-16:00 ET) — volume surge'
        else:                return  0, 'Outside market hours'

    # ── Public API ────────────────────────────────────────────────────────────

    def get_score(self, ticker=None):
        """Composite behavioral score for a ticker (or market-wide if None)."""
        with self._lock:
            sent_vel = self._sentiment_velocity
            rd    = self._reddit_mentions.get((ticker or '').upper(), {})
            st    = self._stocktwits_hot.get((ticker or '').upper(), 0)

        tod, _ = self._time_of_day()

        score = sent_vel + (tod // 2)

        if ticker and rd:
            vel      = rd.get('velocity', 0)
            polarity = rd.get('polarity', 0.0)
            bonus    = min(15, int(vel / 4))
            if polarity > 0.1:
                bonus += 8    # NLP confirms positive sentiment
            elif polarity < -0.1:
                bonus -= 8    # crowd is negative — velocity alone misleads; penalise
            score += bonus

        if ticker and st > 0:
            score += min(8, int(math.log(st + 1)))

        return score

    def get_reddit_score(self, ticker):
        with self._lock:
            rd = self._reddit_mentions.get(ticker.upper(), {})
        if not rd:
            return 0
        polarity = rd.get('polarity', 0.0)
        bonus    = min(15, int(rd.get('velocity', 0) / 4))
        if polarity > 0.1:
            bonus += 8
        elif polarity < -0.1:
            bonus -= 8
        return bonus

    def get_stocktwits_score(self, ticker):
        with self._lock:
            wc = self._stocktwits_hot.get(ticker.upper(), 0)
        return min(8, int(math.log(wc + 1))) if wc else 0

    def get_trending_tickers(self, min_velocity=8.0):
        """Return Reddit-trending tickers — use to expand buy universe."""
        with self._lock:
            return sorted(
                [{'ticker': t, **d} for t, d in self._reddit_mentions.items()
                 if d['velocity'] >= min_velocity],
                key=lambda x: x['velocity'], reverse=True
            )

    def get_summary(self):
        tod_score, tod_note = self._time_of_day()
        with self._lock:
            return {
                'sentiment_velocity':       self._sentiment_velocity,
                'sentiment_velocity_note':  'Social crowd momentum accelerating' if self._sentiment_velocity > 0
                                            else ('Social crowd momentum fading' if self._sentiment_velocity < 0
                                            else 'Social sentiment stable'),
                'time_of_day_score': tod_score,
                'time_of_day_note':  tod_note,
                'reddit_trending':   sorted(
                    [{'ticker': t, **d} for t, d in self._reddit_mentions.items()],
                    key=lambda x: x['velocity'], reverse=True
                )[:25],
                'stocktwits_hot': sorted(
                    [{'ticker': t, 'watchlist_count': c}
                     for t, c in self._stocktwits_hot.items()],
                    key=lambda x: x['watchlist_count'], reverse=True
                )[:20],
                'last_reddit':      self._last_reddit.isoformat() if self._last_reddit else None,
                'last_stocktwits':  self._last_stocktwits.isoformat() if self._last_stocktwits else None,
                'total_score':      self._sentiment_velocity + (tod_score // 2),
                'nlp_engine':       'vader' if HAS_VADER else 'textblob',
            }
