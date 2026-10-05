"""Password accounts and revocable, hashed sessions. No default account/password."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, get_session
from .models import Identified, Scoped, utcnow


class UserAccount(Identified, Scoped, Base):
    __tablename__ = "user_accounts"
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(30), default="reader")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class UserSession(Identified, Base):
    __tablename__ = "user_sessions"
    user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class LoginAttempt(Identified, Base):
    __tablename__ = "login_attempts"
    bucket: Mapped[str] = mapped_column(String(64), index=True)


def hash_password(password: str) -> str:
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Password must contain 12–1024 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt" or len(password) > 1024:
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def authenticate_session(session, token: str) -> dict | None:
    digest = hashlib.sha256(token.encode()).hexdigest()
    record = session.scalar(
        select(UserSession).where(
            UserSession.token_hash == digest,
            UserSession.revoked.is_(False),
            UserSession.expires_at > utcnow(),
        )
    )
    if record is None:
        return None
    user = session.get(UserAccount, record.user_id)
    if user is None or not user.enabled:
        return None
    return {
        "workspace_id": user.workspace_id,
        "role": user.role,
        "username": user.username,
        "user_id": user.id,
        "session_id": record.id,
    }


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=1024)


router = APIRouter(prefix="/v1/auth", tags=["accounts"])


@router.post("/login")
def login(payload: LoginInput, request: Request, session=Depends(get_session)):
    bucket = hashlib.sha256(
        (payload.username.casefold() + ":" + (request.client.host if request.client else "unknown")).encode()
    ).hexdigest()
    recent = session.scalar(
        select(func.count(LoginAttempt.id)).where(
            LoginAttempt.bucket == bucket, LoginAttempt.created_at > utcnow() - timedelta(minutes=15)
        )
    )
    if recent >= 8:
        raise HTTPException(
            429, "Too many login attempts; retry after 15 minutes", headers={"Retry-After": "900"}
        )
    # Deliberately same response for unknown, disabled and wrong-password accounts.
    user = session.scalar(select(UserAccount).where(UserAccount.username == payload.username))
    dummy = "scrypt$16384$8$1$00000000000000000000000000000000$" + "00" * 32
    valid = verify_password(payload.password, user.password_hash if user else dummy)
    if not user or not user.enabled or not valid:
        session.add(LoginAttempt(bucket=bucket))
        session.commit()
        raise HTTPException(401, "Invalid username or password")
    token = secrets.token_urlsafe(48)
    expires = utcnow() + timedelta(hours=12)
    session.add(
        UserSession(
            user_id=user.id, token_hash=hashlib.sha256(token.encode()).hexdigest(), expires_at=expires
        )
    )
    session.commit()
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires.isoformat(),
        "role": user.role,
        "username": user.username,
    }


def request_identity(request: Request, session):
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    identity = authenticate_session(session, token)
    if identity is None:
        raise HTTPException(401, "Authentication required")
    return identity


@router.get("/me")
def me(request: Request, session=Depends(get_session)):
    identity = request_identity(request, session)
    return {key: identity[key] for key in ("username", "role", "workspace_id")}


@router.post("/logout")
def logout(request: Request, session=Depends(get_session)):
    identity = request_identity(request, session)
    record = session.get(UserSession, identity["session_id"])
    record.revoked = True
    session.commit()
    return {"status": "signed_out"}


def main():
    import argparse
    import getpass

    from .db import SessionLocal, init_db
    from .models import Workspace

    parser = argparse.ArgumentParser(
        description="Create an EvidenceHarbor user locally; no public registration"
    )
    parser.add_argument("username")
    parser.add_argument("--workspace", default="default")
    parser.add_argument("--role", choices=["reader", "researcher", "editor", "admin"], default="reader")
    args = parser.parse_args()
    password = getpass.getpass("Password (minimum 12 characters): ")
    if password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords do not match")
    encoded = hash_password(password)
    init_db()
    with SessionLocal() as session:
        if session.scalar(select(UserAccount).where(UserAccount.username == args.username)):
            raise SystemExit("Username already exists; refusing to overwrite")
        if session.get(Workspace, args.workspace) is None:
            session.add(Workspace(id=args.workspace, name=args.workspace))
            session.flush()
        session.add(
            UserAccount(
                username=args.username, workspace_id=args.workspace, role=args.role, password_hash=encoded
            )
        )
        session.commit()
    print(f"Created {args.role} account {args.username}; no password or session saved in logs")


if __name__ == "__main__":
    main()
