import re
import requests
import config

try:
    import praw
    PRAW_OK = True
except ImportError:
    PRAW_OK = False

try:
    from textblob import TextBlob
    TEXTBLOB_OK = True
except ImportError:
    TEXTBLOB_OK = False

BULLISH_WORDS = ['moon','bullish','buy','calls','long','pump','squeeze','breakout','surge','soar','rocket','yolo','ath','gains','green']
BEARISH_WORDS = ['dump','bearish','sell','puts','short','crash','tank','drop','baghold','loss','red','rekt','bankrupt','dilute']

def _polarity(text):
    if TEXTBLOB_OK:
        try:
            return TextBlob(text).sentiment.polarity
        except Exception:
            pass
    text_lower = text.lower()
    bull = sum(1 for w in BULLISH_WORDS if w in text_lower)
    bear = sum(1 for w in BEARISH_WORDS if w in text_lower)
    total = bull + bear
    if total == 0:
        return 0
    return (bull - bear) / total

def _get_reddit_posts(symbol):
    cfg = config.load()
    cid = cfg.get('reddit_client_id', '')
    secret = cfg.get('reddit_client_secret', '')
    ua = cfg.get('reddit_user_agent', 'StopTrading/1.0')
    if not PRAW_OK or not cid or not secret:
        return []
    try:
        reddit = praw.Reddit(client_id=cid, client_secret=secret, user_agent=ua)
        posts = []
        for sub in ['pennystocks', 'wallstreetbets', 'stocks', 'investing']:
            try:
                for submission in reddit.subreddit(sub).search(symbol, limit=10, time_filter='week'):
                    posts.append(submission.title + ' ' + (submission.selftext or ''))
            except Exception:
                pass
        return posts
    except Exception:
        return []

def _get_yahoo_news(symbol):
    try:
        url = f'https://query1.finance.yahoo.com/v1/finance/search?q={symbol}&newsCount=10'
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=5)
        data = r.json()
        news = data.get('news', [])
        return [n.get('title', '') for n in news]
    except Exception:
        return []

def analyze(symbol):
    symbol = symbol.upper().strip()
    reddit_posts = _get_reddit_posts(symbol)
    yahoo_news = _get_yahoo_news(symbol)
    all_texts = reddit_posts + yahoo_news
    if not all_texts:
        return {
            'symbol': symbol,
            'score': 0,
            'label': 'Neutral',
            'reddit_mentions': 0,
            'news_count': len(yahoo_news),
            'headlines': [],
            'bullish_signals': 0,
            'bearish_signals': 0
        }
    scores = [_polarity(t) for t in all_texts]
    avg_score = sum(scores) / len(scores) if scores else 0
    bullish = sum(1 for s in scores if s > 0.05)
    bearish = sum(1 for s in scores if s < -0.05)
    label = 'Bullish' if avg_score > 0.05 else ('Bearish' if avg_score < -0.05 else 'Neutral')
    normalized = int(avg_score * 100)
    return {
        'symbol': symbol,
        'score': max(-100, min(100, normalized)),
        'label': label,
        'reddit_mentions': len(reddit_posts),
        'news_count': len(yahoo_news),
        'headlines': yahoo_news[:8],
        'bullish_signals': bullish,
        'bearish_signals': bearish
    }
