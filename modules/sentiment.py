import yfinance as yf

class SentimentAnalyzer:
    def __init__(self, config):
        self._config = config

    def _textblob_polarity(self, texts):
        try:
            from textblob import TextBlob
            if not texts:
                return 0.0
            scores = []
            for text in texts:
                try:
                    scores.append(TextBlob(str(text)).sentiment.polarity)
                except Exception:
                    pass
            return round(sum(scores) / len(scores), 3) if scores else 0.0
        except ImportError:
            return 0.0

    def _reddit_analysis(self, symbol):
        client_id = self._config.get('reddit_client_id', '')
        secret = self._config.get('reddit_secret', '')
        if not client_id or not secret:
            return 0, 0.0

        try:
            import praw
            reddit = praw.Reddit(
                client_id=client_id,
                client_secret=secret,
                user_agent='StopTrading/1.0',
            )
            subreddits = reddit.subreddit('pennystocks+wallstreetbets')
            mentions = []
            texts = []
            for submission in subreddits.search(symbol, limit=25, time_filter='week'):
                title_lower = submission.title.lower()
                if symbol.lower() in title_lower or f'${symbol.lower()}' in title_lower:
                    mentions.append(submission.title)
                    texts.append(submission.title)
                    if submission.selftext:
                        texts.append(submission.selftext[:200])

            count = len(mentions)
            score = self._textblob_polarity(texts)
            return count, score
        except Exception:
            return 0, 0.0

    def _news_analysis(self, symbol):
        try:
            ticker = yf.Ticker(symbol)
            news = ticker.news
            if not news:
                return [], 0.0
            headlines = []
            for item in news[:10]:
                title = item.get('title', '') or item.get('content', {}).get('title', '')
                if title:
                    headlines.append(str(title))
            score = self._textblob_polarity(headlines)
            return headlines[:8], score
        except Exception:
            return [], 0.0

    def analyze(self, symbol):
        symbol = symbol.upper().strip()

        reddit_mentions, reddit_score = self._reddit_analysis(symbol)
        news_headlines, news_score = self._news_analysis(symbol)

        weights = []
        if reddit_mentions > 0:
            weights.append(reddit_score)
        if news_headlines:
            weights.append(news_score)

        if weights:
            combined = sum(weights) / len(weights)
        else:
            combined = 0.0

        if combined > 0.05:
            label = 'Bullish'
        elif combined < -0.05:
            label = 'Bearish'
        else:
            label = 'Neutral'

        return {
            'symbol': symbol,
            'reddit_mentions': reddit_mentions,
            'reddit_score': round(reddit_score, 3),
            'news_headlines': news_headlines,
            'news_score': round(news_score, 3),
            'combined_score': round(combined, 3),
            'sentiment_label': label,
        }
