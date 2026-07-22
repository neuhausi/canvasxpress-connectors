"""Data-source adapters. Each returns ``(header, rows)`` for ``reshape.rows_to_cx``."""

from .base import DataSource, to_cx

__all__ = ["DataSource", "to_cx", "SqlSource", "GoogleSheetsSource"]


def __getattr__(name):
    # Lazy re-export so importing this package doesn't pull SQLAlchemy / Google libs
    # unless the adapter that needs them is actually used.
    if name == "SqlSource":
        from .sql import SqlSource
        return SqlSource
    if name == "GoogleSheetsSource":
        from .google_sheets import GoogleSheetsSource
        return GoogleSheetsSource
    raise AttributeError(name)
