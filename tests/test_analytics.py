"""Tests for the Google Analytics 4 source.

The GA4 Data API needs Google libraries + real credentials and can't run in CI, so we test
the reshape via an injected fake Data API client — the same approach as the Sheets source.
"""

from cx_connectors.sources.base import to_cx
from cx_connectors.sources.google_analytics import GoogleAnalyticsSource


class _Named:
    def __init__(self, name):
        self.name = name


class _Val:
    def __init__(self, value):
        self.value = value


class _Row:
    def __init__(self, dims, mets):
        self.dimension_values = [_Val(v) for v in dims]
        self.metric_values = [_Val(v) for v in mets]


class _FakeResponse:
    def __init__(self, dim_names, met_names, rows):
        self.dimension_headers = [_Named(n) for n in dim_names]
        self.metric_headers = [_Named(n) for n in met_names]
        self.rows = [_Row(d, m) for (d, m) in rows]


class _FakeClient:
    """Mimics BetaAnalyticsDataClient.run_report(request) -> response."""
    def __init__(self, response):
        self._response = response

    def run_report(self, request):
        self.request = request          # captured so the test can assert the query shape
        return self._response


def test_ga4_source_reshapes_via_injected_client():
    # date (dimension, sample axis) + two numeric metrics -> a two-variable time series.
    resp = _FakeResponse(
        ["date"], ["activeUsers", "sessions"],
        [(["20260101"], ["11", "13"]), (["20260102"], ["25", "16"])],
    )
    src = GoogleAnalyticsSource(
        credentials=None, property_id="123456789",
        dimensions=["date"], metrics=["activeUsers", "sessions"],
        client=_FakeClient(resp),
    )
    cx = to_cx(src)
    assert cx["y"]["smps"] == ["20260101", "20260102"]
    assert cx["y"]["vars"] == ["activeUsers", "sessions"]
    assert cx["y"]["data"] == [[11.0, 25.0], [13.0, 16.0]]


def test_ga4_source_extra_dimension_becomes_annotation():
    # A second (non-numeric) dimension annotates each sample under x.
    resp = _FakeResponse(
        ["date", "sessionDefaultChannelGroup"], ["sessions"],
        [(["20260101", "Organic"], ["13"]), (["20260102", "Direct"], ["16"])],
    )
    src = GoogleAnalyticsSource(
        credentials=None, property_id="123456789",
        dimensions=["date", "sessionDefaultChannelGroup"], metrics=["sessions"],
        client=_FakeClient(resp),
    )
    cx = to_cx(src)
    assert cx["y"]["vars"] == ["sessions"]
    assert cx["x"]["sessionDefaultChannelGroup"] == ["Organic", "Direct"]


def test_ga4_source_prefixes_property_and_forwards_query():
    client = _FakeClient(_FakeResponse(["date"], ["sessions"], [(["20260101"], ["13"])]))
    GoogleAnalyticsSource(
        credentials=None, property_id="123456789",
        dimensions=["date"], metrics=["sessions"],
        start_date="7daysAgo", end_date="today", client=client,
    ).read()
    assert client.request["property"] == "properties/123456789"
    assert client.request["date_range"] == ("7daysAgo", "today")
