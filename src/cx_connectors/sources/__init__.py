"""Data-source adapters. Each returns ``(header, rows)`` for ``reshape.rows_to_cx``."""

from .base import DataSource, to_cx

__all__ = ["DataSource", "to_cx", "SqlSource", "GoogleSheetsSource",
           "GoogleAnalyticsSource", "SalesforceSource", "ServiceNowSource",
           "PackedMatrixSource"]


def __getattr__(name):
    # Lazy re-export so importing this package doesn't pull SQLAlchemy / Google libs
    # unless the adapter that needs them is actually used.
    if name == "SqlSource":
        from .sql import SqlSource
        return SqlSource
    if name == "PackedMatrixSource":
        from .packed import PackedMatrixSource
        return PackedMatrixSource
    if name == "GoogleSheetsSource":
        from .google_sheets import GoogleSheetsSource
        return GoogleSheetsSource
    if name == "GoogleAnalyticsSource":
        from .google_analytics import GoogleAnalyticsSource
        return GoogleAnalyticsSource
    if name == "SalesforceSource":
        from .salesforce import SalesforceSource
        return SalesforceSource
    if name == "ServiceNowSource":
        from .servicenow import ServiceNowSource
        return ServiceNowSource
    raise AttributeError(name)
