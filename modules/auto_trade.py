import threading
import time
from datetime import datetime
import yfinance as yf

class AutoTrader:
    def __init__(self, paper_trader, watchlist_manager, config):
        self._paper = paper_trader
        self._watchlist = watchlist_manager
        self._config = config
        self._log = []
        self._running = False
        self._thread = None

    def _get_momentum(self, symbol):
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period='5d')
            if hist.empty or len(hist) < 2:
                return 0.0
            first = float(hist['Close'].iloc[0])
            last = float(hist['Close'].iloc[-1])
            if first <= 0:
                return 0.0
            return ((last / first) - 1) * 100.0
        except Exception:
            return 0.0

    def _get_price(self, symbol):
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.fast_info
            return float(info.last_price) if info.last_price else 0.0
        except Exception:
            return 0.0

    def _log_entry(self, symbol, action, price, reason):
        entry = {
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'symbol': symbol,
            'action': action,
            'price': round(price, 4),
            'reason': reason,
        }
        self._log.insert(0, entry)
        if len(self._log) > 200:
            self._log = self._log[:200]

    def _run(self):
        while self._running:
            try:
                self._check_trades()
            except Exception:
                pass
            for _ in range(60):
                if not self._running:
                    break
                time.sleep(1)

    def _check_trades(self):
        buy_trigger = float(self._config.get('buy_trigger', 3.0))
        sell_target = float(self._config.get('sell_target', 10.0))
        stop_loss = float(self._config.get('stop_loss', 5.0))

        symbols = list(self._watchlist.symbols)
        state = self._paper.get_state()
        positions = {p['symbol']: p for p in state['positions']}

        for symbol in symbols:
            try:
                price = self._get_price(symbol)
                if price <= 0:
                    continue

                if symbol in positions:
                    pos = positions[symbol]
                    pnl_pct = pos['pnl_pct']

                    if pnl_pct >= sell_target:
                        result = self._paper.sell(symbol, pos['shares'])
                        if 'success' in result:
                            self._log_entry(symbol, 'SELL', price, f'Take profit: +{pnl_pct:.1f}%')
                    elif pnl_pct <= -stop_loss:
                        result = self._paper.sell(symbol, pos['shares'])
                        if 'success' in result:
                            self._log_entry(symbol, 'SELL', price, f'Stop loss: {pnl_pct:.1f}%')
                else:
                    momentum = self._get_momentum(symbol)
                    if momentum >= buy_trigger:
                        shares_to_buy = max(1, int(500.0 / price))
                        result = self._paper.buy(symbol, shares_to_buy)
                        if 'success' in result:
                            self._log_entry(symbol, 'BUY', price, f'Momentum: +{momentum:.1f}%')
            except Exception:
                continue

    def start(self):
        if not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def get_log(self):
        return list(self._log)
