import json
import os

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')

DEFAULTS = {
    'alpaca_paper_key': '',
    'alpaca_paper_secret': '',
    'alpaca_live_key': '',
    'alpaca_live_secret': '',
    'reddit_client_id': '',
    'reddit_client_secret': '',
    'reddit_user_agent': 'StopTrading/1.0',
    'starting_balance': 10000.0,
    'auto_trade_enabled': False,
    'buy_momentum_threshold': 5.0,
    'sell_profit_target': 10.0,
    'stop_loss_pct': 5.0,
    'live_mode': False,
    'watchlist': []
}

def load():
    if not os.path.exists(CONFIG_FILE):
        save(DEFAULTS.copy())
        return DEFAULTS.copy()
    try:
        with open(CONFIG_FILE, 'r') as f:
            data = json.load(f)
        merged = DEFAULTS.copy()
        merged.update(data)
        return merged
    except Exception:
        return DEFAULTS.copy()

def save(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)

def get(key, default=None):
    return load().get(key, default)

def set_key(key, value):
    cfg = load()
    cfg[key] = value
    save(cfg)
