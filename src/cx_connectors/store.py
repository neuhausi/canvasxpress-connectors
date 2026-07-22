"""App-user store + per-user data-source store (SQLite, secrets encrypted at rest).

  users     -- who can log into your app (username + salted PBKDF2 password hash)
  sources   -- each user's registered sources: a name, an ENCRYPTED connection
               string / secret, and the read-only SQL (or range) to run.

Connection strings are Fernet-encrypted. Passwords use PBKDF2-HMAC-SHA256 (stdlib);
plaintext passwords are never stored. This is intentionally dependency-light so the
core stays importable with just ``cryptography``.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
from typing import List, Optional

from cryptography.fernet import Fernet

_PBKDF2_ROUNDS = 200_000


def hash_password(password: str, salt: Optional[bytes] = None):
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return salt, digest


def verify_password(password: str, salt: bytes, expected: bytes) -> bool:
    _, digest = hash_password(password, salt)
    return secrets.compare_digest(digest, expected)


def generate_key() -> str:
    """Return a fresh Fernet key (base64 str) for TOKEN/connection encryption."""
    return Fernet.generate_key().decode("utf-8")


class Store:
    def __init__(self, db_path: str, encryption_key: str):
        self._fernet = Fernet(encryption_key.encode("utf-8"))
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                username  TEXT PRIMARY KEY,
                salt      BLOB NOT NULL,
                pw_hash   BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sources (
                username  TEXT NOT NULL,
                name      TEXT NOT NULL,
                conn_enc  BLOB NOT NULL,
                sql       TEXT NOT NULL,
                PRIMARY KEY (username, name)
            );
            """
        )
        self._conn.commit()

    # ---- users ----
    def create_user(self, username: str, password: str) -> bool:
        salt, digest = hash_password(password)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO users (username, salt, pw_hash) VALUES (?, ?, ?)",
                    (username, salt, digest),
                )
                self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def check_user(self, username: str, password: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT salt, pw_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
        return bool(row) and verify_password(password, row[0], row[1])

    # ---- per-user sources ----
    def save_source(self, username: str, name: str, conn_url: str, sql: str) -> None:
        conn_enc = self._fernet.encrypt(conn_url.encode("utf-8"))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sources (username, name, conn_enc, sql)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(username, name) DO UPDATE SET
                    conn_enc=excluded.conn_enc, sql=excluded.sql
                """,
                (username, name, conn_enc, sql),
            )
            self._conn.commit()

    def list_sources(self, username: str) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM sources WHERE username = ? ORDER BY name", (username,)
            ).fetchall()
        return [r[0] for r in rows]

    def get_source(self, username: str, name: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT conn_enc, sql FROM sources WHERE username = ? AND name = ?",
                (username, name),
            ).fetchone()
        if not row:
            return None
        return {"conn_url": self._fernet.decrypt(row[0]).decode("utf-8"), "sql": row[1]}

    def delete_source(self, username: str, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM sources WHERE username = ? AND name = ?", (username, name)
            )
            self._conn.commit()
