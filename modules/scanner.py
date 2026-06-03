import pandas as pd
from modules.universe import FALLBACK
from modules.market_data import MarketData


class StockScanner:
    def __init__(self, universe=None, config=None):
        self.universe = universe   # DynamicUniverse instance — uses FALLBACK if None
        self.config   = config or {}
        self._md      = MarketData(config or {})

    def scan(self):
        symbols = list(set(
            self.universe.get() if self.universe else FALLBACK
        ))
        try:
            data = self._md.download(
                tickers=' '.join(symbols),
                period='25d', interval='1d',
                group_by='ticker', auto_adjust=True,
                progress=False, threads=True,
            )
        except Exception:
            return []

        results = []
        meta = self.universe.get_meta() if self.universe else {}

        for symbol in symbols:
            try:
                hist = data if len(symbols) == 1 else data.get(symbol, None)
                if hist is None:
                    continue
                hist = hist.dropna()
                if hist.empty or len(hist) < 2:
                    continue

                price = float(hist['Close'].iloc[-1])
                if price <= 0 or price >= float(self.config.get('max_stock_price', 15.0)):
                    continue

                prev_close = float(hist['Close'].iloc[-2])
                change_pct = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0.0
                volume = int(hist['Volume'].iloc[-1]) if not pd.isna(hist['Volume'].iloc[-1]) else 0
                avg_volume = int(hist['Volume'].mean()) if len(hist) >= 5 else volume
                volume_ratio = volume / avg_volume if avg_volume > 0 else 1.0
                ma20 = float(hist['Close'].tail(20).mean()) if len(hist) >= 5 else price
                momentum = ((price / ma20) - 1) * 100 if ma20 > 0 else 0.0

                score = (min(volume_ratio, 5.0) / 5.0) * 35.0
                score += min(abs(change_pct), 20.0) / 20.0 * 25.0
                score += (min(max(momentum, -50.0), 50.0) / 50.0) * 25.0 + 25.0
                score = min(max(score, 0.0), 100.0)

                # Boost score for multi-stream consensus picks
                source_count = meta.get(symbol, {}).get('source_count', 1)
                score = min(score + (source_count - 1) * 5, 100.0)

                results.append({
                    'symbol':       symbol,
                    'price':        round(price, 4),
                    'change_pct':   round(change_pct, 2),
                    'volume':       volume,
                    'avg_volume':   avg_volume,
                    'score':        round(score, 1),
                    'source_count': source_count,
                    'sources':      meta.get(symbol, {}).get('sources', []),
                })
            except Exception:
                continue

        results.sort(key=lambda x: x['score'], reverse=True)
        return results[:20]
