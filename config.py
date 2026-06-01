import json
import os
import threading

from modules.io_safe import atomic_write_json

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')
_SAVE_LOCK = threading.Lock()

DEFAULTS = {
    'alpaca_paper_key': '',
    'alpaca_paper_secret': '',
    'alpaca_live_key': '',
    'alpaca_live_secret': '',
    'reddit_client_id': '',
    'reddit_secret': '',
    'finnhub_key': '',
    'polygon_key': '',
    'benzinga_key': '',
    'use_ibkr_feed': False,
    'ibkr_host': '127.0.0.1',
    'ibkr_port': 7497,
    'ibkr_client_id': 10,
    'paper_balance': 10000,
    'auto_trade_enabled': False,
    'buy_trigger': 3.0,
    'sell_target': 10.0,
    'stop_loss': 5.0,
    'harvest_trigger_pct': 10.0,
    'exit_trigger_pct': 5.0,
    'tx_cost_pct': 0.5,
    'daily_spend_limit': 100.0,
    'per_trade_capital': 50.0,
    'siphon_rate': 50.0,
    'auto_siphon': True,
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
            config = dict(DEFAULTS)
            config.update(data)
            return config
        except Exception:
            pass
    return dict(DEFAULTS)

def save_config(updates):
    # Serialize the read-modify-write so concurrent POST handlers can't lose updates.
    with _SAVE_LOCK:
        config = load_config()
        config.update(updates)
        atomic_write_json(CONFIG_FILE, config)
    return config
