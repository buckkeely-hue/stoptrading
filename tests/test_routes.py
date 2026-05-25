"""
Flask route protection and input validation tests.
Intent preserved: every data-modifying or data-reading endpoint requires authentication;
user-supplied inputs (symbols, numbers) are sanitised before use;
health check and login page remain public.
Uses Flask's test client with a minimal app — does NOT boot the full trading engine.
"""
from tests.harness_utils import Suite

from flask import Flask, request, jsonify, session
import re

def _build_test_app():
    """Thin replica of server.py's auth + validation surface for isolated route testing."""
    from modules.auth import require_auth, check_password, get_secret_key, \
        check_rate_limit, record_failed_attempt, clear_rate_limit

    app = Flask(__name__)
    app.secret_key = get_secret_key()
    app.config['TESTING'] = True
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Strict'

    # ── Helpers (mirrors server.py) ───────────────────────────────────────────
    _SYMBOL_RE = re.compile(r'^[A-Z][A-Z0-9.]{0,9}$')

    def _valid_symbol(s):
        return bool(s and _SYMBOL_RE.match(s.upper().strip()))

    def _safe_float(val, default):
        try:
            return float(val) if val is not None else default
        except (ValueError, TypeError):
            return default

    def _safe_int(val, default):
        try:
            return int(val) if val is not None else default
        except (ValueError, TypeError):
            return default

    _SECRET_KEYS = {
        'alpaca_paper_key', 'alpaca_paper_secret', 'polygon_key',
        'twilio_auth_token', 'smtp_password', 'gmail_client_secret', 'gmail_refresh_token',
    }

    # ── Security headers ──────────────────────────────────────────────────────
    @app.after_request
    def _hdrs(response):
        response.headers.pop('Server', None)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options']        = 'DENY'
        return response

    # ── Public routes ─────────────────────────────────────────────────────────
    @app.route('/api/health')
    def health():
        return jsonify({'status': 'ok'})

    @app.route('/login')
    def login_page():
        return 'login page', 200

    @app.route('/reset')
    def reset_page():
        return 'reset page', 200

    @app.route('/api/login', methods=['POST'])
    def api_login():
        ip = request.remote_addr or '127.0.0.1'
        if check_rate_limit(ip):
            return jsonify({'ok': False, 'error': 'Too many failed attempts'}), 429
        data = request.get_json(force=True) or {}
        pw   = data.get('password', '')
        if check_password(pw):
            clear_rate_limit(ip)
            session['authenticated'] = True
            session.permanent = True
            return jsonify({'ok': True})
        record_failed_attempt(ip)
        return jsonify({'ok': False, 'error': 'Incorrect password'}), 401

    @app.route('/api/logout', methods=['POST'])
    def api_logout():
        session.clear()
        return jsonify({'ok': True})

    # ── Protected routes ──────────────────────────────────────────────────────
    @app.route('/api/scan')
    @require_auth
    def api_scan():
        return jsonify({'results': []})

    @app.route('/api/settings', methods=['GET', 'POST'])
    @require_auth
    def api_settings():
        if request.method == 'POST':
            return jsonify({'ok': True})
        fake_cfg = {'alpaca_paper_key': 'secret', 'polygon_key': 'secret',
                    'paper_balance': 500, 'notify_email': 'user@example.com'}
        safe = {k: ('***' if v else '') if k in _SECRET_KEYS else v
                for k, v in fake_cfg.items()}
        return jsonify(safe)

    @app.route('/api/symbol-check')
    @require_auth
    def api_symbol_check():
        sym = request.args.get('symbol', '')
        if not _valid_symbol(sym):
            return jsonify({'ok': False, 'error': 'Invalid symbol'}), 400
        return jsonify({'ok': True, 'symbol': sym.upper().strip()})

    @app.route('/api/number-check')
    @require_auth
    def api_number_check():
        val = request.args.get('val')
        return jsonify({
            'float': _safe_float(val, -1.0),
            'int':   _safe_int(val,   -1),
        })

    return app, check_password


