"""
Isolated auth-flow test.
Uses a TEMP .auth file — production .auth, sessions, and login state are never touched.
Sends real OTPs via ntfy and Gmail, then polls/reads the code to verify end-to-end delivery.
"""
import sys, os, json, time, tempfile, shutil, urllib.request

sys.path.insert(0, os.path.dirname(__file__))

# ── Patch AUTH_FILE to a temp location BEFORE importing auth ─────────────────
_tmp_dir  = tempfile.mkdtemp(prefix='stoptrading_test_')
_tmp_auth = os.path.join(_tmp_dir, '.auth_test')

import modules.auth as auth_mod
_ORIG_AUTH_FILE = auth_mod.AUTH_FILE
auth_mod.AUTH_FILE = _tmp_auth
auth_mod._creds   = None   # force fresh load from temp file

from config import load_config

PASS  = '\033[92m✓\033[0m'
FAIL  = '\033[91m✗\033[0m'
HEAD  = '\033[94m'
RESET = '\033[0m'

results = []

def check(label, ok, detail=''):
    mark = PASS if ok else FAIL
    msg  = f'  {mark}  {label}'
    if detail:
        msg += f'  ({detail})'
    print(msg)
    results.append((label, ok))

def section(title):
    print(f'\n{HEAD}── {title} {"─"*(54-len(title))}{RESET}')


