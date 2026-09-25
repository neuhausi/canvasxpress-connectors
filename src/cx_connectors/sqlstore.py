"""SQLAlchemy-backed stores for users, data sources, and OAuth tokens.

A drop-in replacement for :class:`cx_connectors.store.Store` and
:class:`cx_connectors.store.TokenStore` backed by any SQLAlchemy-supported
database (Postgres in production, SQLite in tests).  Useful for hosts that
restart or run several processes, where a local SQLite file would be wiped at
every restart and each process would get its own copy.

Tables use the ``cxc_`` prefix so they can share a database with other apps.
SQLAlchemy is an optional ``sql`` extra, imported lazily so the core package
keeps importing with only ``cryptography`` installed.
"""

from __future__ import annotations

import datetime
import json
from typing import List, Optional

from .store import hash_password, verify_password


def normalize_sql_url(url: str) -> str:
    """Rewrite a ``postgres://`` URL to ``postgresql://`` for SQLAlchemy.

    SQLAlchemy removed the ``postgres://`` dialect alias in 1.4.  Both
    ``postgres://`` and ``postgresql://`` are documented as valid URL schemes,
    so normalize here rather than narrowing the contract.

    :param url: A database URL, e.g. ``postgres://user:pw@host/db``.
    :returns: The same URL with a SQLAlchemy-loadable scheme.
    """
    if url and url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


class SqlStore:
    """Users + per-user encrypted data sources on a SQL database (via SQLAlchemy).

    The public API is identical to :class:`~cx_connectors.store.Store`.
    """

    def __init__(self, url: str, encryption_key: str, engine=None):
        from cryptography.fernet import Fernet

        self._fernet = Fernet(encryption_key.encode("utf-8"))
        sa = _sqlalchemy()
        self._sa = sa
        self._engine = engine or sa.create_engine(normalize_sql_url(url), future=True)
        metadata = sa.MetaData()
        self._users = sa.Table(
            "cxc_users",
            metadata,
            sa.Column("username", sa.Text, primary_key=True),
            sa.Column("salt", sa.LargeBinary, nullable=False),
            sa.Column("pw_hash", sa.LargeBinary, nullable=False),
        )
        self._sources = sa.Table(
            "cxc_sources",
            metadata,
            sa.Column("username", sa.Text, nullable=False),
            sa.Column("name", sa.Text, nullable=False),
            sa.Column("conn_enc", sa.LargeBinary, nullable=False),
            sa.Column("sql", sa.Text, nullable=False),
            sa.Column("kind", sa.Text, nullable=False, server_default=sa.text("'sql'")),
            sa.Column("config", sa.Text),
            sa.Column("updated_at", sa.Text),
            sa.PrimaryKeyConstraint("username", "name"),
        )
        metadata.create_all(self._engine)

    # ---- users ----
    def create_user(self, username: str, password: str) -> bool:
        """Create a user; returns False if the username is already taken."""
        salt, digest = hash_password(password)
        users = self._users
        try:
            with self._engine.begin() as conn:
                conn.execute(users.insert().values(
                    username=username, salt=salt, pw_hash=digest,
                ))
            return True
        except self._sa.exc.IntegrityError:
            return False

    def check_user(self, username: str, password: str) -> bool:
        users = self._users
        with self._engine.connect() as conn:
            row = conn.execute(
                self._sa.select(users.c.salt, users.c.pw_hash)
                .where(users.c.username == username)
            ).first()
        return bool(row) and verify_password(password, _b(row[0]), _b(row[1]))

    # ---- per-user sources ----
    def save_source(self, username: str, name: str, conn_url: str, sql: str,
                    kind: str = "sql", config: Optional[dict] = None) -> None:
        """Store (or update) a user's data source.

        :param kind: ``"sql"`` (a SELECT in ``sql``) or another source kind (e.g.
            ``"packed"``) whose reassembly config is carried in ``config``.
        :param config: Optional dict serialized to JSON for non-SQL kinds.
        """
        conn_enc = self._fernet.encrypt(conn_url.encode("utf-8"))
        updated = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        config_json = json.dumps(config) if config is not None else None
        sources = self._sources
        # Insert; on duplicate-key, update in a fresh transaction (Postgres aborts
        # the whole transaction after an IntegrityError, so the update must go in a
        # new begin() block, not inside a savepoint fallback on the same conn).
        try:
            with self._engine.begin() as conn:
                conn.execute(sources.insert().values(
                    username=username, name=name, conn_enc=conn_enc,
                    sql=sql, kind=kind, config=config_json, updated_at=updated,
                ))
        except self._sa.exc.IntegrityError:
            with self._engine.begin() as conn:
                conn.execute(
                    sources.update()
                    .where(
                        (sources.c.username == username) & (sources.c.name == name)
                    )
                    .values(
                        conn_enc=conn_enc, sql=sql, kind=kind,
                        config=config_json, updated_at=updated,
                    )
                )

    def list_sources(self, username: str) -> List[str]:
        sources = self._sources
        with self._engine.connect() as conn:
            rows = conn.execute(
                self._sa.select(sources.c.name)
                .where(sources.c.username == username)
                .order_by(sources.c.name)
            ).all()
        return [r[0] for r in rows]

    def list_sources_meta(self, username: str) -> List[dict]:
        """Names + last-saved timestamps, for UIs that show source metadata."""
        sources = self._sources
        with self._engine.connect() as conn:
            rows = conn.execute(
                self._sa.select(sources.c.name, sources.c.updated_at)
                .where(sources.c.username == username)
                .order_by(sources.c.name)
            ).all()
        return [{"name": r[0], "updated_at": r[1]} for r in rows]

    def get_source(self, username: str, name: str) -> Optional[dict]:
        sources = self._sources
        with self._engine.connect() as conn:
            row = conn.execute(
                self._sa.select(
                    sources.c.conn_enc, sources.c.sql,
                    sources.c.kind, sources.c.config,
                )
                .where(
                    (sources.c.username == username) & (sources.c.name == name)
                )
            ).first()
        if not row:
            return None
        return {
            "conn_url": self._fernet.decrypt(_b(row[0])).decode("utf-8"),
            "sql": row[1],
            "kind": row[2] or "sql",
            "config": json.loads(row[3]) if row[3] else None,
        }

    def delete_source(self, username: str, name: str) -> None:
        sources = self._sources
        with self._engine.begin() as conn:
            conn.execute(
                sources.delete().where(
                    (sources.c.username == username) & (sources.c.name == name)
                )
            )


