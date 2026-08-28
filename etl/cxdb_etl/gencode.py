"""Build ``gencode.sqlite`` — GENCODE gene models + the GWAS catalog.

Python port of ``gencodeServices.pl``'s ``update`` pipeline: parse the GENCODE GTF
into one row per transcript (with packed exon start/end lists) and the GWAS catalog
TSV into association rows, then assemble the SQLite database.
"""

from __future__ import annotations

import os
from typing import Optional

from .common import build_sqlite, download, gunzip

GTF_URL = "http://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_39/gencode.v39.annotation.gtf.gz"
GWAS_URL = "https://www.ebi.ac.uk/gwas/api/search/downloads/full"

_CAPTURE = {"gene_id", "gene_name", "gene_type", "exon_id", "exon_number",
            "transcript_id", "transcript_name"}

SCHEMA = """
CREATE TABLE [genome] (
  [geneName] TEXT NOT NULL, [geneId] TEXT NOT NULL, [transcriptId] TEXT NOT NULL,
  [geneType] TEXT NOT NULL, [chrom] TEXT NOT NULL, [strand] TEXT NOT NULL,
  [txStart] INTEGER NOT NULL, [txEnd] INTEGER NOT NULL,
  [cdsStart] INTEGER NOT NULL, [cdsEnd] INTEGER NOT NULL,
  [exonCount] INTEGER NOT NULL, [exonStarts] BLOB NOT NULL, [exonEnds] BLOB NOT NULL
);
CREATE INDEX [chrom] ON [genome] ([chrom] ASC, [txStart] ASC);
CREATE INDEX [geneName] ON [genome] ([geneName] ASC);
CREATE INDEX [geneId] ON [genome] ([geneId] ASC);
CREATE TABLE [gwas] (
  [pubmedid] INTEGER NOT NULL, [diseaseTrait] TEXT NOT NULL, [chrom] INTEGER NOT NULL,
  [start] INTEGER NOT NULL, [strongestSNPRiskAllele] BLOB NOT NULL,
  [pValue] NUMERIC, [ORorBeta] NUMERIC
);
CREATE INDEX [position] ON [gwas] ([chrom] ASC, [start] ASC);
"""


def _parse_attributes(attributes: str) -> dict:
    """Parse a GTF attribute string into the captured key/value pairs."""
    fields = {}
    for attr in attributes.split(";"):
        attr = attr.strip()
        if not attr or " " not in attr:
            continue
        c_type, c_value = attr.split(" ", 1)
        c_value = c_value.replace('"', "").strip()
        if c_type in _CAPTURE:
            fields[c_type] = c_value
    return fields


def parse_genome(gtf_path: str, out_path: str) -> int:
    """Parse a GENCODE GTF into one row per transcript with packed exon lists.

    Each row: geneName, geneId, transcriptId, geneType, chrom, strand, txStart(=
    transcript start), txEnd, cdsStart(=gene start), cdsEnd(=gene end), exonCount,
    exonStarts (comma-joined), exonEnds (comma-joined).

    :param gtf_path: Path to the (uncompressed) GENCODE GTF.
    :param out_path: TSV output path.
    :returns: Number of genes seen.
    """
    g_start = g_end = t_start = t_end = None
    trans = []          # transcript ids for the current gene, in order
    info = {}           # transcript_id -> [exon field dict, ...]
    genes = 0

    def flush(handle):
        for tid in trans:
            exons = sorted(info.get(tid, []), key=lambda e: int(e.get("exon_number", 0)))
            if not exons:
                continue
            e0 = exons[0]
            e_starts = ",".join(str(e["start"]) for e in exons)
            e_ends = ",".join(str(e["end"]) for e in exons)
            handle.write("\t".join([
                e0.get("gene_name", ""), e0.get("gene_id", ""), e0.get("transcript_id", ""),
                e0.get("gene_type", ""), e0["chr"], e0["strand"],
                str(e0["tStart"]), str(e0["tEnd"]), str(e0["gStart"]), str(e0["gEnd"]),
                str(len(exons)), e_starts, e_ends,
            ]) + "\n")

    with open(gtf_path, encoding="utf-8") as fin, open(out_path, "w", encoding="utf-8") as fout:
        for line in fin:
            if line.startswith("##"):
                continue
            line = line.rstrip("\n")
            parts = line.split("\t")
            if len(parts) < 9:
                continue
            chrom, _source, ftype, start, end, _score, strand, _phase, attributes = parts[:9]
            fields = {"chr": chrom, "start": start, "end": end, "strand": strand}
            fields.update(_parse_attributes(attributes))
            if fields.get("transcript_id") and fields["transcript_id"] not in info:
                info[fields["transcript_id"]] = []
            if ftype == "gene":
                g_start, g_end = start, end
                if genes > 0 and trans:
                    flush(fout)
                    trans = []
                    info = {}
                genes += 1
            elif ftype == "transcript":
                trans.append(fields["transcript_id"])
                t_start, t_end = start, end
            elif ftype == "exon":
                fields["gStart"] = g_start
                fields["gEnd"] = g_end
                fields["tStart"] = t_start
                fields["tEnd"] = t_end
                info[fields["transcript_id"]].append(fields)
        # Final flush — the original Perl dropped the last gene; we keep it.
        if trans:
            flush(fout)
    return genes


def parse_gwas(tsv_path: str, out_path: str) -> int:
    """Parse the GWAS catalog TSV into association rows (chrom prefixed ``chr``).

    :returns: Number of rows written.
    """
    n = 0
    with open(tsv_path, encoding="utf-8", errors="replace") as fin, \
            open(out_path, "w", encoding="utf-8") as fout:
        next(fin, None)   # header
        for line in fin:
            line = line.rstrip("\n")
            f = line.split("\t")
            if len(f) <= 30:
                continue
            fout.write("\t".join([
                f[1], f[7], "chr" + f[11], f[12], f[20], f[27], f[30],
            ]) + "\n")
            n += 1
    return n


def build(outdir: str, keep: bool = False, gtf_path: Optional[str] = None,
          gwas_path: Optional[str] = None) -> str:
    """Download (unless paths given), parse, and assemble ``gencode.sqlite``.

    :param outdir: Working directory; the DB is written here as ``gencode.sqlite``.
    :param keep: Keep intermediate files instead of deleting them.
    :param gtf_path: Use a local GTF instead of downloading.
    :param gwas_path: Use a local GWAS TSV instead of downloading.
    :returns: Path to the built ``gencode.sqlite``.
    """
    os.makedirs(outdir, exist_ok=True)
    if gtf_path is None:
        gz = download(GTF_URL, os.path.join(outdir, "gencode.gtf.gz"))
        gtf_path = gunzip(gz)
    if gwas_path is None:
        gwas_path = download(GWAS_URL, os.path.join(outdir, "gwas_catalog_associations.tsv"))

    genome_txt = os.path.join(outdir, "genome.txt")
    gwas_txt = os.path.join(outdir, "gwas.txt")
    parse_genome(gtf_path, genome_txt)
    parse_gwas(gwas_path, gwas_txt)

    db_path = os.path.join(outdir, "gencode.sqlite")
    build_sqlite(db_path, SCHEMA, [(genome_txt, "genome"), (gwas_txt, "gwas")])

    if not keep:
        for f in (genome_txt, gwas_txt):
            if os.path.exists(f):
                os.remove(f)
    return db_path
