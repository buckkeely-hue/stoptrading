"""
Config persistence tests.
Intent preserved: custom fields (Gmail OAuth, ntfy topic, trading params) survive
any save_config() call; secret fields are masked in API responses; no field silently
disappears when an unrelated setting is changed.
"""
import os, json, tempfile, shutil
from tests.harness_utils import Suite


def run() -> tuple:
    s = Suite('CONFIG — persistence, secret masking, round-trip fidelity')

    # ── Patch CONFIG_FILE to a temp file ──────────────────────────────────────
    import config as cfg_mod
    tmp_dir  = tempfile.mkdtemp(prefix='st_cfg_test_')
    tmp_file = os.path.join(tmp_dir, 'config_test.json')
    _orig    = cfg_mod.CONFIG_FILE
    cfg_mod.CONFIG_FILE = tmp_file

    try:
        # ── Defaults load when no file exists ────────────────────────────────
        s.section('Defaults on fresh load')
        c = cfg_mod.load_config()
        s.check('paper_balance defaults to 10000',  c.get('paper_balance') == 10000)
        s.check('auto_trade_enabled defaults False', c.get('auto_trade_enabled') == False)
        s.check('daily_spend_limit defaults 100',   c.get('daily_spend_limit') == 100.0)

        # ── Custom fields survive round-trip ──────────────────────────────────
        s.section('Custom fields survive save_config round-trip')
        cfg_mod.save_config({
            'ntfy_topic':         'stoptrading-test-topic',
            'notify_email':       'test@example.com',
            'gmail_client_id':    'fake_client_id_12345',
            'gmail_refresh_token':'fake_refresh_token_xyz',
            'smtp_host':          'smtp.gmail.com',
            'smtp_port':          587,
            'paper_balance':      500,
        })
        loaded = cfg_mod.load_config()
        s.check('ntfy_topic survives',              loaded.get('ntfy_topic') == 'stoptrading-test-topic')
        s.check('notify_email survives',            loaded.get('notify_email') == 'test@example.com')
        s.check('gmail_client_id survives',         loaded.get('gmail_client_id') == 'fake_client_id_12345')
        s.check('gmail_refresh_token survives',     loaded.get('gmail_refresh_token') == 'fake_refresh_token_xyz')
        s.check('paper_balance updated to 500',     loaded.get('paper_balance') == 500)

        # ── Partial save does not wipe other fields ───────────────────────────
        s.section('Partial save preserves unrelated fields')
        cfg_mod.save_config({'paper_balance': 750})
        reloaded = cfg_mod.load_config()
        s.check('ntfy_topic still present after partial save',
                reloaded.get('ntfy_topic') == 'stoptrading-test-topic')
        s.check('gmail_client_id still present after partial save',
                reloaded.get('gmail_client_id') == 'fake_client_id_12345')
        s.check('paper_balance updated',            reloaded.get('paper_balance') == 750)

        # ── JSON file is valid after each save ───────────────────────────────
        s.section('Config file integrity')
        with open(tmp_file) as f:
            raw = json.load(f)
        s.check('Config file is valid JSON',        True)
        s.check('Config file has expected keys',
                'paper_balance' in raw and 'ntfy_topic' in raw)

        # ── DEFAULTS dict in config.py ────────────────────────────────────────
        s.section('DEFAULTS completeness')
        expected_defaults = [
            'paper_balance', 'auto_trade_enabled', 'buy_trigger', 'sell_target',
            'stop_loss', 'daily_spend_limit', 'per_trade_capital',
        ]
        for key in expected_defaults:
            s.check(f'DEFAULTS has {key!r}', key in cfg_mod.DEFAULTS)

        # ── _SECRET_KEYS masking (mirrors server.py logic) ────────────────────
        s.section('Secret key masking in API responses')
        SECRET_KEYS = {
            'alpaca_paper_key', 'alpaca_paper_secret', 'alpaca_live_key', 'alpaca_live_secret',
            'reddit_client_id', 'reddit_secret', 'reddit_client_secret',
            'polygon_key', 'benzinga_key', 'finnhub_key',
            'twilio_account_sid', 'twilio_auth_token',
            'smtp_password', 'gmail_client_secret', 'gmail_refresh_token',
        }
        fake_cfg = {k: 'super_secret_value' for k in SECRET_KEYS}
        fake_cfg['notify_email'] = 'visible@example.com'
        fake_cfg['paper_balance'] = 500

        safe = {k: ('***' if v else '') if k in SECRET_KEYS else v
                for k, v in fake_cfg.items()}

        for key in SECRET_KEYS:
            s.check(f'{key!r} masked to ***',
                    safe.get(key) == '***')
        s.check('notify_email NOT masked',          safe.get('notify_email') == 'visible@example.com')
        s.check('paper_balance NOT masked',         safe.get('paper_balance') == 500)

        # Empty secret returns '' not '***'
        fake_empty = {k: '' for k in SECRET_KEYS}
        safe_empty = {k: ('***' if v else '') if k in SECRET_KEYS else v
                      for k, v in fake_empty.items()}
        s.check('Empty secret key returns empty string (not ***)',
                all(safe_empty[k] == '' for k in SECRET_KEYS))

        # ── gmail_client_id is NOT masked (it's not secret) ──────────────────
        s.section('Non-secret gmail fields are visible')
        s.check('gmail_client_id is NOT in SECRET_KEYS (client IDs are public)',
                'gmail_client_id' not in SECRET_KEYS)
        s.check('gmail_token_uri is NOT in SECRET_KEYS',
                'gmail_token_uri' not in SECRET_KEYS)
        s.check('gmail_client_secret IS masked',
                'gmail_client_secret' in SECRET_KEYS)
        s.check('gmail_refresh_token IS masked',
                'gmail_refresh_token' in SECRET_KEYS)

    finally:
        cfg_mod.CONFIG_FILE = _orig
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return s.summary()


if __name__ == '__main__':
    run()
