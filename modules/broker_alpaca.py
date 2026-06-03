"""
AlpacaBroker — real order-execution adapter, interface-compatible with PaperTrader.

This is the EXECUTION layer (places orders, holds real positions/cash), as distinct from the
data layer (market_feed.py). It mirrors PaperTrader's public surface exactly — buy(), sell(),
register_callback(), get_state(), vault_* — so it drops in behind every existing call site with
no change to the harvester, accounting, or server.

SAFETY CONTRACT (read before funding):
  • Endpoint is chosen by live_mode. live_mode=False → Alpaca PAPER endpoint
    (paper-api.alpaca.markets) using alpaca_paper_key/secret. So with the project's default
    (live_mode stays False) this trades Alpaca's simulator with REAL fills/positions/cash
    semantics but NO real money — exactly the safe test bed.
  • live_mode=True → live endpoint + alpaca_live_key/secret. Only flip after this adapter is
    validated against the paper endpoint.
  • This adapter is NOT used unless broker_execution=True in config. Default False keeps the
    simulated PaperTrader in place. So nothing changes until you opt in.

Orders: a price_override (the bot's intended price) becomes a marketable LIMIT order — correct
for wide-spread pennies, where a naked market order can fill catastrophically. No override →
market order. Each call submits, then polls to a terminal state; balance/positions are read
back FROM the broker (source of truth), never tracked locally.
"""
import time
import threading

import requests

PAPER_BASE = 'https://paper-api.alpaca.markets'
LIVE_BASE  = 'https://api.alpaca.markets'
_FILL_POLL_TIMEOUT = 12.0   # seconds to wait for a market/marketable order to reach terminal state
_FILL_POLL_INTERVAL = 0.4


