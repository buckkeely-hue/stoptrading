import json
import os
import threading
from datetime import datetime
import yfinance as yf

TRADES_FILE = os.path.join(os.path.dirname(__file__), '..', 'paper_trades.json')

class PaperTrader:
    def __init__(self, config):
        self._lock = threading.Lock()
        self._config = config
        self._trade_callbacks = []   # accounting hooks
        self._load()

    def _load(self):
        if os.path.exists(TRADES_FILE):
            try:
                with open(TRADES_FILE, 'r') as f:
                    data = json.load(f)
                self._state = data
                if 'balance' not in self._state:
                    self._state['balance'] = float(self._config.get('paper_balance', 10000))
                if 'positions' not in self._state:
                    self._state['positions'] = {}
                if 'history' not in self._state:
                    self._state['history'] = []
                return
            except Exception:
                pass
        self._state = {
            'balance': float(self._config.get('paper_balance', 10000)),
            'positions': {},
            'history': [],
        }

    def register_callback(self, fn):
        self._trade_callbacks.append(fn)

    def _fire(self, event):
        for fn in self._trade_callbacks:
            try:
                fn(event)
            except Exception:
                pass

    def _save(self):
        with open(TRADES_FILE, 'w') as f:
            json.dump(self._state, f, indent=2)

    def _get_price(self, symbol):
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        price = float(info.last_price) if info.last_price else 0.0
        if price <= 0:
            hist = ticker.history(period='1d')
            if not hist.empty:
                price = float(hist['Close'].iloc[-1])
        return price

    def buy(self, symbol, shares):
        symbol = symbol.upper().strip()
        shares = int(shares)
        if shares <= 0:
            return {'error': 'Shares must be positive'}

        with self._lock:
            try:
                price = self._get_price(symbol)
            except Exception as e:
                return {'error': f'Could not fetch price: {e}'}

            if price <= 0:
                return {'error': f'Invalid price for {symbol}'}

            total_cost = price * shares
            if total_cost > self._state['balance']:
                return {'error': f'Insufficient balance. Need ${total_cost:.2f}, have ${self._state["balance"]:.2f}'}

            self._state['balance'] -= total_cost
            pos = self._state['positions']
            if symbol in pos:
                old_shares = pos[symbol]['shares']
                old_cost = pos[symbol]['avg_cost']
                new_shares = old_shares + shares
                new_cost = ((old_shares * old_cost) + (shares * price)) / new_shares
                pos[symbol] = {'shares': new_shares, 'avg_cost': round(new_cost, 4)}
            else:
                pos[symbol] = {'shares': shares, 'avg_cost': round(price, 4)}

            entry = {
                'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'symbol': symbol,
                'action': 'BUY',
                'shares': shares,
                'price': round(price, 4),
                'total': round(total_cost, 2),
            }
            self._state['history'].insert(0, entry)
            self._save()
            result = {'ok': True, 'symbol': symbol, 'shares': shares, 'price': price,
                      'total': total_cost, 'avg_cost': round(price, 4)}
            self._fire({'type': 'BUY', **result})
            return result

    def sell(self, symbol, shares):
        symbol = symbol.upper().strip()
        shares = int(shares)
        if shares <= 0:
            return {'error': 'Shares must be positive'}

        with self._lock:
            pos = self._state['positions']
            if symbol not in pos:
                return {'error': f'No position in {symbol}'}
            if pos[symbol]['shares'] < shares:
                return {'error': f'Only have {pos[symbol]["shares"]} shares of {symbol}'}

            try:
                price = self._get_price(symbol)
            except Exception as e:
                return {'error': f'Could not fetch price: {e}'}

            if price <= 0:
                return {'error': f'Invalid price for {symbol}'}

            avg_cost = pos[symbol]['avg_cost']
            proceeds = price * shares
            pnl = (price - avg_cost) * shares
            self._state['balance'] += proceeds

            pos[symbol]['shares'] -= shares
            if pos[symbol]['shares'] == 0:
                del pos[symbol]

            entry = {
                'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'symbol': symbol,
                'action': 'SELL',
                'shares': shares,
                'price': round(price, 4),
                'total': round(proceeds, 2),
                'pnl': round(pnl, 2),
            }
            self._state['history'].insert(0, entry)
            self._save()
            result = {'ok': True, 'symbol': symbol, 'shares': shares, 'price': price,
                      'total': proceeds, 'avg_cost': round(avg_cost, 4), 'pnl': round(pnl, 2)}
            self._fire({'type': 'SELL', **result})
            return result

    def get_state(self):
        with self._lock:
            positions_detail = []
            for symbol, pos in self._state['positions'].items():
                try:
                    ticker = yf.Ticker(symbol)
                    info = ticker.fast_info
                    current_price = float(info.last_price) if info.last_price else pos['avg_cost']
                except Exception:
                    current_price = pos['avg_cost']

                shares = pos['shares']
                avg_cost = pos['avg_cost']
                market_value = current_price * shares
                cost_basis = avg_cost * shares
                pnl = market_value - cost_basis
                pnl_pct = ((current_price / avg_cost) - 1) * 100.0 if avg_cost > 0 else 0.0

                positions_detail.append({
                    'symbol': symbol,
                    'shares': shares,
                    'avg_cost': round(avg_cost, 4),
                    'current_price': round(current_price, 4),
                    'market_value': round(market_value, 2),
                    'pnl': round(pnl, 2),
                    'pnl_pct': round(pnl_pct, 2),
                })

            return {
                'balance': round(self._state['balance'], 2),
                'positions': positions_detail,
                'history': self._state['history'][:50],
            }
