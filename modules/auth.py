"""
Session-based authentication for StopTrading.
Generates a random password on first run and prints it to the console.
Credentials stored in .auth (outside web root, chmod 600).
OTP delivery: SMS (Twilio), ntfy push, or SMTP email — no console tokens.
"""
import os
import json
import hashlib
import secrets
import smtplib
import ssl
import threading
import time
import urllib.request
import urllib.parse
import base64
from functools import wraps
from flask import session, request, jsonify, redirect

# ── Login rate limiting ───────────────────────────────────────────────────────
_rate_lock     = threading.Lock()
_failed        = {}   # ip -> (count, first_fail_ts)
_MAX_ATTEMPTS  = 10
_LOCKOUT_SECS  = 900  # 15 minutes


def check_rate_limit(ip: str) -> bool:
    """Return True if this IP is locked out."""
    with _rate_lock:
        entry = _failed.get(ip)
        if not entry:
            return False
        count, first_ts = entry
        if time.time() - first_ts > _LOCKOUT_SECS:
            del _failed[ip]
            return False
        return count >= _MAX_ATTEMPTS


def record_failed_attempt(ip: str):
    with _rate_lock:
        entry = _failed.get(ip, (0, time.time()))
        _failed[ip] = (entry[0] + 1, entry[1])


def clear_rate_limit(ip: str):
    with _rate_lock:
        _failed.pop(ip, None)

AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.auth')

_creds = None


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 200_000).hex()


def _load_or_create() -> dict:
    global _creds
    if _creds:
        return _creds
    if os.path.exists(AUTH_FILE):
        with open(AUTH_FILE) as f:
            _creds = json.load(f)
        # Backfill recovery_code if missing from older .auth files
        if 'recovery_code' not in _creds:
            _creds['recovery_code'] = secrets.token_urlsafe(10)
            _write()
            print(f'\n  [StopTrading] Emergency recovery code: {_creds["recovery_code"]}')
            print('  (shown once — saved to .auth)\n')
        return _creds
    # First run — generate credentials
    password      = secrets.token_urlsafe(14)
    salt          = secrets.token_hex(16)
    secret_key    = secrets.token_hex(32)
    recovery_code = secrets.token_urlsafe(10)
    _creds = {'salt': salt, 'hash': _hash(password, salt),
              'secret_key': secret_key, 'recovery_code': recovery_code}
    _write()
    print('\n' + '=' * 62)
    print('  StopTrading — first-run login credentials')
    print(f'  Password      : {password}')
    print(f'  Recovery code : {recovery_code}')
    print(f'  URL           : http://localhost:5175/login')
    print(f'                  https://trade.buckkeely.com/login')
    print('  (credentials saved to .auth — keep this file private)')
    print('=' * 62 + '\n')
    return _creds


def _write():
    with open(AUTH_FILE, 'w') as f:
        json.dump(_creds, f)
    os.chmod(AUTH_FILE, 0o600)


def get_secret_key() -> str:
    return _load_or_create().get('secret_key', secrets.token_hex(32))


def check_password(password: str) -> bool:
    creds = _load_or_create()
    return secrets.compare_digest(_hash(password, creds['salt']), creds['hash'])


def set_password(new_password: str):
    """Replace the stored password hash. Invalidates any active tokens."""
    global _creds
    creds = _load_or_create()
    creds['salt'] = secrets.token_hex(16)
    creds['hash'] = _hash(new_password, creds['salt'])
    creds.pop('reset_token', None)
    creds.pop('reset_expires', None)
    creds.pop('change_token', None)
    creds.pop('change_expires', None)
    _write()


def change_password(old_password: str, new_password: str) -> bool:
    """Verify old password then set a new one."""
    if not check_password(old_password):
        return False
    set_password(new_password)
    return True


# ── OTP delivery ──────────────────────────────────────────────────────────────

def _mask_phone(phone: str) -> str:
    clean = phone.replace(' ', '').replace('-', '')
    return '***-***-' + clean[-4:] if len(clean) >= 4 else '***'


def _mask_email(email: str) -> str:
    at = email.find('@')
    if at > 2:
        return email[:2] + '***' + email[at:]
    return email[:1] + '***' + email[at:] if at >= 0 else '***'


