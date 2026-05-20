import time
import threading
import yfinance as yf
import config
import modules.paper_trade as paper_trade

_log = []
_running = False
_thread = None
_lock = threading.Lock()

def get_status():
    cfg = config.load()
    with _lock:
        return {
            'enabled': cfg.get('auto_trade_enabled', False),
            'running': _running,
            'buy_threshold': cfg.get('buy_momentum_threshold', 5.0),
            'sell_target': cfg.get('sell_profit_target', 10.0),
            'stop_loss': cfg.get('stop_loss_pct', 5.0),
            'log': list(_log[-50:])
        }

def _log_entry(msg, action='INFO'):
    entry = {'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'action': action, 'msg': msg}
    with _lock:
        _log.insert(0, entry)
        if len(_log) > 200:
            _log.pop()
    return entry

def toggle():
    cfg = config.load()
    enabled = not cfg.get('auto_trade_enabled', False)
    cfg['auto_trade_enabled'] = enabled
    config.save(cfg)
    if enabled:
        _start_loop()
        _log_entry('Auto-trade ENABLED', 'SYSTEM')
    else:
        _log_entry('Auto-trade DISABLED', 'SYSTEM')
    return enabled

def _start_loop():
    global _thread, _running
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()

def _loop():
    global _running
    while True:
        cfg = config.load()
        if not cfg.get('auto_trade_enabled', False):
            _running = False
            break
        try:
            _run_checks()
        except Exception as e:
            _log_entry(f'Loop error: {e}', 'ERROR')
        time.sleep(60)

def _run_checks():
    cfg = config.load()
    buy_thr = cfg.get('buy_momentum_threshold', 5.0)
    sell_tgt = cfg.get('sell_profit_target', 10.0)
    stop_loss = cfg.get('stop_loss_pct', 5.0)
    state = paper_trade.get_state()
    for pos in state['positions']:
        sym = pos['symbol']
        pl_pct = pos['pl_pct']
        if pl_pct >= sell_tgt:
            ok, msg = paper_trade.sell(sym, pos['shares'])
            _log_entry(f'SELL {sym}: profit target {pl_pct:.1f}% >= {sell_tgt}% — {msg}', 'SELL')
        elif pl_pct <= -stop_loss:
            ok, msg = paper_trade.sell(sym, pos['shares'])
            _log_entry(f'SELL {sym}: stop loss {pl_pct:.1f}% <= -{stop_loss}% — {msg}', 'STOP')
    watchlist = cfg.get('watchlist', [])
    held = {p['symbol'] for p in state['positions']}
    balance = state['balance']
    if balance < 50:
        return
    for sym in watchlist:
        if sym in held:
            continue
        try:
            t = yf.Ticker(sym)
            info = t.fast_info
            price = getattr(info, 'last_price', None)
            if not price or price > 5:
                continue
            prev = getattr(info, 'previous_close', price)
            change_pct = ((price - prev) / prev * 100) if prev else 0
            if change_pct >= buy_thr:
                spend = min(balance * 0.1, 500)
                shares = int(spend / price)
                if shares < 1:
                    continue
                ok, msg = paper_trade.buy(sym, shares)
                _log_entry(f'BUY {sym}: momentum {change_pct:.1f}% >= {buy_thr}% — {msg}', 'BUY')
        except Exception as e:
            _log_entry(f'Check {sym} error: {e}', 'ERROR')

def init():
    cfg = config.load()
    if cfg.get('auto_trade_enabled', False):
        _start_loop()
        _log_entry('Auto-trade resumed from saved state', 'SYSTEM')