def poll_ntfy_code(topic, since_ts, timeout=30):
    """Poll ntfy for a message containing a 6-digit code sent after since_ts."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = f'https://ntfy.sh/{topic}/json?poll=1&since={int(since_ts)}'
            req = urllib.request.Request(url)
            raw = urllib.request.urlopen(req, timeout=8).read().decode()
            for line in raw.strip().splitlines():
                if not line.strip():
                    continue
                msg = json.loads(line)
                text = msg.get('message', '')
                import re
                m = re.search(r'\b(\d{6})\b', text)
                if m:
                    return m.group(1)
        except Exception:
            pass
        time.sleep(2)
    return None


print(f'\n{HEAD}{"═"*60}')
print('  StopTrading — Isolated Auth Flow Test')
print(f'  Temp auth file: {_tmp_auth}')
print(f'{"═"*60}{RESET}')

cfg = load_config()

# Snapshot production .auth state before any test runs
_prod_hash_before         = None
_prod_reset_token_before  = None
_prod_change_token_before = None
if os.path.exists(_ORIG_AUTH_FILE):
    _snap = json.load(open(_ORIG_AUTH_FILE))
    _prod_hash_before         = _snap.get('hash')
    _prod_reset_token_before  = _snap.get('reset_token')
    _prod_change_token_before = _snap.get('change_token')

# ════════════════════════════════════════════════════════════════
section('1. Temp auth setup')

auth_mod.set_password('TestPass_Initial!1')
creds = json.load(open(_tmp_auth))
check('Temp .auth created',           os.path.exists(_tmp_auth))
check('Temp .auth has salt+hash',     bool(creds.get('salt') and creds.get('hash')))
check('Temp .auth does NOT exist at production path',
      not os.path.samefile(_tmp_auth, _ORIG_AUTH_FILE) if os.path.exists(_ORIG_AUTH_FILE) else True)
check('Initial password verifies',    auth_mod.check_password('TestPass_Initial!1'))
check('Wrong password rejected',      not auth_mod.check_password('wrongpassword'))


# ════════════════════════════════════════════════════════════════
section('2. Forgot-password OTP (reset flow)')

print('  → Generating reset OTP and sending via ntfy/email …')
t_before = time.time()
ok_send, channel, dest = auth_mod.generate_reset_token(cfg)
check(f'OTP delivered via {channel} to {dest}', ok_send)

if ok_send and channel == 'ntfy':
    print('  → Polling ntfy for code (up to 30 s) …')
    topic = cfg.get('ntfy_topic', '')
    code = poll_ntfy_code(topic, t_before)
    check('Code received via ntfy',        bool(code), f'code={code}')
else:
    # email path — read code directly from temp .auth (simulates user reading email)
    creds = json.load(open(_tmp_auth))
    code  = creds.get('reset_token', '')
    check('Code present in temp .auth',    bool(code) and len(code) == 6, f'code={code}')

if code:
    check('Wrong code rejected',           not auth_mod.consume_reset_token('000000', 'NewPass_Reset!2'))
    check('Correct code accepted',          auth_mod.consume_reset_token(code, 'NewPass_Reset!2'))
    check('New password verifies',          auth_mod.check_password('NewPass_Reset!2'))
    check('Old password no longer valid',  not auth_mod.check_password('TestPass_Initial!1'))
    check('Token consumed (can\'t reuse)', not auth_mod.consume_reset_token(code, 'NewPass_Reset!2'))
else:
    check('Code retrieval failed — skipping consume tests', False)


# ════════════════════════════════════════════════════════════════
section('3. Change-password OTP (authenticated change flow)')

# Set a known password first
auth_mod.set_password('ChangeFrom_Pass!3')
check('Password set for change-flow test', auth_mod.check_password('ChangeFrom_Pass!3'))

print('  → Generating change-password OTP …')
t_before2 = time.time()
ok_send2, channel2, dest2 = auth_mod.generate_change_token(cfg)
check(f'Change OTP delivered via {channel2} to {dest2}', ok_send2)

if ok_send2 and channel2 == 'ntfy':
    print('  → Polling ntfy for code (up to 30 s) …')
    topic = cfg.get('ntfy_topic', '')
    code2 = poll_ntfy_code(topic, t_before2)
    check('Code received via ntfy',        bool(code2), f'code={code2}')
else:
    creds = json.load(open(_tmp_auth))
    code2 = creds.get('change_token', '')
    check('Code present in temp .auth',    bool(code2) and len(code2) == 6, f'code={code2}')

if code2:
    check('Wrong code rejected',           not auth_mod.consume_change_token('000000', 'ChangedTo_Pass!4'))
    check('Correct code accepted',          auth_mod.consume_change_token(code2, 'ChangedTo_Pass!4'))
    check('New password verifies',          auth_mod.check_password('ChangedTo_Pass!4'))
    check('Old password no longer valid',  not auth_mod.check_password('ChangeFrom_Pass!3'))
    check('Token consumed (can\'t reuse)', not auth_mod.consume_change_token(code2, 'ChangedTo_Pass!4'))
else:
    check('Code retrieval failed — skipping consume tests', False)


# ════════════════════════════════════════════════════════════════
section('4. Expiry enforcement')

auth_mod._creds = None
auth_mod.set_password('ExpireTest_Pass!5')
creds = auth_mod._load_or_create()
# Manually set expired token
creds['reset_token']   = '999999'
creds['reset_expires'] = time.time() - 1   # already expired
auth_mod._write()
check('Expired reset token rejected',  not auth_mod.consume_reset_token('999999', 'ShouldNotWork!6'))
creds['change_token']   = '888888'
creds['change_expires'] = time.time() - 1
auth_mod._write()
check('Expired change token rejected', not auth_mod.consume_change_token('888888', 'ShouldNotWork!6'))


# ════════════════════════════════════════════════════════════════
section('5. Production auth isolation check')

check('Production .auth untouched (path)',
      auth_mod.AUTH_FILE == _tmp_auth)
if os.path.exists(_ORIG_AUTH_FILE):
    prod_after = json.load(open(_ORIG_AUTH_FILE))
    check('Production password hash unchanged',
          prod_after.get('hash') == _prod_hash_before)
    check('No test tokens written to production .auth',
          prod_after.get('reset_token') == _prod_reset_token_before and
          prod_after.get('change_token') == _prod_change_token_before)


# ════════════════════════════════════════════════════════════════
section('Cleanup')

shutil.rmtree(_tmp_dir, ignore_errors=True)
auth_mod.AUTH_FILE = _ORIG_AUTH_FILE
auth_mod._creds    = None   # restore production state
check('Temp files removed',            not os.path.exists(_tmp_dir))
check('AUTH_FILE restored to production path', auth_mod.AUTH_FILE == _ORIG_AUTH_FILE)


# ════════════════════════════════════════════════════════════════
passed = sum(1 for _, ok in results if ok)
failed = sum(1 for _, ok in results if not ok)
total  = len(results)
color  = '\033[92m' if failed == 0 else '\033[91m'
print(f'\n{HEAD}{"═"*60}{RESET}')
print(f'  {color}Results: {passed}/{total} passed', end='')
if failed:
    print(f'  ·  {failed} FAILED', end='')
print(f'\033[0m')
print(f'{HEAD}{"═"*60}{RESET}\n')

sys.exit(0 if failed == 0 else 1)
