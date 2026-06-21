"""Application database (Postgres via SQLAlchemy).

Wildrider's business logic lives here, separate from the realtime audio path:
for now just the user accounts behind the login wall, but this is the seam the
platform (projects, sharing, billing, …) grows from. Passwords are stored as
salted PBKDF2-HMAC-SHA256 hashes (stdlib — no native crypto dependency).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://wildrider:wildrider@localhost:5432/wildrider")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {"id": self.id, "username": self.username,
                "is_admin": self.is_admin, "active": self.active}


# --------------------------------------------------------------------------- #
# Password hashing (PBKDF2-HMAC-SHA256, stdlib).
# --------------------------------------------------------------------------- #
_ITERATIONS = 240_000


def hash_password(pw: str, iterations: int = _ITERATIONS) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Lifecycle.
# --------------------------------------------------------------------------- #
def init_db(retries: int = 30, delay: float = 1.0) -> None:
    """Create tables, waiting for Postgres to accept connections (the container
    may still be starting even with depends_on: service_healthy)."""
    last = None
    for _ in range(retries):
        try:
            Base.metadata.create_all(engine)
            return
        except Exception as exc:  # noqa: BLE001 — retry any connection error
            last = exc
            time.sleep(delay)
    raise RuntimeError(f"database not reachable: {last}")


def seed_admin() -> None:
    """Ensure the default admin account exists (idempotent)."""
    username = os.environ.get("ADMIN_USER", "nzimas")
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not password:
        return
    with SessionLocal() as s:
        existing = s.scalar(select(User).where(User.username == username))
        if existing:
            if not existing.is_admin:        # keep the seed user an admin
                existing.is_admin = True
                s.commit()
            return
        s.add(User(username=username, password_hash=hash_password(password),
                   is_admin=True, active=True))
        s.commit()


def authenticate(username: str, password: str) -> User | None:
    with SessionLocal() as s:
        user = s.scalar(select(User).where(User.username == username))
        if user and user.active and verify_password(password, user.password_hash):
            return user
        return None


def get_user(uid: int) -> User | None:
    with SessionLocal() as s:
        return s.get(User, uid)
