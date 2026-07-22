"""Google Sheets data source.

Reads a cell range from the Sheets API v4 using already-obtained credentials, and
returns ``(header, rows)``. The OAuth *flow* (getting those credentials) lives in the
web layer / your app — a source only needs a ready ``google.oauth2`` Credentials
object, keeping this class usable from scripts, jobs, or a web request alike.
"""

from __future__ import annotations

from typing import Any, List, Sequence, Tuple


class GoogleSheetsSource:
    def __init__(self, credentials, spreadsheet_id: str, cell_range: str = "A1:Z1000",
                 service=None):
        self.credentials = credentials
        self.spreadsheet_id = spreadsheet_id
        self.cell_range = cell_range
        self._service = service  # inject a prebuilt Sheets service (used in tests)

    def read(self) -> Tuple[Sequence[str], Sequence[Sequence[Any]]]:
        sheets = self._service
        if sheets is None:
            # Lazy import so the core package doesn't require the Google libraries.
            from googleapiclient.discovery import build

            sheets = build("sheets", "v4", credentials=self.credentials,
                           cache_discovery=False)
        resp = (
            sheets.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=self.cell_range)
            .execute()
        )
        values = resp.get("values", [])
        if not values:
            return [], []
        header = values[0]
        ncols = len(header)
        # Sheets omits trailing empty cells; pad so every row is full width.
        rows: List[List[Any]] = [row + [""] * (ncols - len(row)) for row in values[1:]]
        return header, rows
