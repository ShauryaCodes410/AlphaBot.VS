"""Accounts, login sessions, and per-user database ownership."""

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from typing import Any, Dict, List, Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CATALOG_PATH = os.path.join(DATA_DIR, "accounts.db")


def _connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(CATALOG_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_databases (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                storage_key TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                path TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"{salt.hex()}:{digest.hex()}"


def _check_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 200_000
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _user_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {"id": row["id"], "email": row["email"]}


def register(email: str, password: str) -> Dict[str, Any]:
    init()
    email = (email or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ValueError("Enter a valid email address.")
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters.")
    with _connect() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            raise ValueError("An account with that email already exists.")
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email, _hash_password(password), time.time()),
        )
        return {"id": cur.lastrowid, "email": email}


def login(email: str, password: str) -> Optional[tuple]:
    """Returns (user, raw session token) or None."""
    init()
    email = (email or "").strip().lower()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not row or not _check_password(password or "", row["password_hash"]):
            return None
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at) VALUES (?, ?, ?)",
            (_token_hash(token), row["id"], time.time()),
        )
        return _user_row(row), token


def logout(token: Optional[str]) -> None:
    if not token:
        return
    init()
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def user_from_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    init()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT users.id, users.email
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ?
            """,
            (_token_hash(token),),
        ).fetchone()
        return _user_row(row) if row else None


def user_dir(user_id: int) -> str:
    path = os.path.join(DATA_DIR, "users", str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


def storage_key_for(user_id: int, safe_name: str) -> str:
    return f"u{user_id}_{safe_name}"


def is_user_storage_key(name: str) -> bool:
    return bool(re.match(r"^u\d+_", name or ""))


def add_database(user_id: int, safe_name: str, display_name: str, path: str) -> Dict[str, Any]:
    init()
    key = storage_key_for(user_id, safe_name)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_databases (user_id, storage_key, display_name, path, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(storage_key) DO UPDATE SET
                display_name = excluded.display_name,
                path = excluded.path
            """,
            (user_id, key, display_name, path, time.time()),
        )
    return {"storage_key": key, "display_name": display_name, "path": path, "user_id": user_id}


def list_databases(user_id: int) -> List[Dict[str, Any]]:
    init()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT storage_key, display_name, path, user_id
            FROM user_databases
            WHERE user_id = ?
            ORDER BY display_name
            """,
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def all_databases() -> List[Dict[str, Any]]:
    init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT storage_key, display_name, path, user_id FROM user_databases"
        ).fetchall()
    return [dict(row) for row in rows]


def get_database(storage_key: str) -> Optional[Dict[str, Any]]:
    init()
    with _connect() as conn:
        row = conn.execute(
            "SELECT storage_key, display_name, path, user_id FROM user_databases WHERE storage_key = ?",
            (storage_key,),
        ).fetchone()
    return dict(row) if row else None


def delete_database(user_id: int, storage_key: str) -> Optional[Dict[str, Any]]:
    row = get_database(storage_key)
    if not row or row["user_id"] != user_id:
        return None
    with _connect() as conn:
        conn.execute(
            "DELETE FROM user_databases WHERE storage_key = ? AND user_id = ?",
            (storage_key, user_id),
        )
    return row
