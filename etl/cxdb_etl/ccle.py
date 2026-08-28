"""Build ``ccle.sqlite`` — CCLE expression / copy-number (packed) + samples,
genes, mutations, and the json/indices templates the packed-matrix source reads.

Python port of ``ccleServices.pl``'s ``update`` pipeline. The heart of it is the
**packed-matrix** reshape: the CCLE CSVs have samples as rows and genes as columns;
this transposes them into one row per gene holding a JSON array of that gene's values
across a *sorted* sample list (stored as a BLOB), paired with a ``json`` template that
carries the sample axis + annotations once. That packed form is what
``cx_connectors.sources.packed.PackedMatrixSource`` expands back into a CanvasXpress
object (Phase 1) — this module writes it (Phase 2).

NOTE ON INPUT SCHEMAS: the sample-info and mutation column indices below mirror the
original 2012/2022 CCLE files (Figshare). Those layouts drift between releases, so
before a real run, confirm the column positions in ``parse_samples`` / ``parse_mutations``
against the current downloads. The packed reshape (``pack_matrix``) and the
json/indices builders are format-independent and covered by tests.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

from .common import build_sqlite

SCHEMA = """
CREATE TABLE [sample] (
  [id] TEXT NOT NULL, [name] TEXT, [sname] TEXT, [cname] TEXT, [gender] TEXT,
  [site] TEXT, [prm_met] TEXT, [disease] TEXT, [lineage] TEXT, [subtype] TEXT,
  [idx] INTEGER NOT NULL
);
CREATE INDEX [sampleid] ON [sample] ([id] ASC);
CREATE INDEX [sname_idx] ON [sample] ([name] ASC);
CREATE INDEX [lineage] ON [sample] ([lineage] ASC);
CREATE TABLE [gene] (
  [id] TEXT NOT NULL, [name] TEXT NOT NULL, [chrom] TEXT NOT NULL,
  [start] INTEGER NOT NULL, [end] INTEGER NOT NULL, [strand] TEXT NOT NULL
);
CREATE INDEX [ensemblid] ON [gene] ([id] ASC);
CREATE INDEX [symbol] ON [gene] ([name] ASC);
CREATE TABLE [cnv] ([name] TEXT NOT NULL, [cnratio] BLOB NOT NULL);
CREATE INDEX [cnv_name] ON [cnv] ([name] ASC);
CREATE TABLE [rnaseq] ([name] TEXT NOT NULL, [log2tpm] BLOB NOT NULL);
CREATE INDEX [rna_name] ON [rnaseq] ([name] ASC);
CREATE TABLE [mutation] (
  [sample] TEXT NOT NULL, [chrom] TEXT NOT NULL, [start] INTEGER NOT NULL,
  [end] INTEGER NOT NULL, [reference] TEXT, [alt] TEXT, [gene] TEXT NOT NULL,
  [transcript] TEXT NOT NULL, [effect] TEXT, [genome_change] TEXT,
  [prot_change] TEXT, [idx] INTEGER NOT NULL
);
CREATE INDEX [mut_sample] ON [mutation] ([sample] ASC);
CREATE INDEX [mut_gene] ON [mutation] ([gene] ASC);
CREATE TABLE [json] ([key] TEXT NOT NULL, [str] BLOB NOT NULL);
CREATE TABLE [indices] ([key] TEXT NOT NULL, [str] BLOB NOT NULL);
"""

_X_KEYS = ["name", "gender", "site", "prm_met", "disease", "lineage"]


def pack_matrix(csv_path: str, value_fmt: str = "%.3f") -> Tuple[List[str], List[str], List[str]]:
    """Transpose a samples-as-rows / genes-as-columns CSV into packed per-gene arrays.

    :param csv_path: CSV whose header is ``sample,<gene1>,<gene2>,…`` (a gene header
        cell may carry a suffix after a space, e.g. ``TP53 (7157)`` → ``TP53``) and
        whose rows are ``sample,<v1>,<v2>,…``.
    :param value_fmt: printf format for each value (``%.3f`` like the Perl).
    :returns: ``(genes, sorted_samples, packed)`` where ``packed[i]`` is the JSON-array
        string for ``genes[i]`` across ``sorted_samples``.
    """
    with open(csv_path, encoding="utf-8") as fin:
        header = fin.readline().rstrip("\n").split(",")
        genes = [c.split(" ")[0] for c in header[1:]]
        samples = [line.split(",", 1)[0] for line in fin if line.strip()]

    order = sorted(range(len(samples)), key=lambda i: samples[i])
    sorted_samples = [samples[i] for i in order]
    sample_pos = {samples[i]: new for new, i in enumerate(order)}

    # data[gene_index][sorted_sample_pos] = value
    data: List[List[Optional[str]]] = [[None] * len(samples) for _ in genes]
    with open(csv_path, encoding="utf-8") as fin:
        fin.readline()
        for line in fin:
            if not line.strip():
                continue
            row = line.rstrip("\n").split(",")
            pos = sample_pos[row[0]]
            for gi, val in enumerate(row[1:]):
                try:
                    data[gi][pos] = value_fmt % float(val)
                except (ValueError, IndexError):
                    data[gi][pos] = "null"
    packed = ["[" + ",".join(v if v is not None else "null" for v in row) + "]" for row in data]
    return genes, sorted_samples, packed


def build_template(samples: Dict[str, list], sorted_samples: List[str], compartment: str) -> dict:
    """Build a json-table template for a data type's sorted sample list.

    :param samples: ``id -> [name, sname, cname, gender, site, prm_met, disease,
        lineage, subtype]`` (as written to ``sample.txt``).
    :param sorted_samples: The data type's sample order (its ``*-meta`` list).
    :param compartment: ``"x"`` (samples-as-columns) or ``"z"`` (samples-as-rows).
    :returns: ``{"data": {"y": {...}, "<compartment>": {annotation: [...]}}}``.
    """
    ann = {k: [] for k in _X_KEYS}
    for sid in sorted_samples:
        meta = samples.get(sid, [""] * 9)
        ann["name"].append(meta[0])
        ann["gender"].append(meta[3])
        ann["site"].append(meta[4])
        ann["prm_met"].append(meta[5])
        ann["disease"].append(meta[6])
        ann["lineage"].append(meta[7])
    y = {"vars": [], "smps": [], "data": []}
    if compartment == "x":
        y["smps"] = list(sorted_samples)
    else:
        y["vars"] = list(sorted_samples)
    return {"data": {"y": y, compartment: ann}}


def single_indices(global_pos: Dict[str, int], sample_list: List[str]) -> List[int]:
    """Index of each of a data type's samples within the global sorted sample list."""
    return [global_pos[s] for s in sample_list]


