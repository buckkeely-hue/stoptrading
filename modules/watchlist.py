import json
import os
import yfinance as yf

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), '..', 'watchlist.json')

class WatchlistManager:
    def __init__(self):
        self._load()

    def _load(self):
        if os.path.exists(WATCHLIST_FILE):
            try:
                with open(WATCHLIST_FILE, 'r') as f:
                    data = json.load(f)
                self.symbols = list(data) if isinstance(data, list) else []
            except Exception:
                self.symbols = []
        else:
            self.symbols = []

    def _save(self):
        with open(WATCHLIST_FILE, 'w') as f:
            json.dump(self.symbols, f, indent=2)

    def add(self, symbol):
        symbol = symbol.upper().strip()
        if symbol and symbol not in self.symbols:
            self.symbols.append(symbol)
            self._save()

    def remove(self, symbol):
        symbol = symbol.upper().strip()
        if symbol in self.symbols:
            self.symbols.remove(symbol)
            self._save()

    def get_prices(self):
        results = []
        for symbol in self.symbols:
            try:
                ticker = yf.Ticker(symbol)
                info = ticker.fast_info
                price = 0.0
                try:
                    price = float(info.last_price) if info.last_price else 0.0
                except Exception:
                    pass

                prev_close = 0.0
                try:
                    prev_close = float(info.previous_close) if info.previous_close else 0.0
                except Exception:
                    pass

                change_pct = 0.0
                if prev_close and prev_close > 0:
                    change_pct = ((price - prev_close) / prev_close) * 100.0

                volume = 0
                try:
                    volume = int(info.last_volume) if info.last_volume else 0
                except Exception:
                    pass

                results.append({
                    'symbol': symbol,
                    'price': round(price, 4),
                    'change_pct': round(change_pct, 2),
                    'volume': volume,
                })
            except Exception:
                results.append({
                    'symbol': symbol,
                    'price': 0.0,
                    'change_pct': 0.0,
                    'volume': 0,
                })
        return results
