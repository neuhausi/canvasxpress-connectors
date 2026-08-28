"""Tests for the Salesforce (SOQL) source.

The Salesforce API needs simple-salesforce + real credentials and can't run in CI, so we
test the reshape via an injected fake client — the same approach as the Sheets/GA4 sources.
"""

import pytest

from cx_connectors.sources.base import to_cx
from cx_connectors.sources.salesforce import (
    ReadOnlyViolation,
    SalesforceSource,
    soql_field_names,
)


class _FakeSalesforce:
    """Mimics simple_salesforce.Salesforce.query_all(soql) -> {'records': [...]}."""
    def __init__(self, records):
        self._records = records

    def query_all(self, soql):
        self.soql = soql
        return {"totalSize": len(self._records), "done": True, "records": self._records}


def test_soql_field_names_parses_select_list():
    assert soql_field_names("SELECT Id, Name, Amount FROM Opportunity") == \
        ["Id", "Name", "Amount"]


def test_salesforce_source_reshapes_via_injected_client():
    records = [
        {"attributes": {"type": "Opportunity"}, "Name": "Deal A", "Amount": "1000",
         "StageName": "Won"},
        {"attributes": {"type": "Opportunity"}, "Name": "Deal B", "Amount": "2500",
         "StageName": "Lost"},
    ]
    src = SalesforceSource(
        "SELECT Name, Amount, StageName FROM Opportunity",
        client=_FakeSalesforce(records),
    )
    cx = to_cx(src)
    assert cx["y"]["smps"] == ["Deal A", "Deal B"]        # first field = sample axis
    assert cx["y"]["vars"] == ["Amount"]                  # numeric metric
    assert cx["y"]["data"] == [[1000.0, 2500.0]]
    assert cx["x"]["StageName"] == ["Won", "Lost"]        # text annotation


def test_salesforce_source_reads_relationship_fields():
    records = [
        {"attributes": {}, "Name": "Deal A", "Account": {"attributes": {}, "Name": "Acme"}},
        {"attributes": {}, "Name": "Deal B", "Account": None},   # null relationship -> ""
    ]
    header, rows = SalesforceSource(
        "SELECT Name, Account.Name FROM Opportunity",
        client=_FakeSalesforce(records),
    ).read()
    assert header == ["Name", "Account.Name"]
    assert rows == [["Deal A", "Acme"], ["Deal B", ""]]


def test_salesforce_source_rejects_non_select():
    with pytest.raises(ReadOnlyViolation):
        SalesforceSource("DELETE FROM Opportunity", client=_FakeSalesforce([]))
