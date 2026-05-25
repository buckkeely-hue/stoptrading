"""
Notification dispatcher — SMS via Twilio REST API, push via ntfy.sh.
No external dependencies: uses only stdlib urllib for both channels.
All sends are non-blocking (daemon thread). All sends are logged.
"""
import json
import os
import threading
import urllib.request
import urllib.parse
import base64
from datetime import datetime

NOTIFY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'notifications.json')


class Notifier:
    def __init__(self, config: dict):
        self.config = config
        self._lock  = threading.Lock()
        self._log: list = []
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self):
        try:
            if os.path.exists(NOTIFY_FILE):
                with open(NOTIFY_FILE) as f:
                    self._log = json.load(f).get('log', [])
        except Exception:
            pass

    def _append(self, message: str, channel: str, ok: bool, error: str = ''):
        entry = {
            'time':    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'message': message[:200],
            'channel': channel,
            'ok':      ok,
            'error':   error,
        }
        with self._lock:
            self._log.append(entry)
            try:
                with open(NOTIFY_FILE, 'w') as f:
                    json.dump({'log': self._log[-300:]}, f, indent=2)
            except Exception:
                pass

    # ── Public API ────────────────────────────────────────────────────────────

    def send(self, message: str, level: str = 'info'):
        """Fire-and-forget notification. Returns immediately."""
        threading.Thread(target=self._dispatch, args=(message, level),
                         daemon=True, name='notify').start()

    def is_configured(self) -> bool:
        cfg = self.config
        twilio_ok = bool(cfg.get('twilio_account_sid') and
                         cfg.get('twilio_auth_token') and
                         cfg.get('twilio_from') and
                         cfg.get('notify_phone'))
        ntfy_ok   = bool(cfg.get('ntfy_topic'))
        return twilio_ok or ntfy_ok

    def get_log(self) -> list:
        with self._lock:
            return list(reversed(self._log[-50:]))

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def _dispatch(self, message: str, level: str):
        cfg   = self.config
        sent  = False

        # ── Twilio SMS (REST API, no SDK needed) ──────────────────────────────
        sid  = cfg.get('twilio_account_sid', '').strip()
        tok  = cfg.get('twilio_auth_token', '').strip()
        frm  = cfg.get('twilio_from', '').strip()
        to   = cfg.get('notify_phone', '').strip()
        if sid and tok and frm and to:
            try:
                url  = 'https://api.twilio.com/2010-04-01/Accounts/{}/Messages.json'.format(sid)
                body = urllib.parse.urlencode({'From': frm, 'To': to, 'Body': message[:1600]})
                cred = base64.b64encode('{}:{}'.format(sid, tok).encode()).decode()
                req  = urllib.request.Request(
                    url, data=body.encode(),
                    headers={'Authorization': 'Basic ' + cred,
                             'Content-Type': 'application/x-www-form-urlencoded'})
                urllib.request.urlopen(req, timeout=10)
                self._append(message, 'twilio-sms', True)
                sent = True
            except Exception as e:
                self._append(message, 'twilio-sms', False, str(e))

        # ── ntfy.sh push (free, no account) ───────────────────────────────────
        topic = cfg.get('ntfy_topic', '').strip()
        if topic:
            try:
                priority = 'high' if level == 'alert' else 'default'
                req = urllib.request.Request(
                    'https://ntfy.sh/' + topic,
                    data=message.encode(),
                    headers={
                        'Title':    'StopTrading',
                        'Priority': priority,
                        'Tags':     'chart_with_upwards_trend',
                    },
                    method='POST')
                urllib.request.urlopen(req, timeout=8)
                self._append(message, 'ntfy', True)
                sent = True
            except Exception as e:
                self._append(message, 'ntfy', False, str(e))

        if not sent:
            self._append(message, 'none', False, 'no channel configured')


# ── Message builders ──────────────────────────────────────────────────────────

def msg_buy(symbol, shares, price, cost, score):
    return '📈 BUY: {} × {} @ ${:.4f} | ${:.0f} deployed | score {:.0f}'.format(
        symbol, shares, price, cost, score)

def msg_harvest(symbol, frac_pct, net_gain, profit, harvests_done):
    return '💰 HARVEST #{}: {} — sold {:.0f}% at {:.1f}% gain | +${:.2f}'.format(
        harvests_done, symbol, frac_pct, net_gain, profit)

def msg_exit(action, symbol, reason, pnl):
    icon = '✅' if pnl >= 0 else '🔴'
    return '{} {}: {} | {}'.format(icon, action, symbol, reason if reason else 'P&L ${:+.2f}'.format(pnl))

def msg_halt(reason):
    return '🚨 HALT: {}'.format(reason)

def msg_evasive(symbol, shares_sold, price, pnl_protected):
    return '⚡ EVASIVE: {} — cascade sell detected, shed {} shares @ ${:.4f} | protected ~${:.2f}'.format(
        symbol, shares_sold, price, pnl_protected)

def msg_eod(total_pnl, trades, wins, losses):
    wr = wins / max(wins + losses, 1) * 100
    icon = '📈' if total_pnl >= 0 else '📉'
    return '{} EOD: P&L ${:+.2f} | {} trades | {:.0f}% win rate'.format(
        icon, total_pnl, trades, wr)
