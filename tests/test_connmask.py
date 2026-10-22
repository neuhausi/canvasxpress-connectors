"""Masking a connection URL's secrets, restoring them on save, and naming a source's type."""

import pytest

from cx_connectors.connmask import MASK, mask_conn_url, unmask_conn_url
from cx_connectors.kinds import connector_type

PAT = "dapi0123456789abcdef"
PG = "postgresql+psycopg://app:s3cr3t-pw@db.example.com:5432/sales?sslmode=require"
DBX = "databricks://token:%s@dbc-1.cloud.databricks.com?http_path=/sql/1.0/warehouses/abc&catalog=main&schema=default" % PAT
ODBC = "mssql+pyodbc://sa:p%40ss@sql.example.com/db?odbc_connect=DRIVER%3D%7BODBC+Driver+18%7D%3BPWD%3Dhunter2%3BSERVER%3Dx"


# ---- masking ------------------------------------------------------------------------------

def test_the_password_is_masked_and_everything_else_is_kept():
    assert mask_conn_url(PG) == "postgresql+psycopg://app:%s@db.example.com:5432/sales?sslmode=require" % MASK


def test_a_databricks_token_is_masked_and_its_http_path_is_kept():
    masked = mask_conn_url(DBX)
    assert PAT not in masked
    assert masked.startswith("databricks://token:%s@dbc-1.cloud.databricks.com" % MASK)
    assert "http_path=/sql/1.0/warehouses/abc" in masked and "catalog=main" in masked


def test_secret_looking_query_parameters_are_masked():
    masked = mask_conn_url("snowflake://u:pw@acct/db?private_key=KEYDATA&access_token=TOK&warehouse=WH")
    assert "KEYDATA" not in masked and "TOK" not in masked and "pw@" not in masked
    assert "warehouse=WH" in masked


def test_a_password_inside_an_odbc_connect_value_is_masked():
    masked = mask_conn_url(ODBC)
    assert "hunter2" not in masked and "p%40ss" not in masked
    assert "SERVER" in masked.replace("%3B", ";").replace("%3D", "=")      # the rest is still there


def test_a_secret_only_inside_an_odbc_value_still_round_trips():
    """The mask must survive percent-encoding, or the form would send back text with no
    ``********`` in it and the server would store the mask as the password."""
    only_odbc = "mssql+pyodbc://sql.example.com/db?odbc_connect=DRIVER%3D%7BX%7D%3BPWD%3Dhunter2%3BSERVER%3Dx"
    masked = mask_conn_url(only_odbc)
    assert "hunter2" not in masked and MASK in masked
    assert unmask_conn_url(masked, only_odbc) == only_odbc
    edited = masked.replace("sql.example.com/db", "sql.example.com/other")
    assert unmask_conn_url(edited, only_odbc) == only_odbc.replace("/db?", "/other?")


@pytest.mark.parametrize("url", [
    "sqlite:///path/to/file.db", "duckdb:///:memory:", "postgresql://app@db.example.com/sales",
    "databricks://posit-connect@dbc-1.cloud.databricks.com?http_path=/sql/1.0/warehouses/abc",
    "not a url at all", "",
])
def test_urls_with_nothing_secret_are_unchanged(url):
    assert mask_conn_url(url) == url


def test_masking_is_idempotent():
    assert mask_conn_url(mask_conn_url(PG)) == mask_conn_url(PG)


# ---- restoring ----------------------------------------------------------------------------

def test_an_untouched_masked_url_gets_the_stored_url_back_exactly():
    assert unmask_conn_url(mask_conn_url(PG), PG) == PG
    assert unmask_conn_url(mask_conn_url(DBX), DBX) == DBX
    assert unmask_conn_url(mask_conn_url(ODBC), ODBC) == ODBC


def test_a_url_without_a_mask_is_used_as_typed():
    typed = "postgresql://app:brand-new@db.example.com/sales"
    assert unmask_conn_url(typed, PG) == typed
    assert unmask_conn_url(typed, None) == typed


def test_editing_other_parts_keeps_the_stored_secret():
    edited = "postgresql+psycopg://app:%s@db.example.com:5432/reports?sslmode=require" % MASK
    assert unmask_conn_url(edited, PG) == "postgresql+psycopg://app:s3cr3t-pw@db.example.com:5432/reports?sslmode=require"
    renamed = mask_conn_url(DBX).replace("catalog=main", "catalog=other")
    assert unmask_conn_url(renamed, DBX) == DBX.replace("catalog=main", "catalog=other")


def test_a_masked_url_for_another_server_is_refused_so_the_secret_cannot_be_redirected():
    for evil in ("postgresql+psycopg://app:%s@attacker.example/db" % MASK,         # other host
                 "postgresql+psycopg://app:%s@db.example.com:6543/sales" % MASK,    # other port
                 "mysql://app:%s@db.example.com:5432/sales" % MASK):                # other scheme
        with pytest.raises(ValueError, match="Re-enter the password"):
            unmask_conn_url(evil, PG)


def test_a_mask_with_nothing_to_take_it_from_is_an_error():
    with pytest.raises(ValueError):
        unmask_conn_url("postgresql://app:%s@h/db" % MASK, None)
    with pytest.raises(ValueError, match="none to reuse"):
        unmask_conn_url("postgresql://app:%s@h/db" % MASK, "postgresql://app@h/db?x=1")


def test_a_stored_secret_in_a_query_parameter_is_restored():
    stored = "snowflake://u@acct/db?private_key=KEYDATA&warehouse=WH"
    assert unmask_conn_url(mask_conn_url(stored).replace("WH", "WH2"), stored) == stored.replace("WH", "WH2")


def test_a_mangled_mask_is_never_passed_through():
    with pytest.raises(ValueError):
        unmask_conn_url("postgresql+psycopg://app:%s@db.example.com:5432/sales?sslmode=require&token=%s" % (MASK, MASK), PG)


# ---- naming the type ----------------------------------------------------------------------

@pytest.mark.parametrize("kind, url, expected", [
    ("salesforce", '{"oauth_app": "default"}', "salesforce"),
    ("servicenow", "{}", "servicenow"),
    ("pins", "{}", "pins"),
    ("packed", "sqlite:///x.db", "packed"),
    ("sql", DBX, "databricks"),
    ("sql", "databricks+connector://token:t@h?http_path=/p", "databricks"),
    ("sql", "databricks://posit-connect@h?http_path=/p", "databricks-viewer"),
    ("sql", PG, "postgresql"),
    (None, "snowflake://u:p@a/d", "snowflake"),
    ("", "sqlite:///f.db", "sqlite"),
    ("sql", "", "sql"),
    ("SQL", "mysql+pymysql://u:p@h/d", "mysql"),
])
def test_connector_type(kind, url, expected):
    assert connector_type(kind, url) == expected
