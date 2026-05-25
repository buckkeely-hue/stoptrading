import yfinance as yf
import numpy as np
from datetime import datetime

class TrendAnalyzer:
    def analyze(self, symbol):
        symbol = symbol.upper().strip()
        try:
            ticker = yf.Ticker(symbol)
            # 5-minute bars for today (covers 5 trading hours)
            hist = ticker.history(period='2d', interval='5m')
            if hist.empty:
                return {'error': 'No intraday data available for ' + symbol}

            # Take last 60 bars = 5 hours of 5-min bars
            hist = hist.tail(60)
            if len(hist) < 6:
                return {'error': 'Not enough intraday data for ' + symbol}

            closes = hist['Close'].values.tolist()
            volumes = hist['Volume'].values.tolist()
            times = [str(t)[:16] for t in hist.index.tolist()]

            current_price = closes[-1]
            open_5h = closes[0]
            pct_change_5h = ((current_price - open_5h) / open_5h) * 100 if open_5h > 0 else 0.0

            # Linear regression slope over 5h window
            x = np.arange(len(closes))
            slope, intercept = np.polyfit(x, closes, 1)
            slope_pct = (slope / open_5h) * 100 if open_5h > 0 else 0.0

            # Volume trend — compare first half vs second half
            mid = len(volumes) // 2
            avg_vol_first = sum(volumes[:mid]) / max(mid, 1)
            avg_vol_second = sum(volumes[mid:]) / max(len(volumes) - mid, 1)
            vol_increasing = avg_vol_second > avg_vol_first

            # 1-hour price levels
            last_12 = closes[-12:] if len(closes) >= 12 else closes
            support = min(last_12)
            resistance = max(last_12)

            # Signal logic
            if slope_pct > 0.05 and vol_increasing and pct_change_5h > 0:
                signal = 'BUY'
                reason = 'Upward trend with increasing volume over 5h'
            elif slope_pct < -0.05 and pct_change_5h < 0:
                signal = 'SELL'
                reason = 'Downward trend over 5h — wait for reversal'
            elif abs(slope_pct) <= 0.05:
                signal = 'HOLD'
                reason = 'Sideways movement — no clear trend'
            else:
                signal = 'WATCH'
                reason = 'Mixed signals — monitor closely'

            return {
                'symbol': symbol,
                'current_price': round(current_price, 4),
                'open_5h': round(open_5h, 4),
                'pct_change_5h': round(pct_change_5h, 2),
                'slope_pct': round(slope_pct, 4),
                'vol_increasing': vol_increasing,
                'support': round(support, 4),
                'resistance': round(resistance, 4),
                'signal': signal,
                'reason': reason,
                'prices': [round(p, 4) for p in closes],
                'times': times,
                'bar_count': len(closes),
            }
        except Exception as e:
            return {'error': str(e)}