def _send_otp(code: str, config: dict) -> tuple:
    """
    Deliver OTP via ALL configured channels simultaneously.
    Returns (ok: bool, channel: str, masked_dest: str) for the first success,
    but always attempts every configured channel.
    """
    msg = f'Your StopTrading verification code: {code}  (expires 15 min)'
    results = []

    # ── Twilio SMS ────────────────────────────────────────────────────────────
    sid = (config.get('twilio_account_sid') or '').strip()
    tok = (config.get('twilio_auth_token') or '').strip()
    frm = (config.get('twilio_from') or '').strip()
    to  = (config.get('notify_phone') or '').strip()
    if sid and tok and frm and to:
        try:
            url  = f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json'
            body = urllib.parse.urlencode({'From': frm, 'To': to, 'Body': msg})
            cred = base64.b64encode(f'{sid}:{tok}'.encode()).decode()
            req  = urllib.request.Request(
                url, data=body.encode(),
                headers={'Authorization': f'Basic {cred}',
                         'Content-Type': 'application/x-www-form-urlencoded'})
            urllib.request.urlopen(req, timeout=10)
            results.append((True, 'sms', _mask_phone(to)))
        except Exception:
            pass

    # ── ntfy.sh push ──────────────────────────────────────────────────────────
    topic = (config.get('ntfy_topic') or '').strip()
    if topic:
        try:
            req = urllib.request.Request(
                f'https://ntfy.sh/{topic}',
                data=msg.encode(),
                headers={'Title': 'StopTrading Auth', 'Priority': 'high'},
                method='POST')
            urllib.request.urlopen(req, timeout=8)
            results.append((True, 'ntfy', topic))
        except Exception:
            pass

    # ── Gmail API (OAuth2) — always attempt when configured ───────────────────
    gmail_client_id     = (config.get('gmail_client_id') or '').strip()
    gmail_client_secret = (config.get('gmail_client_secret') or '').strip()
    gmail_refresh_token = (config.get('gmail_refresh_token') or '').strip()
    gmail_token_uri     = (config.get('gmail_token_uri') or 'https://oauth2.googleapis.com/token').strip()
    notify_email        = (config.get('notify_email') or '').strip()
    gmail_from          = (config.get('smtp_user') or notify_email).strip()
    if gmail_client_id and gmail_client_secret and gmail_refresh_token and notify_email:
        try:
            token_data = urllib.parse.urlencode({
                'client_id':     gmail_client_id,
                'client_secret': gmail_client_secret,
                'refresh_token': gmail_refresh_token,
                'grant_type':    'refresh_token',
            }).encode()
            token_req = urllib.request.Request(
                gmail_token_uri, data=token_data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'})
            token_resp = json.loads(urllib.request.urlopen(token_req, timeout=10).read())
            access_token = token_resp.get('access_token', '')
            if access_token:
                subject  = 'StopTrading — Verification Code'
                body_txt = (
                    f'Your StopTrading verification code is:\n\n'
                    f'    {code}\n\n'
                    f'This code expires in 15 minutes.\n'
                    f'If you did not request this, ignore this email.'
                )
                raw_msg = (
                    f'From: {gmail_from}\r\n'
                    f'To: {notify_email}\r\n'
                    f'Subject: {subject}\r\n'
                    f'\r\n'
                    f'{body_txt}'
                )
                encoded = base64.urlsafe_b64encode(raw_msg.encode()).decode().rstrip('=')
                send_req = urllib.request.Request(
                    'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
                    data=json.dumps({'raw': encoded}).encode(),
                    headers={'Authorization': f'Bearer {access_token}',
                             'Content-Type': 'application/json'},
                    method='POST')
                urllib.request.urlopen(send_req, timeout=10)
                results.append((True, 'email', _mask_email(notify_email)))
        except Exception:
            pass

    # ── SMTP email (fallback) ─────────────────────────────────────────────────
    smtp_host    = (config.get('smtp_host') or 'smtp.gmail.com').strip()
    smtp_port    = int(config.get('smtp_port') or 587)
    smtp_user    = (config.get('smtp_user') or '').strip()
    smtp_pass    = (config.get('smtp_password') or '').strip()
    if smtp_user and smtp_pass and notify_email:
        try:
            ctx = ssl.create_default_context()
            subject  = 'StopTrading — Verification Code'
            body_txt = (
                f'Your StopTrading verification code is:\n\n'
                f'    {code}\n\n'
                f'This code expires in 15 minutes.\n'
                f'If you did not request this, ignore this email.'
            )
            raw = (
                f'From: {smtp_user}\r\n'
                f'To: {notify_email}\r\n'
                f'Subject: {subject}\r\n'
                f'\r\n'
                f'{body_txt}'
            )
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
                s.starttls(context=ctx)
                s.login(smtp_user, smtp_pass)
                s.sendmail(smtp_user, notify_email, raw)
            results.append((True, 'smtp', _mask_email(notify_email)))
        except Exception:
            pass

    if results:
        # Return the best channel (sms > email > ntfy) while having fired all of them
        for preferred in ('sms', 'email', 'smtp', 'ntfy'):
            for r in results:
                if r[1] == preferred:
                    return r
        return results[0]
    return False, 'none', ''


def emergency_reset(recovery_code: str, new_password: str) -> bool:
    """Set a new password using the persistent recovery code (no OTP required)."""
    creds = _load_or_create()
    stored = creds.get('recovery_code', '')
    if not stored or not secrets.compare_digest(str(recovery_code).strip(), str(stored)):
        return False
    set_password(new_password)
    # Rotate the recovery code after use
    global _creds
    _creds['recovery_code'] = secrets.token_urlsafe(10)
    _write()
    print(f'\n  [StopTrading] New recovery code: {_creds["recovery_code"]}\n')
    return True


def get_recovery_hint() -> str:
    """Return the last 4 chars of the recovery code as a hint."""
    creds = _load_or_create()
    code  = creds.get('recovery_code', '')
    return '...' + code[-4:] if len(code) >= 4 else '????'


def _six_digit_code() -> str:
    return str(secrets.randbelow(900000) + 100000)


# ── Reset token (unauthenticated — forgot password) ───────────────────────────

def generate_reset_token(config: dict = None) -> tuple:
    """
    Generate a 6-digit OTP and deliver via SMS/ntfy/email.
    Returns (ok: bool, channel: str, masked_dest: str).
    """
    global _creds
    creds = _load_or_create()
    code = _six_digit_code()
    creds['reset_token']   = code
    creds['reset_expires'] = time.time() + 900
    _write()

    if config:
        return _send_otp(code, config)

    # No channel configured — fallback console only
    print('\n' + '=' * 62)
    print('  StopTrading — Password Reset Code')
    print(f'  Code    : {code}')
    print('  Expires : 15 minutes')
    print('=' * 62 + '\n')
    return True, 'console', 'server log'


def consume_reset_token(token: str, new_password: str) -> bool:
    """Validate the reset token and atomically set the new password."""
    global _creds
    creds = _load_or_create()
    stored  = creds.get('reset_token', '')
    expires = creds.get('reset_expires', 0)
    if not stored or time.time() > expires:
        return False
    if not secrets.compare_digest(str(token), str(stored)):
        return False
    set_password(new_password)
    return True


# ── Change-password token (authenticated — wants to change) ───────────────────

def generate_change_token(config: dict = None) -> tuple:
    """
    Generate a 6-digit OTP for change-password confirmation and deliver it.
    Returns (ok: bool, channel: str, masked_dest: str).
    """
    global _creds
    creds = _load_or_create()
    code = _six_digit_code()
    creds['change_token']   = code
    creds['change_expires'] = time.time() + 900
    _write()

    if config:
        return _send_otp(code, config)

    print('\n' + '=' * 62)
    print('  StopTrading — Change Password Code')
    print(f'  Code    : {code}')
    print('  Expires : 15 minutes')
    print('=' * 62 + '\n')
    return True, 'console', 'server log'


def consume_change_token(token: str, new_password: str) -> bool:
    """Validate the change token and atomically set the new password."""
    global _creds
    creds = _load_or_create()
    stored  = creds.get('change_token', '')
    expires = creds.get('change_expires', 0)
    if not stored or time.time() > expires:
        return False
    if not secrets.compare_digest(str(token), str(stored)):
        return False
    set_password(new_password)
    return True


def require_auth(f):
    """Decorator: returns 401 JSON (or redirects GET requests to /login) if not logged in."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            if request.method == 'GET':
                return redirect('/login')
            return jsonify({'error': 'Unauthorized — please log in at /login'}), 401
        return f(*args, **kwargs)
    return decorated
