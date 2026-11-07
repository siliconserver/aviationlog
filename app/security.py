import hashlib
import io
import os
import secrets

import bcrypt
import pyotp
import qrcode
import qrcode.image.svg
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-please")
RESET_MAX_AGE = 3600  # seconds


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], password_hash.encode("ascii"))
    except ValueError:
        return False


# ---- TOTP -----------------------------------------------------------------

def new_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str) -> bool:
    code = (code or "").replace(" ", "")
    return bool(secret) and pyotp.TOTP(secret).verify(code, valid_window=1)


def totp_qr_svg(secret: str, username: str) -> str:
    uri = pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="AviationLog")
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


# ---- Recovery codes ---------------------------------------------------------

def _normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "").lower() if ch.isalnum())


def _hash_code(code: str) -> str:
    return hashlib.sha256(_normalize_code(code).encode()).hexdigest()


def new_recovery_codes(count: int = 10) -> tuple[list[str], list[str]]:
    """Returns (plain codes to show once, hashes to store)."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(alphabet) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes, [_hash_code(c) for c in codes]


def use_recovery_code(stored: list[str], code: str) -> list[str] | None:
    """Returns the remaining hashes if the code was valid, otherwise None."""
    h = _hash_code(code)
    if not _normalize_code(code) or h not in (stored or []):
        return None
    return [x for x in stored if x != h]


# ---- Password reset tokens ---------------------------------------------------

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(SECRET_KEY, salt="password-reset")


def make_reset_token(user) -> str:
    # The password hash fragment makes the token single-use: it stops working
    # as soon as the password was changed.
    return _serializer().dumps({"u": user.id, "p": user.password_hash[-12:]})


def read_reset_token(token: str):
    try:
        return _serializer().loads(token, max_age=RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
