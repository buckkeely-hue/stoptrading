import json
import os
import time
import yfinance as yf
import config

TRADES_FILE = os.path.join(os.path.dirname(__file__), '..', 'paper_trades.json')

def _load():
    if not os.path.exists(TRADES_FILE):
        starting = config.get('starting_balance', 10000.0)
        data = {'balance': starting, 'positions': {}, 'history': []}
        _save(data)
        return data
    try:
        with open(TRADES_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        starting = config.get('starting_balance', 10000.0)
        return {'balance': starting, 'positions': {}, 'history': []}

def _save(data):
    with open(TRADES_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def _get_price(symbol):
    try:
        t = yf.Ticker(symbol)
        p = t.fast_info.last_price
        return float(p) if p else None
    except Exception:
        return None

def get_state():
    data = _load()
    positions_out = []
    total_pl = 0
    for sym, pos in data['positions'].items():
        price = _get_price(sym) or pos['avg_cost']
        pl = (price - pos['avg_cost']) * pos['shares']
        pl_pct = ((price - pos['avg_cost']) / pos['avg_cost'] * 100) if pos['avg_cost'] else 0
        total_pl += pl
        positions_out.append({
            'symbol': sym,
            'shares': pos['shares'],
            'avg_cost': round(pos['avg_cost'], 4),
            'current_price': round(price, 4),
            'pl': round(pl, 2),
            'pl_pct': round(pl_pct, 2),
            'value': round(price * pos['shares'], 2)
        })
    positions_out.sort(key=lambda x: x['symbol'])
    return {
        'balance': round(data['balance'], 2),
        'positions': positions_out,
        'history': data['history'][-100:],
        'total_pl': round(total_pl, 2)
    }

def buy(symbol, shares):
    symbol = symbol.upper().strip()
    shares = int(shares)
    if shares <= 0:
        return False, 'Shares must be positive'
    price = _get_price(symbol)
    if not price:
        return False, f'Could not get price for {symbol}'
    data = _load()
    cost = price * shares
    if cost > data['balance']:
        return False, f'Insufficient funds. Need ${cost:.2f}, have ${data["balance"]:.2f}'
    data['balance'] -= cost
    if symbol in data['positions']:
        pos = data['positions'][symbol]
        total_shares = pos['shares'] + shares
        total_cost = pos['avg_cost'] * pos['shares'] + price * shares
        data['positions'][symbol] = {'shares': total_shares, 'avg_cost': total_cost / total_shares}
    else:
        data['positions'][symbol] = {'shares': shares, 'avg_cost': price}
    data['history'].insert(0, {
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        'action': 'BUY',
        'symbol': symbol,
        'shares': shares,
        'price': round(price, 4),
        'total': round(cost, 2)
    })
    _save(data)
    return True, f'Bought {shares} shares of {symbol} @ ${price:.4f}'

def sell(symbol, shares):
    symbol = symbol.upper().strip()
    shares = int(shares)
    if shares <= 0:
        return False, 'Shares must be positive'
    data = _load()
    if symbol not in data['positions']:
        return False, f'No position in {symbol}'
    pos = data['positions'][symbol]
    if shares > pos['shares']:
        return False, f'Only have {pos["shares"]} shares of {symbol}'
    price = _get_price(symbol)
    if not price:
        return False, f'Could not get price for {symbol}'
    proceeds = price * shares
    data['balance'] += proceeds
    pl = (price - pos['avg_cost']) * shares
    if shares == pos['shares']:
        del data['positions'][symbol]
    else:
        data['positions'][symbol]['shares'] -= shares
    data['history'].insert(0, {
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        'action': 'SELL',
        'symbol': symbol,
        'shares': shares,
        'price': round(price, 4),
        'total': round(proceeds, 2),
        'pl': round(pl, 2)
    })
    _save(data)
    return True, f'Sold {shares} shares of {symbol} @ ${price:.4f} (P&L: ${pl:+.2f})'

def reset(new_balance=None):
    if new_balance is None:
        new_balance = config.get('starting_balance', 10000.0)
    data = {'balance': float(new_balance), 'positions': {}, 'history': []}
    _save(data)
    return data
