"""Name the type of a stored source: ``salesforce``, ``databricks``, ``postgresql``, ….

A source's stored ``kind`` only separates the SaaS sources (``salesforce``, ``servicenow``,
``pins``) and the packed-matrix source from "a database" (``sql``). Which database is in the URL
scheme (``databricks://``, ``postgresql+psycopg://``, ``snowflake://`` …), and Databricks comes in
two forms that differ in what they store: a personal access token in the URL, or no credential at
all (sign in as the viewer). :func:`connector_type` folds that into one label for lists, forms and
documentation.
"""

from __future__ import annotations

from typing import Optional

from .databricks_oauth import is_viewer_oauth_url

#: Types that store no credential of their own.
NO_STORED_CREDENTIAL = ("databricks-viewer",)


def connector_type(kind: Optional[str], conn_url: Optional[str] = None) -> str:
    """The type of a stored source.

    :param kind: The stored ``kind`` (``None``/empty means a plain database, ``"sql"``).
    :param conn_url: The stored connection string; only read to tell databases apart.
    :returns: ``salesforce``, ``servicenow``, ``pins``, ``packed``, ``databricks``,
        ``databricks-viewer`` (signs in as the viewer, stores no credential), or the database
        dialect from the URL scheme (``postgresql``, ``mysql``, ``mssql``, ``snowflake``,
        ``bigquery``, ``duckdb``, ``sqlite`` …); ``sql`` when the URL has no scheme.
    """
    kind = (kind or "sql").strip().lower()
    if kind != "sql":
        return kind
    scheme = (conn_url or "").partition("://")[0].strip().lower()
    if not scheme:
        return "sql"
    dialect = scheme.split("+")[0]
    if dialect == "databricks":
        return "databricks-viewer" if is_viewer_oauth_url(conn_url) else "databricks"
    return dialect
