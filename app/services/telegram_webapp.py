"""
Telegram Mini App ``initData`` verification and account resolution (Phase T, T-3).

Verification follows https://core.telegram.org/bots/webapps
("Validating data received via the Mini App"):

    data_check_string = all received fields except ``hash``, sorted by key,
                        as ``key=value`` joined with "\\n" (values URL-decoded)
    secret_key        = HMAC_SHA256(key="WebAppData", msg=<bot_token>)
    valid             = hex(HMAC_SHA256(key=secret_key, msg=data_check_string)) == hash

Then ``auth_date`` must be inside the freshness window, and each ``hash`` may
sign in only once (replay guard, app/models/telegram_webapp.py).

Accounts: the Telegram user maps to at most one Siiqo user, by ``telegram_id``
(unique column). Legacy bot accounts that only have the virtual email
``tg_<id>@siiqo.app`` are linked on first sign-in. New accounts use the same
virtual-account scheme as the bot and /api/auth/telegram-login.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.telegram_webapp import TelegramInitDataUse
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_SECONDS = 900          # initData older than 15 minutes is refused
FUTURE_SKEW_SECONDS = 60               # tolerate small clock drift only
MAX_INIT_DATA_LENGTH = 8192
LINK_TOKEN_MAX_AGE_SECONDS = 600       # new-user link token lifetime
_LINK_SALT = 'siiqo.telegram-webapp.link.v1'


class InitDataError(Exception):
    """initData was refused. ``code`` is stable for the client; ``status`` is HTTP."""

    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class TelegramWebAppUser:
    telegram_id: str
    first_name: str
    last_name: str
    username: str
    photo_url: str
    auth_date: int
    init_hash: str


def max_age_seconds() -> int:
    raw = os.environ.get('TG_WEBAPP_MAX_AGE_SECONDS', '')
    try:
        value = int(raw) if raw.strip() else DEFAULT_MAX_AGE_SECONDS
    except ValueError:
        value = DEFAULT_MAX_AGE_SECONDS
    return max(60, min(value, 86400))


def _bot_token() -> str:
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    if not token:
        raise InitDataError('not_configured', 'Telegram sign-in is not configured on the server.', 503)
    return token


def compute_hash(fields: dict[str, str], bot_token: str) -> str:
    """HMAC-SHA256 hex digest over the data-check-string (every field except ``hash``)."""
    check_string = '\n'.join(f'{k}={fields[k]}' for k in sorted(fields) if k != 'hash')
    secret_key = hmac.new(b'WebAppData', bot_token.encode('utf-8'), hashlib.sha256).digest()
    return hmac.new(secret_key, check_string.encode('utf-8'), hashlib.sha256).hexdigest()


def verify_init_data(init_data: str, now: int | None = None) -> TelegramWebAppUser:
    """Check signature, freshness and shape. Does not record the hash (see consume)."""
    if not isinstance(init_data, str) or not init_data.strip():
        raise InitDataError('missing_init_data', 'initData is required.', 400)
    if len(init_data) > MAX_INIT_DATA_LENGTH:
        raise InitDataError('invalid_init_data', 'initData is too long.', 400)

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise InitDataError('invalid_init_data', 'initData is not a valid query string.', 400)

    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            raise InitDataError('invalid_init_data', 'initData has a repeated field.', 400)
        fields[key] = value

    received_hash = fields.get('hash', '')
    if not received_hash:
        raise InitDataError('missing_field', 'initData has no hash.', 400)
    if 'auth_date' not in fields:
        raise InitDataError('missing_field', 'initData has no auth_date.', 400)
    if 'user' not in fields:
        raise InitDataError('missing_field', 'initData has no user.', 400)

    expected = compute_hash(fields, _bot_token())
    if not hmac.compare_digest(expected, received_hash.lower()):
        raise InitDataError('bad_hash', 'Telegram sign-in could not be verified.', 401)

    try:
        auth_date = int(fields['auth_date'])
    except ValueError:
        raise InitDataError('invalid_init_data', 'auth_date is not a number.', 400)
    now = int(time.time()) if now is None else now
    if auth_date > now + FUTURE_SKEW_SECONDS:
        raise InitDataError('expired', 'Telegram sign-in data is not valid yet.', 401)
    if now - auth_date > max_age_seconds():
        raise InitDataError('expired', 'Telegram sign-in data has expired. Reopen the app from Telegram.', 401)

    try:
        user = json.loads(fields['user'])
    except ValueError:
        raise InitDataError('invalid_init_data', 'user is not valid JSON.', 400)
    if not isinstance(user, dict) or not isinstance(user.get('id'), int) or user['id'] <= 0:
        raise InitDataError('missing_field', 'user.id is missing.', 400)

    return TelegramWebAppUser(
        telegram_id=str(user['id']),
        first_name=str(user.get('first_name') or '')[:100],
        last_name=str(user.get('last_name') or '')[:100],
        username=str(user.get('username') or '')[:100],
        photo_url=str(user.get('photo_url') or '')[:500],
        auth_date=auth_date,
        init_hash=received_hash.lower(),
    )


def consume(tg: TelegramWebAppUser) -> None:
    """Record the initData hash; a second use raises ``replayed``."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds() + FUTURE_SKEW_SECONDS)
    try:
        TelegramInitDataUse.query.filter(TelegramInitDataUse.used_at < cutoff).delete(synchronize_session=False)
        db.session.add(TelegramInitDataUse(init_hash=tg.init_hash, telegram_id=tg.telegram_id, auth_date=tg.auth_date))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise InitDataError('replayed', 'This Telegram sign-in was already used. Reopen the app from Telegram.', 401)


