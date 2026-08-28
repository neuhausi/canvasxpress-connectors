"""Register (or update) named database sources for a user, in bulk, from a
JSON config — so a deployment can seed its live-data sources without clicking
through the /connectors UI or hand-writing store code.

    python register_sources.py sources.json \
        --db /srv/cxd/connectors.db \
        --key "$ENCRYPTION_KEY"

The DB files themselves stay wherever they already live on the server; only the
(encrypted) connection URL + SQL are stored. This is the server-side counterpart
to a canvasxpress-dashboards `mode:"param"` control: declare `:name` bind
parameters in the SQL and a dashboard `"query": { "region": "$region" }` maps
onto them (see the connectors README "Parameterized queries").

Config format (JSON):

    {
      "user": "alice",
      "password": "optional — only needed to CREATE the user if absent",
      "sources": [
        {
          "name": "sales",
          "url": "sqlite:///file:/srv/data/sales.sqlite?mode=ro&uri=true",
          "sql": "SELECT sample, revenue FROM sales
                  WHERE (:region IS NULL OR region = :region) ORDER BY sample"
        },
        {
          "name": "genes",
          "path": "/srv/data/genes.sqlite",        // shortcut → read-only sqlite URL
          "sql_file": "queries/genes.sql"          // SQL from a file (relative to the config)
        }
      ]
    }

`url` is any SQLAlchemy connection URL; `path` is a convenience that expands to a
read-only SQLite URL. `sql` is inline; `sql_file` reads it from a path relative
to the config file. Re-running is idempotent — a source with an existing name is
updated in place.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _sqlite_ro_url(path: str) -> str:
    """A read-only SQLite SQLAlchemy URL for an absolute file path (mode=ro
    blocks any write at the driver)."""
    return "sqlite:///file:" + os.path.abspath(path) + "?mode=ro&uri=true"


def _resolve_sql(entry: dict, config_dir: str) -> str:
    """Return an entry's SQL, from inline `sql` or a `sql_file` relative to the
    config directory."""
    if entry.get("sql"):
        return entry["sql"]
    if entry.get("sql_file"):
        with open(os.path.join(config_dir, entry["sql_file"]), encoding="utf-8") as handle:
            return handle.read()
    raise ValueError("source %r needs either 'sql' or 'sql_file'" % entry.get("name"))


def _resolve_url(entry: dict) -> str:
    """Return an entry's connection URL, from `url` or a `path` shortcut."""
    if entry.get("url"):
        return entry["url"]
    if entry.get("path"):
        return _sqlite_ro_url(entry["path"])
    raise ValueError("source %r needs either 'url' or 'path'" % entry.get("name"))


def register(config: dict, config_dir: str, db_path: str, key: str,
             dry_run: bool = False) -> int:
    """Register every source in `config` into the connectors store at `db_path`.

    :param config: The parsed config (`user`, optional `password`, `sources`).
    :param config_dir: Directory of the config file (for `sql_file` paths).
    :param db_path: Path to the connectors store SQLite DB.
    :param key: The store's Fernet ENCRYPTION_KEY.
    :param dry_run: When True, validate + print but write nothing.
    :returns: Count of sources registered.
    """
    from cx_connectors.sources.sql import SqlSource, bind_param_names
    from cx_connectors.store import Store

    user = config.get("user")
    if not user:
        raise ValueError("config needs a 'user'")
    sources = config.get("sources") or []

    store = Store(db_path, key)
    # Ensure the user exists. create_user is a no-op (returns False) if present;
    # creating a NEW user needs a password.
    if config.get("password"):
        created = store.create_user(user, config["password"])
        if created:
            print("  created user %r" % user)

    count = 0
    for entry in sources:
        name = entry.get("name")
        if not name:
            raise ValueError("every source needs a 'name'")
        url = _resolve_url(entry)
        sql = _resolve_sql(entry, config_dir)
        # Validate read-only + surface the bind params the query declares (these
        # are the dashboard params the source can consume).
        SqlSource(url, sql)  # raises ReadOnlyViolation on anything but a SELECT
        binds = bind_param_names(sql)
        safe_url = url.split("://", 1)[0] + "://…"   # never print credentials/paths
        print("  %-24s %-14s params=%s" % (name, safe_url, binds or "(none)"))
        if not dry_run:
            store.save_source(user, name, url, sql)
        count += 1
    return count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Bulk-register connector sources from JSON.")
    parser.add_argument("config", help="Path to the JSON config file.")
    parser.add_argument("--db", default=os.getenv("APP_DB_PATH", "app.db"),
                        help="Connectors store DB path (default: $APP_DB_PATH or app.db).")
    parser.add_argument("--key", default=os.getenv("ENCRYPTION_KEY"),
                        help="Store Fernet key (default: $ENCRYPTION_KEY).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate and print, but write nothing.")
    args = parser.parse_args(argv)

    if not args.key:
        parser.error("no encryption key: pass --key or set ENCRYPTION_KEY")

    with open(args.config, encoding="utf-8") as handle:
        config = json.load(handle)
    config_dir = os.path.dirname(os.path.abspath(args.config))

    print(("[dry-run] " if args.dry_run else "") + "registering sources into %s" % args.db)
    count = register(config, config_dir, args.db, args.key, dry_run=args.dry_run)
    print(("[dry-run] " if args.dry_run else "") + "done — %d source(s)." % count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