def pair_indices(list1: List[str], list2: List[str]) -> List[List[int]]:
    """Positions of samples present in BOTH lists (for a cross-datatype correlation)."""
    s2 = set(list2)
    idx1 = [i for i, s in enumerate(list1) if s in s2]
    s1 = set(list1)
    idx2 = [i for i, s in enumerate(list2) if s in s1]
    return [idx1, idx2]


def write_packed(genes: List[str], packed: List[str], out_path: str) -> None:
    """Write ``gene\\t[packed array]`` rows for the cnv/rnaseq tables."""
    with open(out_path, "w", encoding="utf-8") as fout:
        for g, p in zip(genes, packed):
            fout.write("%s\t%s\n" % (g, p))


def assemble(outdir: str, sample_txt: str, gene_txt: str, mutation_txt: str,
             rna_csv: str, cnv_csv: str) -> str:
    """Assemble ``ccle.sqlite`` from prepared sample/gene/mutation files and the
    raw RNA/CNV CSVs (which are packed + templated here).

    :returns: Path to ``ccle.sqlite``.
    """
    os.makedirs(outdir, exist_ok=True)
    # Samples map id -> [name, sname, cname, gender, site, prm_met, disease, lineage, subtype]
    samples: Dict[str, list] = {}
    with open(sample_txt, encoding="utf-8") as fh:
        for line in fh:
            f = line.rstrip("\n").split("\t")
            samples[f[0]] = f[1:10]
    global_sorted = sorted(samples)
    global_pos = {s: i for i, s in enumerate(global_sorted)}

    rna_genes, rna_smps, rna_packed = pack_matrix(rna_csv)
    cnv_genes, cnv_smps, cnv_packed = pack_matrix(cnv_csv)
    rna_txt = os.path.join(outdir, "rna.txt")
    cnv_txt = os.path.join(outdir, "cnv.txt")
    write_packed(rna_genes, rna_packed, rna_txt)
    write_packed(cnv_genes, cnv_packed, cnv_txt)

    # json + indices
    json_txt = os.path.join(outdir, "json.txt")
    with open(json_txt, "w", encoding="utf-8") as fj:
        fj.write("rna1\t%s\n" % json.dumps(build_template(samples, rna_smps, "x")))
        fj.write("rna2\t%s\n" % json.dumps(build_template(samples, rna_smps, "z")))
        fj.write("cnv1\t%s\n" % json.dumps(build_template(samples, cnv_smps, "x")))
        fj.write("cnv2\t%s\n" % json.dumps(build_template(samples, cnv_smps, "z")))
        cr = pair_indices(cnv_smps, rna_smps)
        # cnv-rna2 template: paired samples on the z axis (correlation view)
        paired = [cnv_smps[i] for i in cr[0]]
        fj.write("cnv-rna2\t%s\n" % json.dumps(build_template(samples, paired, "z")))
    index_txt = os.path.join(outdir, "index.txt")
    with open(index_txt, "w", encoding="utf-8") as fi:
        fi.write("cnv\t%s\n" % json.dumps(single_indices(global_pos, cnv_smps)))
        fi.write("rna\t%s\n" % json.dumps(single_indices(global_pos, rna_smps)))
        fi.write("cnv-rna\t%s\n" % json.dumps(pair_indices(cnv_smps, rna_smps)))

    db_path = os.path.join(outdir, "ccle.sqlite")
    build_sqlite(db_path, SCHEMA, [
        (sample_txt, "sample"), (gene_txt, "gene"),
        (cnv_txt, "cnv"), (rna_txt, "rnaseq"), (mutation_txt, "mutation"),
        (json_txt, "json"), (index_txt, "indices"),
    ])
    return db_path


def build(outdir: str, keep: bool = False) -> str:  # pragma: no cover - needs downloads
    """Full download→build pipeline. Not exercised in tests (needs the Figshare
    downloads); see ``assemble`` for the reshape core and the SCHEMA note above about
    validating the sample/mutation column mapping against current files."""
    raise NotImplementedError(
        "ccle.build downloads the current CCLE files; wire the Figshare/probemap URLs "
        "and parse_samples/parse_mutations to current column layouts, then call assemble()."
    )
