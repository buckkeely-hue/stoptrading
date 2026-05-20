import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
import config

def get_watchlist():
    symbols = config.get('watchlist', [])
    if not symbols:
        return []
    results = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_fetch, s): s for s in symbols}
        for fut in as_completed(futures):
            r = fut.result()
            if r:
                results.append(r)
    results.sort(key=lambda x: x['symbol'])
    return results

def _fetch(symbol):
    try:
        t = yf.Ticker(symbol)
        info = t.fast_info
        price = getattr(info, 'last_price', None)
        if price is None:
            return {'symbol': symbol, 'price': 0, 'change_pct': 0, 'volume': 0, 'error': True}
        prev = getattr(info, 'previous_close', price)
        change_pct = ((price - prev) / prev * 100) if prev else 0
        volume = getattr(info, 'last_volume', 0) or 0
        return {
            'symbol': symbol,
            'price': round(price, 4),
            'change_pct': round(change_pct, 2),
            'volume': int(volume),
            'error': False
        }
    except Exception as e:
        return {'symbol': symbol, 'price': 0, 'change_pct': 0, 'volume': 0, 'error': True}

def add(symbol):
    symbol = symbol.upper().strip()
    wl = config.get('watchlist', [])
    if symbol not in wl:
        wl.append(symbol)
        config.set_key('watchlist', wl)
    return wl

def remove(symbol):
    symbol = symbol.upper().strip()
    wl = config.get('watchlist', [])
    wl = [s for s in wl if s != symbol]
    config.set_key('watchlist', wl)
    return wl
