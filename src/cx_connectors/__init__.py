"""canvasxpress-connectors — feed CanvasXpress from authenticated data sources.

Quick use::

    from cx_connectors.sources import SqlSource
    from cx_connectors.sources.base import to_cx

    data = to_cx(SqlSource("sqlite:///demo.db", "SELECT sample, geneA FROM t"))
    # -> {"y": {"vars": [...], "smps": [...], "data": [...]}, "x": {...}}
    # hand `data` to a JSON endpoint; the browser passes it to new CanvasXpress(...)

Core (``reshape``, ``store``, ``sources.base``) needs only ``cryptography``. The SQL
and Google Sheets adapters and the web app factory pull their deps lazily; install the
matching extra (``pip install "canvasxpress-connectors[sql]"`` etc.).
"""

from .reshape import rows_to_cx

__version__ = "0.1.0"
__all__ = ["rows_to_cx", "__version__"]
