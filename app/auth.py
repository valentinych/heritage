from __future__ import annotations

import logging
import re
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import User
from app.util import utcnow

log = logging.getLogger(__name__)
_hasher = PasswordHasher()
_dummy_hash: str | None = None
_USERNAME = re.compile(r"(?=.*[^\W_])[\w.-]{2,64}", flags=re.UNICODE)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


def _dummy() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("not-a-real-password")
    return _dummy_hash


def verify_login(user: User | None, password: str) -> bool:
    if user is None:
        verify_password(password, _dummy())
        return False
    return verify_password(password, user.password_hash)


def validate_username(username: str) -> str | None:
    username = username.strip()
    if _USERNAME.fullmatch(username) is None:
        return None
    return username


def unset_password_hash() -> str:
    return hash_password(secrets.token_urlsafe(32))


def password_error(password: str) -> str | None:
    if len(password) < 8:
        return "Пароль короче 8 символов."
    if len(password) > 200:
        return "Пароль слишком длинный."
    return None


def ensure_admin(db: Session, settings: Settings) -> None:
    count = db.scalar(select(func.count()).select_from(User)) or 0
    if count:
        return
    username = validate_username(settings.admin_user)
    if username is None or password_error(settings.admin_password):
        log.error("Администратор не создан: проверьте HERITAGE_ADMIN_USER и HERITAGE_ADMIN_PASSWORD")
        return
    db.add(
        User(
            username=username,
            password_hash=hash_password(settings.admin_password),
            is_admin=True,
            created_at=utcnow(),
        )
    )
    db.commit()
    log.info("Создан администратор %s", username)


class LoginGuard:
    def __init__(self, limit: int = 8, window_seconds: int = 900) -> None:
        self.limit = limit
        self.window = window_seconds
        self._fails: dict[str, list[float]] = {}

    def too_many(self, key: str) -> bool:
        return len(self._prune(key)) >= self.limit

    def record_failure(self, key: str) -> None:
        self._prune(key).append(time.monotonic())

    def clear(self, key: str) -> None:
        self._fails.pop(key, None)

    def _prune(self, key: str) -> list[float]:
        now = time.monotonic()
        kept = [item for item in self._fails.get(key, []) if now - item < self.window]
        self._fails[key] = kept
        return kept
