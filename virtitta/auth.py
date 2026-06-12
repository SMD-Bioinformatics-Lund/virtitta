from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from virtitta.config import Config
from virtitta.repository import (
    connect,
    create_auth_session,
    delete_auth_session,
    get_auth_session,
    get_auth_user,
)


ROLE_ADMIN = "admin"
ROLE_REVIEWER = "reviewer"
ROLE_COMMENTER = "commenter"
ROLE_VIEWER = "viewer"
AUTH_ROLES = {ROLE_ADMIN, ROLE_REVIEWER, ROLE_COMMENTER, ROLE_VIEWER}

PERMISSION_VIEW = "sample.view"
PERMISSION_EXPORT_READ = "export.read"
PERMISSION_EXPORT_LIMS = "export.lims"
PERMISSION_COMMENT_ADD = "comment.add"
PERMISSION_COMMENT_DELETE = "comment.delete"
PERMISSION_QC_UPDATE = "qc.update"
PERMISSION_GROUP_UPDATE = "group.update"
PERMISSION_CATEGORY_UPDATE = "category.update"
PERMISSION_SAMPLE_DELETE = "sample.delete"
PERMISSION_METADATA_OVERRIDE = "metadata.override"
PERMISSION_RUN_REFRESH = "run.refresh"
PERMISSION_USER_ADMIN = "user.admin"

ALL_PERMISSIONS = {
    PERMISSION_VIEW,
    PERMISSION_EXPORT_READ,
    PERMISSION_EXPORT_LIMS,
    PERMISSION_COMMENT_ADD,
    PERMISSION_COMMENT_DELETE,
    PERMISSION_QC_UPDATE,
    PERMISSION_GROUP_UPDATE,
    PERMISSION_CATEGORY_UPDATE,
    PERMISSION_SAMPLE_DELETE,
    PERMISSION_METADATA_OVERRIDE,
    PERMISSION_RUN_REFRESH,
    PERMISSION_USER_ADMIN,
}

ROLE_PERMISSIONS = {
    ROLE_ADMIN: set(ALL_PERMISSIONS),
    ROLE_REVIEWER: {
        PERMISSION_VIEW,
        PERMISSION_EXPORT_READ,
        PERMISSION_EXPORT_LIMS,
        PERMISSION_COMMENT_ADD,
        PERMISSION_QC_UPDATE,
        PERMISSION_GROUP_UPDATE,
        PERMISSION_CATEGORY_UPDATE,
    },
    ROLE_COMMENTER: {
        PERMISSION_VIEW,
        PERMISSION_EXPORT_READ,
        PERMISSION_COMMENT_ADD,
        PERMISSION_GROUP_UPDATE,
    },
    ROLE_VIEWER: {
        PERMISSION_VIEW,
        PERMISSION_EXPORT_READ,
    },
}


@dataclass(frozen=True)
class CurrentUser:
    username: str
    display_name: str
    role: str
    permissions: frozenset[str]
    csrf_token: str = ""
    authenticated: bool = False

    def can(self, permission: str) -> bool:
        return permission in self.permissions


def disabled_auth_user() -> CurrentUser:
    return CurrentUser(
        username="",
        display_name="",
        role=ROLE_ADMIN,
        permissions=frozenset(ALL_PERMISSIONS),
        authenticated=False,
    )


def user_from_session_row(row: dict) -> CurrentUser:
    username = str(row["username"])
    display_name = str(row.get("display_name") or username)
    role = str(row.get("role") or ROLE_VIEWER)
    permissions = ROLE_PERMISSIONS.get(role, set())
    return CurrentUser(
        username=username,
        display_name=display_name,
        role=role,
        permissions=frozenset(permissions),
        csrf_token=str(row["csrf_token"]),
        authenticated=True,
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_timestamp(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def normalize_username(username: str) -> str:
    return username.strip().lower()


def validate_role(role: str) -> str:
    normalized = role.strip().lower()
    if normalized not in AUTH_ROLES:
        raise ValueError(f"Invalid role: {role}")
    return normalized


def hash_password(password: str, *, iterations: int) -> str:
    if not password:
        raise ValueError("Password cannot be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        [
            "pbkdf2_sha256",
            str(iterations),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
    except (binascii.Error, ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def authenticate_local_user(config: Config, username: str, password: str) -> dict | None:
    normalized = normalize_username(username)
    if not normalized:
        return None
    connection = connect(config.database.path)
    try:
        user = get_auth_user(connection, normalized)
    finally:
        connection.close()
    if user is None or not user.get("is_enabled"):
        return None
    if not verify_password(password, str(user["password_hash"])):
        return None
    return user


def create_login_session(config: Config, username: str) -> tuple[str, CurrentUser]:
    token = new_session_token()
    csrf_token = new_csrf_token()
    expires_at = utc_timestamp(utc_now() + timedelta(days=config.auth.session_days))
    connection = connect(config.database.path)
    try:
        create_auth_session(connection, token_hash(token), username, csrf_token, expires_at)
        row = get_auth_session(connection, token_hash(token), utc_timestamp(utc_now()))
    finally:
        connection.close()
    if row is None:
        raise RuntimeError("Failed to create login session")
    return token, user_from_session_row(row)


def get_user_from_cookie(config: Config, cookie_value: str | None) -> CurrentUser | None:
    if not cookie_value:
        return None
    connection = connect(config.database.path)
    try:
        row = get_auth_session(connection, token_hash(cookie_value), utc_timestamp(utc_now()))
    finally:
        connection.close()
    if row is None or not row.get("is_enabled"):
        return None
    return user_from_session_row(row)


def logout_session(config: Config, cookie_value: str | None) -> None:
    if not cookie_value:
        return
    connection = connect(config.database.path)
    try:
        delete_auth_session(connection, token_hash(cookie_value))
    finally:
        connection.close()