def virtual_email(telegram_id: str) -> str:
    return f'tg_{telegram_id}@siiqo.app'


def is_virtual_email(email: str) -> bool:
    email = (email or '').lower()
    return email.startswith('tg_') and email.endswith('@siiqo.app')


def find_account(telegram_id: str) -> User | None:
    """The one account for this Telegram user, linking a legacy bot account if needed."""
    user = User.query.filter_by(telegram_id=telegram_id).first()
    if user:
        return user
    legacy = User.query.filter_by(email=virtual_email(telegram_id)).first()
    if legacy and not legacy.telegram_id:
        legacy.telegram_id = telegram_id
        db.session.commit()
        return legacy
    return None


def create_virtual_account(telegram_id: str, first_name: str, last_name: str, photo_url: str) -> User:
    """Same scheme as the bot: tg_<id>@siiqo.app with password tg_<id>_<TG_AUTH_SECRET>.

    Never creates a second account: an existing match is returned instead, and
    a concurrent insert that loses the unique-constraint race re-reads the winner.
    """
    existing = find_account(telegram_id)
    if existing:
        return existing

    if User.query.filter_by(email=virtual_email(telegram_id)).first():
        # The virtual email belongs to an account linked to a different Telegram id.
        raise InitDataError('conflict', 'This Telegram account cannot be set up automatically. Please contact support.', 409)

    tg_secret = os.environ.get('TG_AUTH_SECRET', '')
    if not tg_secret.strip():
        logger.error('[TG-WEBAPP] TG_AUTH_SECRET is not set; Telegram account creation disabled')
        raise InitDataError('signup_unavailable',
                            'Telegram sign-up is temporarily unavailable. Please sign up with email.', 503)

    user = User(
        email=virtual_email(telegram_id),
        first_name=first_name or 'Telegram',
        last_name=last_name or 'User',
        role=UserRole.BUYER,
        is_verified=True,
        is_active=True,
        profile_pic=photo_url or None,
        telegram_id=telegram_id,
    )
    user.set_password(f'tg_{telegram_id}_{tg_secret}')
    try:
        db.session.add(user)
        db.session.flush()
        user.generate_referral_code()
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        winner = find_account(telegram_id)
        if winner:
            return winner
        raise
    return user


# ── Link token: lets a verified-but-unlinked Telegram user finish sign-up ────
# initData is single-use, so the first call hands back a short-lived signed
# token naming the verified Telegram user. It is not a JWT and grants no access.

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt=_LINK_SALT)


def make_link_token(tg: TelegramWebAppUser) -> str:
    return _serializer().dumps({
        'tid': tg.telegram_id, 'fn': tg.first_name, 'ln': tg.last_name, 'ph': tg.photo_url,
    })


def read_link_token(token: str) -> dict:
    if not isinstance(token, str) or not token:
        raise InitDataError('missing_link_token', 'link_token is required.', 400)
    try:
        data = _serializer().loads(token, max_age=LINK_TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired:
        raise InitDataError('expired', 'This sign-up step has expired. Reopen the app from Telegram.', 401)
    except BadSignature:
        raise InitDataError('bad_link_token', 'This sign-up step could not be verified.', 401)
    if not isinstance(data, dict) or not str(data.get('tid') or '').isdigit():
        raise InitDataError('bad_link_token', 'This sign-up step could not be verified.', 401)
    return data
