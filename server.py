import os
import sys
import threading
import webbrowser
import time
from flask import Flask, jsonify, request, send_from_directory

sys.path.insert(0, os.path.dirname(__file__))

import config
import modules.scanner as scanner
import modules.watchlist as watchlist
import modules.paper_trade as paper_trade
import modules.sentiment as sentiment
import modules.auto_trade as auto_trade

app = Flask(__name__, static_folder=os.path.dirname(__file__))

_scan_cache = {'result': [], 'ts': 0}
_scan_lock = threading.Lock()
_sentiment_cache = {}

# ─── Static files ────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(os.path.dirname(__file__), 'index.html')

@app.route('/index.css')
def css():
    return send_from_directory(os.path.dirname(__file__), 'index.css')

# ─── Scanner ─────────────────────────────────────────────────────────────────

@app.route('/api/scan')
def api_scan():
    with _scan_lock:
        result = scanner.run_scan(_sentiment_cache)
        _scan_cache['result'] = result
        _scan_cache['ts'] = time.time()
    return jsonify({'stocks': result, 'ts': _scan_cache['ts']})

@app.route('/api/scan/cached')
def api_scan_cached():
    with _scan_lock:
        return jsonify({'stocks': _scan_cache['result'], 'ts': _scan_cache['ts']})

# ─── Watchlist ────────────────────────────────────────────────────────────────

@app.route('/api/watchlist')
def api_watchlist():
    data = watchlist.get_watchlist()
    state = paper_trade.get_state()
    pos_map = {p['symbol']: p for p in state['positions']}
    for item in data:
        sym = item['symbol']
        if sym in pos_map:
            item['pl'] = pos_map[sym]['pl']
            item['pl_pct'] = pos_map[sym]['pl_pct']
            item['shares'] = pos_map[sym]['shares']
        else:
            item['pl'] = None
            item['pl_pct'] = None
            item['shares'] = 0
    return jsonify({'watchlist': data})

@app.route('/api/watchlist/add', methods=['POST'])
def api_watchlist_add():
    body = request.get_json(silent=True) or {}
    sym = body.get('symbol', '').upper().strip()
    if not sym:
        return jsonify({'error': 'symbol required'}), 400
    wl = watchlist.add(sym)
    return jsonify({'watchlist': wl})

@app.route('/api/watchlist/remove', methods=['POST'])
def api_watchlist_remove():
    body = request.get_json(silent=True) or {}
    sym = body.get('symbol', '').upper().strip()
    if not sym:
        return jsonify({'error': 'symbol required'}), 400
    wl = watchlist.remove(sym)
    return jsonify({'watchlist': wl})

# ─── Paper Trade ──────────────────────────────────────────────────────────────

@app.route('/api/paper')
def api_paper():
    return jsonify(paper_trade.get_state())

@app.route('/api/paper/buy', methods=['POST'])
def api_paper_buy():
    body = request.get_json(silent=True) or {}
    sym = body.get('symbol', '').upper().strip()
    shares = body.get('shares', 0)
    if not sym or not shares:
        return jsonify({'error': 'symbol and shares required'}), 400
    ok, msg = paper_trade.buy(sym, shares)
    if ok:
        return jsonify({'ok': True, 'msg': msg})
    return jsonify({'ok': False, 'error': msg}), 400

@app.route('/api/paper/sell', methods=['POST'])
def api_paper_sell():
    body = request.get_json(silent=True) or {}
    sym = body.get('symbol', '').upper().strip()
    shares = body.get('shares', 0)
    if not sym or not shares:
        return jsonify({'error': 'symbol and shares required'}), 400
    ok, msg = paper_trade.sell(sym, shares)
    if ok:
        return jsonify({'ok': True, 'msg': msg})
    return jsonify({'ok': False, 'error': msg}), 400

@app.route('/api/paper/reset', methods=['POST'])
def api_paper_reset():
    body = request.get_json(silent=True) or {}
    bal = body.get('balance', None)
    paper_trade.reset(bal)
    return jsonify({'ok': True})

# ─── Sentiment ────────────────────────────────────────────────────────────────

@app.route('/api/sentiment')
def api_sentiment():
    sym = request.args.get('symbol', '').upper().strip()
    if not sym:
        return jsonify({'error': 'symbol required'}), 400
    result = sentiment.analyze(sym)
    _sentiment_cache[sym] = result
    return jsonify(result)

# ─── Auto Trade ───────────────────────────────────────────────────────────────

@app.route('/api/autotrade')
def api_autotrade():
    return jsonify(auto_trade.get_status())

@app.route('/api/autotrade/toggle', methods=['POST'])
def api_autotrade_toggle():
    enabled = auto_trade.toggle()
    return jsonify({'enabled': enabled})

# ─── Settings ─────────────────────────────────────────────────────────────────

@app.route('/api/settings')
def api_settings():
    cfg = config.load()
    safe = {
        'alpaca_paper_key': '***' if cfg.get('alpaca_paper_key') else '',
        'alpaca_paper_secret': '***' if cfg.get('alpaca_paper_secret') else '',
        'alpaca_live_key': '***' if cfg.get('alpaca_live_key') else '',
        'alpaca_live_secret': '***' if cfg.get('alpaca_live_secret') else '',
        'reddit_client_id': '***' if cfg.get('reddit_client_id') else '',
        'reddit_client_secret': '***' if cfg.get('reddit_client_secret') else '',
        'reddit_user_agent': cfg.get('reddit_user_agent', 'StopTrading/1.0'),
        'starting_balance': cfg.get('starting_balance', 10000.0),
        'buy_momentum_threshold': cfg.get('buy_momentum_threshold', 5.0),
        'sell_profit_target': cfg.get('sell_profit_target', 10.0),
        'stop_loss_pct': cfg.get('stop_loss_pct', 5.0),
        'live_mode': cfg.get('live_mode', False),
        'auto_trade_enabled': cfg.get('auto_trade_enabled', False)
    }
    return jsonify(safe)

@app.route('/api/settings', methods=['POST'])
def api_settings_save():
    body = request.get_json(silent=True) or {}
    cfg = config.load()
    updatable = [
        'alpaca_paper_key', 'alpaca_paper_secret',
        'alpaca_live_key', 'alpaca_live_secret',
        'reddit_client_id', 'reddit_client_secret', 'reddit_user_agent',
        'starting_balance', 'buy_momentum_threshold', 'sell_profit_target',
        'stop_loss_pct'
    ]
    for key in updatable:
        if key in body and body[key] != '***':
            cfg[key] = body[key]
    if 'live_mode' in body:
        cfg['live_mode'] = bool(body['live_mode'])
    config.save(cfg)
    return jsonify({'ok': True})

# ─── Main ─────────────────────────────────────────────────────────────────────

def open_browser():
    time.sleep(1.2)
    webbrowser.open('http://localhost:5175')

if __name__ == '__main__':
    auto_trade.init()
    t = threading.Thread(target=open_browser, daemon=True)
    t.start()
    print('StopTrading server starting at http://localhost:5175')
    app.run(host='0.0.0.0', port=5175, debug=False, use_reloader=False)