class SqlTokenStore:
    """Per-user OAuth refresh tokens on a SQL database (via SQLAlchemy).

    The public API is identical to :class:`~cx_connectors.store.TokenStore`.
    Scopes are stored space-joined and returned as a list, matching the stdlib
    store's behavior.
    """

    def __init__(self, url: str, encryption_key: str, engine=None):
        from cryptography.fernet import Fernet

        self._fernet = Fernet(encryption_key.encode("utf-8"))
        sa = _sqlalchemy()
        self._sa = sa
        self._engine = engine or sa.create_engine(normalize_sql_url(url), future=True)
        metadata = sa.MetaData()
        self._tokens = sa.Table(
            "cxc_tokens",
            metadata,
            sa.Column("user_id", sa.Text, primary_key=True),
            sa.Column("refresh_enc", sa.LargeBinary, nullable=False),
            sa.Column("token_uri", sa.Text, nullable=False),
            sa.Column("client_id", sa.Text, nullable=False),
            sa.Column("client_secret", sa.Text, nullable=False),
            sa.Column("scopes", sa.Text, nullable=False),
            sa.Column("email", sa.Text),
        )
        metadata.create_all(self._engine)

    def save(self, user_id, refresh_token, token_uri, client_id,
             client_secret, scopes, email=None):
        refresh_enc = self._fernet.encrypt(refresh_token.encode("utf-8"))
        tokens = self._tokens
        try:
            with self._engine.begin() as conn:
                conn.execute(tokens.insert().values(
                    user_id=user_id, refresh_enc=refresh_enc,
                    token_uri=token_uri, client_id=client_id,
                    client_secret=client_secret, scopes=" ".join(scopes),
                    email=email,
                ))
        except self._sa.exc.IntegrityError:
            with self._engine.begin() as conn:
                conn.execute(
                    tokens.update()
                    .where(tokens.c.user_id == user_id)
                    .values(
                        refresh_enc=refresh_enc, token_uri=token_uri,
                        client_id=client_id, client_secret=client_secret,
                        scopes=" ".join(scopes), email=email,
                    )
                )

    def load(self, user_id):
        tokens = self._tokens
        with self._engine.connect() as conn:
            row = conn.execute(
                self._sa.select(
                    tokens.c.refresh_enc, tokens.c.token_uri,
                    tokens.c.client_id, tokens.c.client_secret,
                    tokens.c.scopes, tokens.c.email,
                ).where(tokens.c.user_id == user_id)
            ).first()
        if not row:
            return None
        return {
            "refresh_token": self._fernet.decrypt(_b(row[0])).decode("utf-8"),
            "token_uri": row[1],
            "client_id": row[2],
            "client_secret": row[3],
            "scopes": row[4].split(),
            "email": row[5],
        }

    def delete(self, user_id):
        tokens = self._tokens
        with self._engine.begin() as conn:
            conn.execute(tokens.delete().where(tokens.c.user_id == user_id))


def open_store(target: str, encryption_key: str) -> object:
    """Return a store backed by the right engine for *target*.

    ``postgres://``, ``postgresql://``, ``postgresql+<driver>://`` or
    ``sqlite://`` → :class:`SqlStore`.  A bare path or ``file://`` →
    the stdlib :class:`~cx_connectors.store.Store` (unchanged default).

    :param target: A database URL or a bare SQLite file path.
    :param encryption_key: Fernet key for connection-string encryption.
    """
    from urllib.parse import urlparse

    scheme = urlparse(target).scheme if target else ""
    if scheme in ("postgres", "postgresql", "sqlite") or scheme.startswith("postgresql+"):
        return SqlStore(target, encryption_key)
    if scheme == "file":
        from .store import Store
        return Store(urlparse(target).path or target, encryption_key)
    from .store import Store
    return Store(target, encryption_key)


def open_token_store(target: str, encryption_key: str) -> object:
    """Return a token store backed by the right engine for *target*.

    Scheme dispatch mirrors :func:`open_store`.
    """
    from urllib.parse import urlparse

    scheme = urlparse(target).scheme if target else ""
    if scheme in ("postgres", "postgresql", "sqlite") or scheme.startswith("postgresql+"):
        return SqlTokenStore(target, encryption_key)
    if scheme == "file":
        from .store import TokenStore
        return TokenStore(urlparse(target).path or target, encryption_key)
    from .store import TokenStore
    return TokenStore(target, encryption_key)


def _b(value) -> bytes:
    """Normalize a driver binary value (memoryview/bytes) to bytes."""
    return value.tobytes() if isinstance(value, memoryview) else bytes(value)


def _sqlalchemy():
    try:
        import sqlalchemy  # noqa: WPS433 — optional 'sql' extra, imported on demand
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "the sql store requires SQLAlchemy (install the 'sql' extra: "
            "pip install 'canvasxpress-connectors[sql]')"
        ) from exc
    return sqlalchemy
