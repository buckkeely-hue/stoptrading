"""
Internal Accounting Ledger.

Tracks two separate accounts:
  - Trading Capital  : funds available for buying/selling stock
  - Profit Reserve   : siphoned profits extracted from winning trades

Every buy and sell executed through PaperTrader is recorded here automatically
via callback. When a trade closes with a net gain, a configurable percentage
(siphon_rate) of that profit is automatically transferred to the Profit Reserve.

In a future live-money mode, the same ledger works — just swap PaperTrader for
a real broker, keep the accounting identical.
"""

import json
import os
import threading
from datetime import datetime

LEDGER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'accounting.json')


class AccountingLedger:

    def __init__(self, config, paper_trader):
        self._lock = threading.Lock()
        self.config = config
        self._paper = paper_trader
        self._load()
        paper_trader.register_callback(self._on_trade)

    # ── Persistence ──────────────────────────────────────────────────────────

    def _default_state(self):
        starting = float(self.config.get('paper_balance', 10000))
        return {
            'trading_balance':  starting,
            'profit_reserve':   0.0,
            'total_deposited':  starting,
            'total_siphoned':   0.0,
            'entries':          [],
            'stats': {
                'total_trades':  0,
                'total_buys':    0,
                'total_sells':   0,
                'total_profit':  0.0,
                'total_loss':    0.0,
                'winners':       0,
                'losers':        0,
                'best_trade':    {'symbol': '—', 'pnl': 0.0},
                'worst_trade':   {'symbol': '—', 'pnl': 0.0},
            },
        }

    def _load(self):
        try:
            if os.path.exists(LEDGER_FILE):
                with open(LEDGER_FILE) as f:
                    self._state = json.load(f)
                if 'stats' not in self._state:
                    self._state['stats'] = self._default_state()['stats']
                return
        except Exception:
            pass
        self._state = self._default_state()

    def _save(self):
        try:
            data = dict(self._state)
            data['entries'] = self._state['entries'][-1000:]
            with open(LEDGER_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    # ── Trade Callback (called by PaperTrader after every buy/sell) ───────────

    def _on_trade(self, event):
        trade_type = event.get('type')
        symbol = event.get('symbol', '')
        shares = event.get('shares', 0)
        price  = event.get('price', 0.0)
        total  = event.get('total', 0.0)
        trigger = event.get('trigger', 'manual')

        with self._lock:
            if trade_type == 'BUY':
                self._state['stats']['total_buys'] += 1
                self._state['stats']['total_trades'] += 1
                self._add_entry('BUY', symbol, shares, price, -total, 0.0, trigger, 'trading')

            elif trade_type == 'SELL':
                pnl = event.get('pnl', 0.0)
                self._state['stats']['total_sells'] += 1
                self._state['stats']['total_trades'] += 1

                if pnl > 0:
                    self._state['stats']['winners'] += 1
                    self._state['stats']['total_profit'] += pnl
                    best = self._state['stats']['best_trade']
                    if pnl > best['pnl']:
                        self._state['stats']['best_trade'] = {'symbol': symbol, 'pnl': round(pnl, 2)}
                else:
                    self._state['stats']['losers'] += 1
                    self._state['stats']['total_loss'] += abs(pnl)
                    worst = self._state['stats']['worst_trade']
                    if pnl < worst['pnl']:
                        self._state['stats']['worst_trade'] = {'symbol': symbol, 'pnl': round(pnl, 2)}

                self._add_entry('SELL', symbol, shares, price, total, pnl, trigger, 'trading')

                # Auto-siphon: move a % of net profit to the Profit Reserve
                siphon_rate = float(self.config.get('siphon_rate', 50.0))
                auto_siphon = self.config.get('auto_siphon', True)
                if auto_siphon and pnl > 0:
                    siphon_amt = round(pnl * (siphon_rate / 100.0), 2)
                    if siphon_amt > 0:
                        self._state['profit_reserve']  += siphon_amt
                        self._state['total_siphoned']  += siphon_amt
                        note = '{:.0f}% siphon from ${:.2f} gain on {}'.format(
                            siphon_rate, pnl, symbol)
                        self._add_entry('SIPHON', symbol, 0, 0.0, siphon_amt, pnl, note, 'profit')

            # Keep trading_balance in sync with paper_trader
            self._state['trading_balance'] = round(self._paper._state.get('balance', 0), 2)
            self._save()

    # ── Manual Siphon ─────────────────────────────────────────────────────────

    def manual_siphon(self, amount):
        amount = round(float(amount), 2)
        with self._lock:
            trading = self._state['trading_balance']
            if amount <= 0 or amount > trading:
                return {'error': 'Invalid amount — must be > 0 and ≤ trading balance'}
            self._state['profit_reserve'] += amount
            self._state['total_siphoned'] += amount
            self._state['trading_balance'] -= amount
            self._add_entry('SIPHON', '—', 0, 0.0, amount, 0.0, 'manual transfer', 'profit')
            self._save()
            return {'ok': True, 'siphoned': amount,
                    'profit_reserve': self._state['profit_reserve'],
                    'trading_balance': self._state['trading_balance']}

    def return_to_trading(self, amount):
        amount = round(float(amount), 2)
        with self._lock:
            reserve = self._state['profit_reserve']
            if amount <= 0 or amount > reserve:
                return {'error': 'Invalid amount — must be > 0 and ≤ profit reserve'}
            self._state['profit_reserve'] -= amount
            self._state['trading_balance'] += amount
            self._add_entry('RETURN', '—', 0, 0.0, amount, 0.0, 'returned to trading', 'trading')
            self._save()
            return {'ok': True, 'returned': amount,
                    'profit_reserve': self._state['profit_reserve'],
                    'trading_balance': self._state['trading_balance']}

    def reset(self, starting_balance=None):
        with self._lock:
            if starting_balance is None:
                starting_balance = float(self.config.get('paper_balance', 10000))
            self._state = self._default_state()
            self._state['trading_balance'] = starting_balance
            self._state['total_deposited'] = starting_balance
            self._add_entry('DEPOSIT', '—', 0, 0.0, starting_balance, 0.0,
                            'Account opened', 'trading')
            self._save()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _add_entry(self, action, symbol, shares, price, amount, pnl, trigger, account):
        entry_id = len(self._state['entries']) + 1
        self._state['entries'].append({
            'id':      entry_id,
            'time':    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'action':  action,
            'symbol':  symbol,
            'shares':  shares,
            'price':   round(price, 4),
            'amount':  round(amount, 2),
            'pnl':     round(pnl, 2),
            'trigger': trigger,
            'account': account,
            'reserve_balance': round(self._state.get('profit_reserve', 0), 2),
        })

    # ── Report ────────────────────────────────────────────────────────────────

    def get_report(self):
        with self._lock:
            st = self._state['stats']
            total_trades = st['total_trades']
            wins = st['winners']
            win_rate = round(wins / max(st['total_sells'], 1) * 100, 1)
            net_pnl = round(st['total_profit'] - st['total_loss'], 2)

            return {
                'trading_balance':  round(self._state['trading_balance'], 2),
                'profit_reserve':   round(self._state['profit_reserve'], 2),
                'total_deposited':  round(self._state['total_deposited'], 2),
                'total_siphoned':   round(self._state['total_siphoned'], 2),
                'stats': {
                    'total_trades':  total_trades,
                    'total_buys':    st['total_buys'],
                    'total_sells':   st['total_sells'],
                    'winners':       wins,
                    'losers':        st['losers'],
                    'win_rate':      win_rate,
                    'total_profit':  round(st['total_profit'], 2),
                    'total_loss':    round(st['total_loss'], 2),
                    'net_pnl':       net_pnl,
                    'best_trade':    st['best_trade'],
                    'worst_trade':   st['worst_trade'],
                },
                'entries': list(reversed(self._state['entries'][-200:])),
                'siphon_rate': float(self.config.get('siphon_rate', 50.0)),
                'auto_siphon': bool(self.config.get('auto_siphon', True)),
            }