class AlpacaBroker:
    def __init__(self, config):
        self._config = config
        self._lock = threading.Lock()
        self._trade_callbacks = []
        self._vault = 0.0          # logical profit reserve (see vault_deposit docstring)
        self._refresh_endpoint()

    # ---- endpoint / auth -------------------------------------------------
    def _refresh_endpoint(self):
        live = bool(self._config.get('live_mode'))
        self._base = LIVE_BASE if live else PAPER_BASE
        self._key    = (self._config.get('alpaca_live_key') if live else self._config.get('alpaca_paper_key')) or ''
        self._secret = (self._config.get('alpaca_live_secret') if live else self._config.get('alpaca_paper_secret')) or ''

    def _hdr(self):
        return {'APCA-API-KEY-ID': self._key, 'APCA-API-SECRET-KEY': self._secret}

    def _ready(self):
        self._refresh_endpoint()           # pick up live_mode/key changes pushed onto _config
        return bool(self._key and self._secret)

    def _get(self, path, **params):
        r = requests.get(self._base + path, headers=self._hdr(), params=params or None, timeout=10)
        return r

    # ---- callbacks (accounting hooks) -----------------------------------
    def register_callback(self, fn):
        self._trade_callbacks.append(fn)

    def _fire(self, event):
        for fn in self._trade_callbacks:
            try:
                fn(event)
            except Exception:
                pass

    # ---- order plumbing --------------------------------------------------
    def _position(self, symbol):
        """Return (qty, avg_entry_price) for an open position, or (0, 0.0)."""
        try:
            r = self._get('/v2/positions/%s' % symbol.upper())
            if r.status_code == 200:
                p = r.json()
                return int(float(p.get('qty', 0))), float(p.get('avg_entry_price', 0) or 0)
        except Exception:
            pass
        return 0, 0.0

    def _submit(self, symbol, shares, side, price_override=None):
        """Submit one order and poll to a terminal state. Returns (filled_qty, avg_fill_price, err)."""
        order = {'symbol': symbol.upper(), 'qty': str(int(shares)), 'side': side,
                 'time_in_force': 'day'}
        if price_override is not None and float(price_override) > 0:
            order['type'] = 'limit'
            order['limit_price'] = str(round(float(price_override), 4))
        else:
            order['type'] = 'market'
        try:
            r = requests.post(self._base + '/v2/orders', headers=self._hdr(), json=order, timeout=10)
        except Exception as e:
            return 0, 0.0, 'order submit failed: %s' % e
        if r.status_code not in (200, 201):
            return 0, 0.0, 'order rejected (%d): %s' % (r.status_code, r.text[:200])
        oid = r.json().get('id')

        deadline = _FILL_POLL_TIMEOUT
        waited = 0.0
        while waited < deadline:
            try:
                o = self._get('/v2/orders/%s' % oid).json()
            except Exception:
                o = {}
            status = o.get('status', '')
            filled = int(float(o.get('filled_qty', 0) or 0))
            if status == 'filled':
                return filled, float(o.get('filled_avg_price', 0) or 0), None
            if status in ('canceled', 'rejected', 'expired'):
                if filled > 0:
                    return filled, float(o.get('filled_avg_price', 0) or 0), None
                return 0, 0.0, 'order %s' % status
            time.sleep(_FILL_POLL_INTERVAL)
            waited += _FILL_POLL_INTERVAL

        # Timed out unfilled — cancel so we don't leave a surprise working order.
        try:
            requests.delete(self._base + '/v2/orders/%s' % oid, headers=self._hdr(), timeout=8)
        except Exception:
            pass
        try:
            o = self._get('/v2/orders/%s' % oid).json()
            filled = int(float(o.get('filled_qty', 0) or 0))
            if filled > 0:
                return filled, float(o.get('filled_avg_price', 0) or 0), None
        except Exception:
            pass
        return 0, 0.0, 'not filled within %.0fs (order canceled)' % _FILL_POLL_TIMEOUT

    # ---- public interface (mirrors PaperTrader) --------------------------
    def buy(self, symbol, shares, price_override=None):
        symbol = symbol.upper().strip()
        shares = int(shares)
        if shares <= 0:
            return {'error': 'Shares must be positive'}
        if not self._ready():
            return {'error': 'Alpaca keys not configured for current mode'}
        with self._lock:
            filled, price, err = self._submit(symbol, shares, 'buy', price_override)
            if err:
                return {'error': err}
            if filled <= 0 or price <= 0:
                return {'error': 'buy not filled'}
            total = price * filled
            result = {'ok': True, 'symbol': symbol, 'shares': filled, 'price': price,
                      'total': total, 'avg_cost': round(price, 4)}
            self._fire({'type': 'BUY', **result})
            return result

    def sell(self, symbol, shares, price_override=None):
        symbol = symbol.upper().strip()
        shares = int(shares)
        if shares <= 0:
            return {'error': 'Shares must be positive'}
        if not self._ready():
            return {'error': 'Alpaca keys not configured for current mode'}
        with self._lock:
            held, avg_cost = self._position(symbol)
            if held <= 0:
                return {'error': 'No position in %s' % symbol}
            if held < shares:
                return {'error': 'Only have %d shares of %s' % (held, symbol)}
            filled, price, err = self._submit(symbol, shares, 'sell', price_override)
            if err:
                return {'error': err}
            if filled <= 0 or price <= 0:
                return {'error': 'sell not filled'}
            # Realized P&L from the broker's avg entry vs the fill, net of modeled tx cost so it
            # reconciles with the accounting ledger's convention.
            gross = price * filled
            tx_cost = gross * float(self._config.get('tx_cost_pct', 1.5)) / 100.0
            proceeds = gross - tx_cost
            pnl = proceeds - avg_cost * filled
            result = {'ok': True, 'symbol': symbol, 'shares': filled, 'price': price,
                      'total': round(proceeds, 2), 'avg_cost': round(avg_cost, 4),
                      'pnl': round(pnl, 2)}
            self._fire({'type': 'SELL', **result})
            return result

    def vault_deposit(self, amount):
        """Logical profit reserve only. A broker has no 'vault' — real fund withdrawal is an ACH
        transfer that is deliberately NOT automated here. We track the reserved amount so the
        reported balance/siphon math stays consistent with paper mode; move real cash manually."""
        amount = round(float(amount), 4)
        if amount <= 0:
            return False
        with self._lock:
            self._vault += amount
            return True

    def vault_return(self, amount):
        amount = round(float(amount), 4)
        if amount <= 0:
            return
        with self._lock:
            self._vault = max(0.0, self._vault - amount)

    def get_state(self):
        if not self._ready():
            return {'balance': 0.0, 'unsettled_total': 0.0, 'unsettled': [], 'cash_mode': True,
                    'positions': [], 'history': [], 'error': 'Alpaca keys not configured'}
        balance = 0.0
        positions_detail = []
        try:
            acct = self._get('/v2/account').json()
            # cash that has settled and is available; live buying power differs but cash is the
            # closest analog to PaperTrader's settled 'balance'.
            balance = float(acct.get('cash', 0) or 0) - self._vault
        except Exception:
            pass
        try:
            for p in self._get('/v2/positions').json():
                shares = int(float(p.get('qty', 0) or 0))
                avg = float(p.get('avg_entry_price', 0) or 0)
                cur = float(p.get('current_price', 0) or 0) or avg
                mv = float(p.get('market_value', 0) or 0) or cur * shares
                pnl = float(p.get('unrealized_pl', 0) or 0)
                pnl_pct = float(p.get('unrealized_plpc', 0) or 0) * 100.0
                positions_detail.append({
                    'symbol': p.get('symbol'), 'shares': shares, 'avg_cost': round(avg, 4),
                    'current_price': round(cur, 4), 'market_value': round(mv, 2),
                    'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct, 2),
                })
        except Exception:
            pass
        history = []
        try:
            for a in self._get('/v2/account/activities/FILL', page_size=50).json():
                history.append({
                    'time': a.get('transaction_time', '')[:19].replace('T', ' '),
                    'symbol': a.get('symbol'),
                    'action': (a.get('side', '') or '').upper(),
                    'shares': int(float(a.get('qty', 0) or 0)),
                    'price': round(float(a.get('price', 0) or 0), 4),
                    'total': round(float(a.get('price', 0) or 0) * float(a.get('qty', 0) or 0), 2),
                })
        except Exception:
            pass
        return {
            'balance': round(balance, 2),
            'unsettled_total': 0.0, 'unsettled': [],
            'cash_mode': bool(self._config.get('cash_account_mode', False)),
            'positions': positions_detail,
            'history': history[:50],
            'vault': round(self._vault, 2),
            'broker': 'alpaca-%s' % ('live' if self._config.get('live_mode') else 'paper'),
        }


def get_trader(config):
    """Factory: real Alpaca execution if opted in, else the simulated PaperTrader.

    broker_execution=True switches the whole bot's execution onto Alpaca. With live_mode=False
    that's Alpaca's PAPER endpoint (safe). Default (flag absent/False) returns PaperTrader, so
    existing behavior is unchanged until you explicitly opt in."""
    if config.get('broker_execution'):
        return AlpacaBroker(config)
    from modules.paper_trade import PaperTrader
    return PaperTrader(config)
