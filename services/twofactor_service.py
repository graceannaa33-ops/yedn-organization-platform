"""Time-based one-time passwords (TOTP, RFC 6238) for staff 2FA.

Works with any authenticator app (Google Authenticator, Microsoft
Authenticator, Authy...). No JavaScript and no QR library is needed: the
setup page shows the secret key, which the user types into their app.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30
DIGITS = 6


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret, counter):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % (10 ** DIGITS)).zfill(DIGITS)


def current_code(secret, at=None):
    return _code(secret, int((at or time.time()) // STEP))


def verify(user, code, at=None):
    """Accept the current code or one step either side; never accept a code twice."""
    code = (code or "").strip().replace(" ", "")
    if not user.totp_secret or not code.isdigit() or len(code) != DIGITS:
        return False
    counter = int((at or time.time()) // STEP)
    for c in (counter - 1, counter, counter + 1):
        if hmac.compare_digest(_code(user.totp_secret, c), code):
            if user.totp_last_counter is not None and c <= user.totp_last_counter:
                return False  # replay
            user.totp_last_counter = c
            return True
    return False


def provisioning_uri(user, issuer):
    return (f"otpauth://totp/{quote(issuer)}:{quote(user.email)}?secret={user.totp_secret}"
            f"&issuer={quote(issuer)}&digits={DIGITS}&period={STEP}")


def formatted_secret(secret):
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
