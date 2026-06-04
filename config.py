import json
import os
import threading

from modules.io_safe import atomic_write_json

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')
_SAVE_LOCK = threading.Lock()


def _config_path():
    """Active config path. STOPTRADING_CONFIG env var lets a child process (e.g. a backtest
    sweep) point at an alternate config WITHOUT touching the live config.json the server reads."""
    return os.environ.get('STOPTRADING_CONFIG', CONFIG_FILE)

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
    path = _config_path()
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            config = dict(DEFAULTS)
            config.update(data)
            return _derive(config)
        except Exception:
            pass
    return _derive(dict(DEFAULTS))


def _derive(config):
    """Single master switch for going real-time.

    Flip `realtime_subscribed` true the day you activate a paid full-SIP feed (Alpaca Algo
    Trader Plus ~$99/mo, Polygon Developer ~$79/mo, or an IBKR data bundle). The FeedManager
    already auto-detects the entitlement and upgrades to REAL_TIME on its own; this just ties
    the two model-integrity flags to that same decision so it's ONE flip, not three:
      • train_on_live          → live outcomes are now delay-clean, so let them train the model
      • block_trades_on_delayed → refuse to trade if the feed ever degrades (live money safety)
    Explicit per-flag values still apply while realtime_subscribed is false."""
    if config.get('realtime_subscribed'):
        config['train_on_live'] = True
        config['block_trades_on_delayed'] = True
    return config

def save_config(updates):
    # Serialize the read-modify-write so concurrent POST handlers can't lose updates.
    with _SAVE_LOCK:
        config = load_config()
        config.update(updates)
        atomic_write_json(CONFIG_FILE, config)
    return config