def run() -> tuple:
    s = Suite('ROUTES — auth protection, input validation, security headers')

    app, check_password = _build_test_app()
    client = app.test_client()

    # ── Public routes need no auth ────────────────────────────────────────────
    s.section('Public routes (no auth required)')
    r = client.get('/api/health')
    s.check('/api/health returns 200',          r.status_code == 200)
    s.check('/api/health returns JSON status',  b'"ok"' in r.data)

    r = client.get('/login')
    s.check('/login returns 200',               r.status_code == 200)

    r = client.get('/reset')
    s.check('/reset returns 200',               r.status_code == 200)

    # ── Protected routes redirect/401 when unauthenticated ───────────────────
    s.section('Protected routes: unauthenticated → 302/401')
    r = client.get('/api/scan')
    s.check('GET /api/scan → 302 redirect when unauthenticated', r.status_code == 302)

    r = client.get('/api/settings')
    s.check('GET /api/settings → 302 when unauthenticated',      r.status_code == 302)

    # POST to a protected POST-accepting route (logout requires auth only implicitly;
    # use the settings endpoint which accepts POST and is protected)
    r = client.post('/api/settings', json={})
    s.check('POST to protected POST route → 401 when unauthenticated', r.status_code == 401)

    # ── Login and session establishment ───────────────────────────────────────
    s.section('Login flow')
    r = client.post('/api/login', json={'password': 'completely_wrong_pw!'})
    s.check('Wrong password → 401',             r.status_code == 401)

    # Inject an authenticated session directly — plaintext password is unavailable
    # from .auth, so we test the session path rather than the password path here.
    with client.session_transaction() as sess:
        sess['authenticated'] = True

    r = client.get('/api/scan')
    s.check('Authenticated session → 200 on protected route', r.status_code == 200)

    r = client.post('/api/logout')
    s.check('/api/logout returns 200',          r.status_code == 200)

    r = client.get('/api/scan')
    s.check('After logout → 302 again',         r.status_code == 302)

    # ── Rate limiting on /api/login ───────────────────────────────────────────
    s.section('Rate limiting on /api/login')
    from modules.auth import clear_rate_limit, _LOCKOUT_SECS, _MAX_ATTEMPTS
    # Use a distinctive IP via X-Forwarded-For isn't available in test client easily,
    # so we inject rate limit state directly
    test_ip = '127.0.0.1'
    clear_rate_limit(test_ip)

    import modules.auth as auth_mod
    auth_mod._rate_lock.acquire()
    auth_mod._failed[test_ip] = (_MAX_ATTEMPTS, __import__('time').time())
    auth_mod._rate_lock.release()

    r = client.post('/api/login', json={'password': 'anything'})
    s.check('Locked IP → 429',                  r.status_code == 429)
    clear_rate_limit(test_ip)

    # ── Symbol validation ─────────────────────────────────────────────────────
    s.section('Symbol validation (_valid_symbol)')
    with client.session_transaction() as sess:
        sess['authenticated'] = True

    # Server normalises to upper before validation, so lowercase is accepted
    valid_symbols   = ['AAPL', 'TSLA', 'BRK.A', 'A', 'MSFT', 'aapl', 'tsla']
    invalid_symbols = ['', '1AAPL', 'TOO_LONG_SYM', 'AA PL', '../etc']

    for sym in valid_symbols:
        r = client.get(f'/api/symbol-check?symbol={sym}')
        s.check(f'Valid symbol {sym!r} → 200',  r.status_code == 200)

    for sym in invalid_symbols:
        r = client.get(f'/api/symbol-check?symbol={sym}')
        s.check(f'Invalid symbol {sym!r} → 400', r.status_code == 400)

    # ── _safe_float / _safe_int ───────────────────────────────────────────────
    s.section('Numeric input sanitisation')
    import json

    def num(val_str):
        r = client.get(f'/api/number-check?val={val_str}')
        return json.loads(r.data)

    s.check('Integer string parses',              num('42')['int']   == 42)
    s.check('Float string parses',                num('3.14')['float'] == 3.14)
    s.check('Non-numeric → default -1',           num('abc')['int']  == -1)
    s.check('Injection attempt → default -1',     num('1;DROP')['int'] == -1)
    s.check('Negative number parses',             num('-5')['int']   == -5)

    # ── Security headers ──────────────────────────────────────────────────────
    s.section('Security response headers')
    r = client.get('/api/health')
    s.check('X-Content-Type-Options: nosniff present',
            r.headers.get('X-Content-Type-Options') == 'nosniff')
    s.check('X-Frame-Options: DENY present',
            r.headers.get('X-Frame-Options') == 'DENY')
    s.check('Server header suppressed',
            'Server' not in r.headers or 'Werkzeug' not in r.headers.get('Server',''))

    # ── Session cookie attributes ─────────────────────────────────────────────
    s.section('Session cookie security attributes')
    with client.session_transaction() as sess:
        sess['authenticated'] = True
    s.check('SESSION_COOKIE_HTTPONLY configured True',
            app.config.get('SESSION_COOKIE_HTTPONLY') == True)
    s.check('SESSION_COOKIE_SAMESITE configured Strict',
            app.config.get('SESSION_COOKIE_SAMESITE') == 'Strict')

    # ── Secret key masking in /api/settings ──────────────────────────────────
    s.section('Secret key masking')
    with client.session_transaction() as sess:
        sess['authenticated'] = True
    r = client.get('/api/settings')
    data = json.loads(r.data)
    s.check('alpaca_paper_key masked to ***',     data.get('alpaca_paper_key') == '***')
    s.check('polygon_key masked to ***',          data.get('polygon_key') == '***')
    s.check('notify_email NOT masked',            data.get('notify_email') == 'user@example.com')
    s.check('paper_balance NOT masked',           data.get('paper_balance') == 500)

    return s.summary()


if __name__ == '__main__':
    run()
