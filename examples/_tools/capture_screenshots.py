"""Render each example's CanvasXpress chart headlessly and save its screenshot.

    pip install playwright && python -m playwright install chromium
    python examples/_tools/capture_screenshots.py

Writes examples/<name>/screenshot.png for each example below. This tool is not part
of the package (it lives outside src/); it just regenerates the README images.
"""

import base64
import json
import os

from playwright.sync_api import sync_playwright

EXAMPLES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Per-example data + title. sqlite/postgres share the 8-sample table; google_sheets and
# byo_database use their own demo data so each image reflects that example.
EIGHT = {
    "y": {
        "vars": ["GeneA", "GeneB", "GeneC", "GeneD"],
        "smps": ["Sample1", "Sample2", "Sample3", "Sample4",
                 "Sample5", "Sample6", "Sample7", "Sample8"],
        "data": [
            [11, 25, 12, 22, 15, 21, 19, 28],
            [13, 16, 9, 23, 24, 11, 20, 14],
            [14, 17, 10, 25, 24, 17, 13, 22],
            [15, 18, 11, 26, 25, 18, 16, 12],
        ],
    },
    "x": {
        "Category": ["A", "A", "A", "B", "B", "B", "C", "C"],
        "Group": ["X", "X", "Y", "Y", "Z", "Z", "X", "Z"],
    },
}

SHEETS = {
    "y": {
        "vars": ["GeneA", "GeneB", "GeneC", "GeneD"],
        "smps": ["Sample1", "Sample2", "Sample3", "Sample4", "Sample5", "Sample6"],
        "data": [
            [11, 25, 12, 22, 15, 21],
            [13, 16, 9, 23, 24, 11],
            [14, 17, 10, 25, 24, 17],
            [15, 18, 11, 26, 25, 18],
        ],
    },
    "x": {"Category": ["A", "A", "A", "B", "B", "B"], "Group": ["X", "X", "Y", "Y", "Z", "Z"]},
}

BYO = {
    "y": {
        "vars": ["GeneA", "GeneB", "GeneC", "GeneD"],
        "smps": ["Sample1", "Sample2", "Sample3", "Sample4"],
        "data": [[11, 25, 12, 22], [13, 16, 9, 23], [14, 17, 10, 25], [15, 18, 11, 26]],
    },
    "x": {"Category": ["A", "A", "A", "B"], "Group": ["X", "X", "Y", "Y"]},
}

TARGETS = [
    ("sqlite", "SQLite → CanvasXpress", EIGHT),
    ("postgres", "Postgres → CanvasXpress", EIGHT),
    ("google_sheets", "Google Sheets → CanvasXpress", SHEETS),
    ("byo_database", "Your database → CanvasXpress", BYO),
]

PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<link href="https://www.canvasxpress.org/dist/canvasXpress.css" rel="stylesheet">
<script src="https://www.canvasxpress.org/dist/canvasXpress.min.js"></script>
</head><body style="margin:0">
<canvas id="cx" width="900" height="500"></canvas>
</body></html>"""


def capture(page, title, data):
    page.set_content(PAGE)
    # Wait for the CDN's CanvasXpress to actually load before constructing.
    page.wait_for_function("typeof CanvasXpress !== 'undefined'", timeout=20000)
    page.evaluate(
        """(args) => {
            new CanvasXpress("cx", args.data, {
                graphType: "Bar", title: args.title,
                subtitle: "via canvasxpress-connectors",
                // For a vertical Bar, CanvasXpress maps these opposite to the
                // visual axes, so this yields Sample on X and Value on Y.
                xAxisTitle: "Value", yAxisTitle: "Sample",
                graphOrientation: "vertical", showLegend: true
            });
        }""",
        {"data": data, "title": title},
    )
    page.wait_for_timeout(1500)  # let the draw settle
    return page.evaluate("document.getElementById('cx').toDataURL('image/png')")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 960, "height": 560},
                                device_scale_factor=2)
        for name, title, data in TARGETS:
            data_url = capture(page, title, data)
            png = base64.b64decode(data_url.split(",", 1)[1])
            out = os.path.join(EXAMPLES_DIR, name, "screenshot.png")
            with open(out, "wb") as f:
                f.write(png)
            print("wrote %s (%d bytes)" % (out, len(png)))
        browser.close()


if __name__ == "__main__":
    main()
