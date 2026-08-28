"""Google Analytics 4 (GA4) data source.

Runs a GA4 **Data API** ``runReport`` (dimensions + metrics over a date range) and returns
``(header, rows)`` so ``reshape.rows_to_cx`` can turn it into a CanvasXpress object. Like the
Sheets source, it needs an already-obtained ``google.oauth2`` Credentials object (service
account or user OAuth) — the auth *flow* lives in your app / the web layer, keeping this class
usable from a script, a job, or a web request alike.

The GA4 report *is* the query: the caller names the dimensions and metrics (server-side
config, never the browser). A GA4 report is inherently read-only, so there is no SELECT guard
to mirror — the API cannot mutate data.

The first requested dimension becomes the sample axis (``y.smps``); metrics are numeric and
become variables (``y.vars``/``y.data``); any further dimensions become per-sample annotations
(``x``). So ``dimensions=["date"], metrics=["activeUsers","sessions"]`` yields a time series of
two variables, and adding ``"sessionDefaultChannelGroup"`` as a second dimension annotates each
point with its channel.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple


class GoogleAnalyticsSource:
    """A GA4 Data API ``runReport`` over one property, reshaped to ``(header, rows)``."""

    def __init__(self, credentials, property_id: str, dimensions: Sequence[str],
                 metrics: Sequence[str], start_date: str = "28daysAgo",
                 end_date: str = "today", limit: Optional[int] = None, client=None):
        """
        :param credentials: A ready ``google.oauth2`` Credentials object (service account or
            user OAuth) with the Analytics read scope. Ignored when ``client`` is injected.
        :param property_id: The GA4 property id, digits only (e.g. ``"123456789"``); a
            ``"properties/"`` prefix is added if absent.
        :param dimensions: GA4 dimension API names; the first is the sample axis.
        :param metrics: GA4 metric API names; each becomes a numeric variable.
        :param start_date: Report range start (GA4 date or relative like ``"28daysAgo"``).
        :param end_date: Report range end (GA4 date or relative like ``"today"``).
        :param limit: Optional max rows to request from the API.
        :param client: Inject a prebuilt Data API client (used in tests); when ``None`` a
            ``BetaAnalyticsDataClient`` is built lazily from ``credentials``.
        """
        self.credentials = credentials
        self.property_id = property_id
        self.dimensions = list(dimensions)
        self.metrics = list(metrics)
        self.start_date = start_date
        self.end_date = end_date
        self.limit = limit
        self._client = client  # inject a prebuilt Data API client (used in tests)

    def read(self) -> Tuple[Sequence[str], Sequence[Sequence[Any]]]:
        """Run the report and return ``(header, rows)`` — dimension columns then metric columns."""
        response = self._run_report()
        header: List[str] = (
            [h.name for h in response.dimension_headers]
            + [h.name for h in response.metric_headers]
        )
        rows: List[List[Any]] = []
        for row in getattr(response, "rows", None) or []:
            values = [d.value for d in row.dimension_values]
            values += [m.value for m in row.metric_values]
            rows.append(values)
        return header, rows

    def _run_report(self):
        """Call the GA4 Data API (or the injected client) and return the runReport response.

        :returns: A response exposing ``dimension_headers``, ``metric_headers`` and ``rows``,
            each header having ``.name`` and each row value having ``.value``.
        """
        prop = self.property_id
        if not prop.startswith("properties/"):
            prop = "properties/" + prop

        # An injected client (tests) needs no Google libraries at all — pass a plain-dict
        # request it can inspect or ignore, so the reshape path is testable offline.
        if self._client is not None:
            return self._client.run_report({
                "property": prop,
                "dimensions": self.dimensions,
                "metrics": self.metrics,
                "date_range": (self.start_date, self.end_date),
                "limit": self.limit,
            })

        # Lazy import so the core package doesn't require the Analytics libraries.
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            DateRange,
            Dimension,
            Metric,
            RunReportRequest,
        )

        client = BetaAnalyticsDataClient(credentials=self.credentials)
        request = RunReportRequest(
            property=prop,
            dimensions=[Dimension(name=name) for name in self.dimensions],
            metrics=[Metric(name=name) for name in self.metrics],
            date_ranges=[DateRange(start_date=self.start_date, end_date=self.end_date)],
            limit=self.limit,
        )
        return client.run_report(request)
