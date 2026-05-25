"""
Auth module tests — password hashing, OTP lifecycle, rate limiting.
Intent preserved: only the correct OTP (sent to phone/email) can change a password;
brute-force is blocked; tokens expire; production .auth is never touched.
"""
import time
from tests.harness_utils import Suite, TempAuth


def run() -> tuple:
    s = Suite('AUTH — password, OTP, rate limiting')

    # ── Password hashing ──────────────────────────────────────────────────────
    s.section('Password hashing')
    with TempAuth() as a:
        a.set_password('Hunter2_Test!')
        s.check('Correct password accepted',        a.check_password('Hunter2_Test!'))
        s.check('Wrong password rejected',          not a.check_password('wrongpassword'))
        s.check('Empty string rejected',            not a.check_password(''))
        s.check('Case-sensitive',                   not a.check_password('hunter2_test!'))

        # Hash must not be stored in plaintext
        import json, os
        raw = json.load(open(a.AUTH_FILE))
        s.check('Plaintext password absent from .auth file',
                'Hunter2_Test!' not in json.dumps(raw))
        s.check('.auth has salt + hash fields',     bool(raw.get('salt') and raw.get('hash')))
        s.check('.auth mode is 0o600',
                oct(os.stat(a.AUTH_FILE).st_mode)[-3:] == '600')

    # ── change_password (old-pw gate) ─────────────────────────────────────────
    s.section('change_password (old-password gate)')
    with TempAuth() as a:
        a.set_password('OldPass_1!')
        s.check('Wrong old-pw → False',             not a.change_password('WrongOld!', 'NewPass_2!'))
        s.check('Original password still valid after failed change',
                a.check_password('OldPass_1!'))
        s.check('Correct old-pw → True',             a.change_password('OldPass_1!', 'NewPass_2!'))
        s.check('New password now valid',            a.check_password('NewPass_2!'))
        s.check('Old password invalidated',          not a.check_password('OldPass_1!'))

    # ── Reset OTP lifecycle ───────────────────────────────────────────────────
    s.section('Reset OTP lifecycle')
    with TempAuth() as a:
        a.set_password('BeforeReset_1!')
        cfg = {'ntfy_topic': '', 'twilio_account_sid': '',
               'gmail_client_id': '', 'notify_email': ''}
        ok, channel, dest = a.generate_reset_token(cfg)
        # No channel configured → falls back to console, still stores token
        import json as _j
        creds = _j.load(open(a.AUTH_FILE))
        token = creds.get('reset_token', '')

        s.check('Token generated (6 digits)',        len(token) == 6 and token.isdigit())
        s.check('Token has expiry > now',            creds.get('reset_expires', 0) > time.time())
        s.check('Wrong code rejected',               not a.consume_reset_token('000000', 'New_2!Pass'))
        s.check('Correct code accepted',              a.consume_reset_token(token, 'New_2!Pass'))
        s.check('Password updated after reset',      a.check_password('New_2!Pass'))
        s.check('Original password invalidated',     not a.check_password('BeforeReset_1!'))
        s.check('Token consumed — cannot reuse',     not a.consume_reset_token(token, 'New_2!Pass'))

    # ── Change OTP lifecycle ──────────────────────────────────────────────────
    s.section('Change OTP lifecycle (authenticated flow)')
    with TempAuth() as a:
        a.set_password('BeforeChange_1!')
        cfg = {'ntfy_topic': '', 'twilio_account_sid': '',
               'gmail_client_id': '', 'notify_email': ''}
        a.generate_change_token(cfg)
        import json as _j
        token = _j.load(open(a.AUTH_FILE)).get('change_token', '')

        s.check('Change token is separate from reset token',
                'reset_token' not in _j.load(open(a.AUTH_FILE)) or
                _j.load(open(a.AUTH_FILE)).get('reset_token') != token)
        s.check('Wrong code rejected',               not a.consume_change_token('000000', 'Changed_2!'))
        s.check('Correct code accepted',              a.consume_change_token(token, 'Changed_2!'))
        s.check('Password changed',                  a.check_password('Changed_2!'))
        s.check('Token consumed — cannot reuse',     not a.consume_change_token(token, 'Changed_2!'))

    # ── Token expiry ──────────────────────────────────────────────────────────
    s.section('Token expiry enforcement')
    with TempAuth() as a:
        a.set_password('Expiry_Test_1!')
        creds = a._load_or_create()

        creds['reset_token']    = '123456'
        creds['reset_expires']  = time.time() - 1   # already expired
        a._write()
        s.check('Expired reset token rejected',      not a.consume_reset_token('123456', 'NewPass!'))

        creds['change_token']   = '654321'
        creds['change_expires'] = time.time() - 1
        a._write()
        s.check('Expired change token rejected',     not a.consume_change_token('654321', 'NewPass!'))

        # Fresh token not yet expired
        creds['reset_token']   = '999888'
        creds['reset_expires'] = time.time() + 900
        a._write()
        s.check('Unexpired token accepted',          a.consume_reset_token('999888', 'Fresh_Pass!2'))

    # ── Password minimum length enforcement ───────────────────────────────────
    s.section('Password minimum length')
    with TempAuth() as a:
        a.set_password('ValidPass_1!')
        # set_password itself doesn't enforce length — that's the server route's job.
        # Verify the hashing works for edge-case lengths.
        a.set_password('12345678')
        s.check('8-char password hashes and verifies', a.check_password('12345678'))

    # ── Rate limiter ──────────────────────────────────────────────────────────
    s.section('Login rate limiting')
    import modules.auth as auth
    test_ip = '10.0.0.1'
    auth.clear_rate_limit(test_ip)

    s.check('Fresh IP not locked out',               not auth.check_rate_limit(test_ip))
    for _ in range(10):
        auth.record_failed_attempt(test_ip)
    s.check('Locked out after 10 failures',          auth.check_rate_limit(test_ip))
    auth.clear_rate_limit(test_ip)
    s.check('Lock cleared after explicit clear',     not auth.check_rate_limit(test_ip))

    # Simulate expiry
    auth._rate_lock.acquire()
    auth._failed[test_ip] = (10, time.time() - auth._LOCKOUT_SECS - 1)
    auth._rate_lock.release()
    s.check('Lock auto-expires after 15 minutes',    not auth.check_rate_limit(test_ip))
    auth.clear_rate_limit(test_ip)

    # ── Production .auth isolation ────────────────────────────────────────────
    s.section('Production .auth isolation')
    import modules.auth as auth_mod, os as _os
    import json as _j2
    prod_path = auth_mod._ORIG_AUTH_FILE if hasattr(auth_mod, '_ORIG_AUTH_FILE') else \
                _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), '.auth')
    if _os.path.exists(prod_path):
        before = _j2.load(open(prod_path))
        with TempAuth():
            pass   # run and exit a TempAuth context
        after = _j2.load(open(prod_path))
        s.check('Production .auth hash unchanged',   before.get('hash') == after.get('hash'))
        s.check('Production .auth secret_key unchanged',
                before.get('secret_key') == after.get('secret_key'))
    else:
        s.warn('Production .auth not found — skipping isolation check')

    return s.summary()


if __name__ == '__main__':
    run()
