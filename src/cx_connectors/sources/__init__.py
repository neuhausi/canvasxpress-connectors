"""Data-source adapters. Each returns ``(header, rows)`` for ``reshape.rows_to_cx``."""

from .base import DataSource, to_cx

__all__ = ["DataSource", "to_cx", "SqlSource", "GoogleSheetsSource",
           "GoogleAnalyticsSource", "SalesforceSource", "ServiceNowSource",
           "YahooFinanceSource", "YahooOptionsSource", "StooqSource",
           "AlphaVantageSource", "AlphaVantageOptionsSource", "NasdaqOptionsSource",
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
    if name == "YahooFinanceSource":
        from .yahoo_finance import YahooFinanceSource
        return YahooFinanceSource
    if name == "YahooOptionsSource":
        from .yahoo_finance import YahooOptionsSource
        return YahooOptionsSource
    if name == "StooqSource":
        from .stooq import StooqSource
        return StooqSource
    if name == "AlphaVantageSource":
        from .alphavantage import AlphaVantageSource
        return AlphaVantageSource
    if name == "AlphaVantageOptionsSource":
        from .alphavantage import AlphaVantageOptionsSource
        return AlphaVantageOptionsSource
    if name == "NasdaqOptionsSource":
        from .nasdaq import NasdaqOptionsSource
        return NasdaqOptionsSource
    raise AttributeError(name)
