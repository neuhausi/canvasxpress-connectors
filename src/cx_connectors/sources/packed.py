"""Packed-matrix data source — expands a column-store SQLite into a CanvasXpress
data object.

Some CanvasXpress reference databases (CCLE, TCGA) store an expression / copy-number
matrix in a *packed* form for fast single-gene retrieval:

* one row per gene: ``[gene, <JSON array of values across all samples>]`` in a fixed
  sorted-sample order (indexed by gene name), and
* a shared ``json`` table holding the sample axis + annotations once, as a template
  ``{"data": {"y": {"smps": [...], "vars": [], "data": []}, "x": {...}}}``.

A plain ``SELECT`` can't reassemble that (two queries + JSON-decode + reshape), so this
source does it: load the template, fetch the requested genes' packed arrays, and append
each as a ``vars``/``data`` row — yielding a CanvasXpress object ready for a boxplot,
violin, or heatmap grouped by any annotation (disease, lineage, …). This mirrors the
original ``ccleServices.pl`` / ``tcgaServices.pl`` boxplot+heatmap logic.

The table/column identifiers come from server-side source config (never the browser) and
are validated as SQL identifiers; the gene values are always **bound** parameters.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str, what: str) -> str:
    """Validate a SQL identifier (table/column name) from trusted config.

    :param name: The identifier.
    :param what: Human label for the error.
    :returns: The identifier unchanged.
    :raises ValueError: If it is not a bare identifier.
    """
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError("invalid %s identifier: %r" % (what, name))
    return name


class PackedMatrixSource:
    """Reassemble a packed gene×sample matrix into a CanvasXpress data object.

    :param conn_url: SQLAlchemy connection URL (read-only recommended: ``…?mode=ro``).
    :param table: The packed table (e.g. ``rnaseq`` / ``cnv``).
    :param value_col: The packed BLOB column (e.g. ``log2tpm`` / ``cnratio``) — a JSON
        array of per-sample values aligned to the template's ``smps``.
    :param template_key: The template's key value (e.g. ``rna1``). ``None`` means the
        template table holds a single template row (no key lookup) — the GTEx shape.
    :param name_col: The gene-name column in ``table`` (default ``name``).
    :param json_table: The template table (default ``json``).
    :param genes: Gene names to include (order preserved; missing genes skipped).
    :param max_genes: Cap on how many genes to expand (guards a huge heatmap request).
    :param template_col: The template-JSON column in ``json_table`` (default ``str``;
        GTEx uses ``samples``).
    :param template_key_col: The key column (default ``key``); ignored when
        ``template_key`` is ``None``.
    :param value_encoding: How a packed value is stored — ``"json"`` (a JSON array, the
        CCLE/TCGA shape) or ``"delimited"`` (a ``value_sep``-joined string, GTEx's
        ``;``-separated tpm).
    :param value_sep: The separator for ``value_encoding="delimited"`` (default ``;``).
    """

    def __init__(self, conn_url: str, table: str, value_col: str, template_key: Optional[str],
                 name_col: str = "name", json_table: str = "json",
                 genes: Optional[List[str]] = None, max_genes: int = 200,
                 template_col: str = "str", template_key_col: str = "key",
                 value_encoding: str = "json", value_sep: str = ";"):
        self.conn_url = conn_url
        self.table = _ident(table, "table")
        self.value_col = _ident(value_col, "value_col")
        self.name_col = _ident(name_col, "name_col")
        self.json_table = _ident(json_table, "json_table")
        self.template_col = _ident(template_col, "template_col")
        self.template_key_col = _ident(template_key_col, "template_key_col")
        self.template_key = template_key
        self.genes = [g for g in (genes or []) if g]
        self.max_genes = max_genes
        if value_encoding not in ("json", "delimited"):
            raise ValueError("value_encoding must be 'json' or 'delimited'")
        self.value_encoding = value_encoding
        self.value_sep = value_sep

    def _decode(self, packed):
        """Decode a packed cell into a list of values per the configured encoding."""
        if self.value_encoding == "json":
            return json.loads(packed)
        out = []
        for part in str(packed).split(self.value_sep):
            part = part.strip()
            if part == "" or part.lower() in ("na", "null"):
                out.append(None)
            else:
                try:
                    out.append(float(part))
                except ValueError:
                    out.append(part)
        return out

    def read_cx(self) -> dict:
        """Return the assembled CanvasXpress ``data`` object (``{y:{…}, x:{…}}``).

        :returns: The template's ``data`` with the requested genes appended as
            ``y.vars`` / ``y.data`` rows (empty ``vars``/``data`` when no gene matched).
        :raises ValueError: If the template is absent.
        """
        from sqlalchemy import create_engine, text

        engine = create_engine(self.conn_url, future=True)
        try:
            with engine.connect() as conn:
                if self.template_key is None:
                    tsql = "SELECT %s FROM %s LIMIT 1" % (self.template_col, self.json_table)
                    trow = conn.execute(text(tsql)).fetchone()
                else:
                    tsql = "SELECT %s FROM %s WHERE %s = :k" % (
                        self.template_col, self.json_table, self.template_key_col)
                    trow = conn.execute(text(tsql), {"k": self.template_key}).fetchone()
                if not trow:
                    raise ValueError("no template (%r) in %s" % (self.template_key, self.json_table))
                obj = json.loads(trow[0])
                y = obj.setdefault("data", {}).setdefault("y", {})
                y.setdefault("vars", [])
                y.setdefault("data", [])

                genes = self.genes[: self.max_genes]
                if genes:
                    placeholders = ",".join(":g%d" % i for i in range(len(genes)))
                    params = {"g%d" % i: g for i, g in enumerate(genes)}
                    sql = "SELECT %s, %s FROM %s WHERE %s IN (%s)" % (
                        self.name_col, self.value_col, self.table, self.name_col, placeholders,
                    )
                    rows = conn.execute(text(sql), params).fetchall()
                    by_name = {r[0]: r[1] for r in rows}
                    for gene in genes:               # preserve requested order
                        packed = by_name.get(gene)
                        if packed is not None:
                            y["vars"].append(gene)
                            y["data"].append(self._decode(packed))
        finally:
            engine.dispose()
        return obj["data"]
