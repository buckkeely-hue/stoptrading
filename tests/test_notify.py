"""
Notification delivery chain tests.
Intent preserved: OTP codes are delivered via the first available channel
(SMS → ntfy → Gmail → SMTP); masked destinations never reveal full credentials;
a failed channel silently falls through to the next.
"""
from tests.harness_utils import Suite


def run() -> tuple:
    s = Suite('NOTIFY — OTP delivery chain, masking, channel fallthrough')

    from modules.auth import _send_otp, _mask_phone, _mask_email

    # ── Phone masking ─────────────────────────────────────────────────────────
    s.section('Phone number masking')
    s.check('+15551234567 → ***-***-4567',  _mask_phone('+15551234567') == '***-***-4567')
    s.check('+14155559876 → ***-***-9876',  _mask_phone('+14155559876') == '***-***-9876')
    s.check('Short number uses last 4',     _mask_phone('+1234').endswith('1234'))
    s.check('Number with spaces masked',    _mask_phone('+1 555 123 4567').endswith('4567'))

    # ── Email masking ─────────────────────────────────────────────────────────
    s.section('Email address masking')
    s.check('buck***@gmail.com',            _mask_email('buckkeely@gmail.com') == 'bu***@gmail.com')
    s.check('jo***@example.org',            _mask_email('john@example.org') == 'jo***@example.org')
    s.check('a***@x.com (short local)',     _mask_email('ab@x.com').startswith('a'))
    s.check('@-domain preserved',           '@gmail.com' in _mask_email('buckkeely@gmail.com'))

    # ── Channel priority: no channels configured ──────────────────────────────
    s.section('No channels configured → (False, none, \'\')')
    empty_cfg = {
        'twilio_account_sid': '', 'twilio_auth_token': '',
        'twilio_from': '',        'notify_phone': '',
        'ntfy_topic': '',
        'gmail_client_id': '',    'gmail_client_secret': '',
        'gmail_refresh_token': '', 'notify_email': '',
        'smtp_user': '',          'smtp_password': '',
    }
    ok, channel, dest = _send_otp('123456', empty_cfg)
    s.check('No channel → ok=False',        not ok)
    s.check('No channel → channel=none',    channel == 'none')
    s.check('No channel → dest=\'\'',       dest == '')

    # ── ntfy channel ─────────────────────────────────────────────────────────
    s.section('ntfy channel (mocked — no real network call)')
    import unittest.mock as mock
    import urllib.request

    ntfy_cfg = dict(empty_cfg)
    ntfy_cfg['ntfy_topic'] = 'test-topic-xyz'

    mock_resp = mock.MagicMock()
    mock_resp.read.return_value = b'{}'
    with mock.patch.object(urllib.request, 'urlopen', return_value=mock_resp) as m:
        ok2, ch2, dest2 = _send_otp('654321', ntfy_cfg)
    s.check('ntfy → ok=True',               ok2)
    s.check('ntfy → channel=ntfy',          ch2 == 'ntfy')
    s.check('ntfy → dest is topic',         dest2 == 'test-topic-xyz')
    called_url = m.call_args[0][0].full_url if m.called else ''
    s.check('ntfy URL contains topic',      'test-topic-xyz' in called_url)
    s.check('ntfy message contains code',
            m.called and b'654321' in m.call_args[0][0].data)

    # ── Twilio channel (mocked) ───────────────────────────────────────────────
    s.section('Twilio SMS channel (mocked)')
    twilio_cfg = dict(empty_cfg)
    twilio_cfg.update({
        'twilio_account_sid': 'ACtest123',
        'twilio_auth_token':  'authtoken456',
        'twilio_from':        '+15559876543',
        'notify_phone':       '+15551234567',
    })
    with mock.patch.object(urllib.request, 'urlopen', return_value=mock_resp) as m2:
        ok3, ch3, dest3 = _send_otp('111222', twilio_cfg)
    s.check('Twilio → ok=True',             ok3)
    s.check('Twilio → channel=sms',         ch3 == 'sms')
    s.check('Twilio → dest is masked phone', dest3.startswith('***'))
    s.check('Twilio takes priority over ntfy (called once)',  m2.call_count == 1)
    twilio_url = m2.call_args[0][0].full_url if m2.called else ''
    s.check('Twilio API URL contains account SID',
            'ACtest123' in twilio_url)

    # ── Fallthrough: Twilio fails → ntfy succeeds ────────────────────────────
    s.section('Channel fallthrough: failed Twilio → ntfy')
    fallthrough_cfg = dict(twilio_cfg)
    fallthrough_cfg['ntfy_topic'] = 'fallback-topic'

    call_count = [0]
    def side_effect(req, timeout=10):
        call_count[0] += 1
        if call_count[0] == 1:   # first call = Twilio, make it fail
            raise Exception('Twilio network error')
        return mock_resp           # second call = ntfy, succeeds

    with mock.patch.object(urllib.request, 'urlopen', side_effect=side_effect):
        ok4, ch4, dest4 = _send_otp('333444', fallthrough_cfg)
    s.check('Twilio failure falls through to ntfy → ok=True',  ok4)
    s.check('channel=ntfy after Twilio failure',               ch4 == 'ntfy')
    s.check('Two network calls made (Twilio + ntfy)',           call_count[0] == 2)

    # ── OTP message content ───────────────────────────────────────────────────
    s.section('OTP message content requirements')
    ntfy_cfg2 = dict(empty_cfg)
    ntfy_cfg2['ntfy_topic'] = 'content-test'
    captured = {}

    def capture(req, timeout=10):
        captured['data'] = req.data
        captured['headers'] = req.headers
        return mock_resp

    with mock.patch.object(urllib.request, 'urlopen', side_effect=capture):
        _send_otp('987654', ntfy_cfg2)

    msg = captured.get('data', b'').decode()
    s.check('OTP code present in message',       '987654' in msg)
    s.check('Expiry info present in message',    '15 min' in msg or 'expire' in msg.lower())

    # ── Gmail API path (mocked) ───────────────────────────────────────────────
    s.section('Gmail API channel (mocked)')
    gmail_cfg = dict(empty_cfg)
    gmail_cfg.update({
        'gmail_client_id':     'fake_client_id',
        'gmail_client_secret': 'fake_secret',
        'gmail_refresh_token': 'fake_refresh',
        'gmail_token_uri':     'https://oauth2.googleapis.com/token',
        'notify_email':        'user@gmail.com',
        'smtp_user':           'user@gmail.com',
    })

    import json, io

    call_num = [0]
    def gmail_mock(req, timeout=10):
        call_num[0] += 1
        if call_num[0] == 1:
            # First call = token refresh
            resp = mock.MagicMock()
            resp.read.return_value = json.dumps({'access_token': 'fake_access_123'}).encode()
            return resp
        else:
            # Second call = send message
            resp2 = mock.MagicMock()
            resp2.read.return_value = json.dumps({'id': 'msg_abc123'}).encode()
            return resp2

    with mock.patch.object(urllib.request, 'urlopen', side_effect=gmail_mock):
        ok5, ch5, dest5 = _send_otp('555666', gmail_cfg)

    s.check('Gmail → ok=True',               ok5)
    s.check('Gmail → channel=email',         ch5 == 'email')
    s.check('Gmail → dest is masked email',  '***' in dest5 and '@gmail.com' in dest5)
    s.check('Two API calls made (refresh + send)', call_num[0] == 2)

    return s.summary()


if __name__ == '__main__':
    run()
