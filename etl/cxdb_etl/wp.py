"""Build ``wp.sqlite`` — WikiPathways pathways + members + NCBI gene/taxonomy.

Python port of ``wpServices.pl``'s ``update`` pipeline: parse the WikiPathways GMT
files into pathway + member rows, then filter NCBI ``gene_info`` / ``taxdmp`` down to
the genes those pathways reference, and assemble the SQLite database.

The GMT descriptor field is ``name%version%wpId%taxName``; the second field is the
pathway URL (rewritten to ``/assets/gpml/<file>`` when a GPML filename is known); the
remaining fields are NCBI gene ids.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from .common import build_sqlite, download, gunzip, run

WP_BASE = "https://wikipathways-data.wmcloud.org/current"
GENE_INFO_URL = "https://ftp.ncbi.nlm.nih.gov/gene/DATA/gene_info.gz"
TAXDMP_URL = "https://ftp.ncbi.nih.gov/pub/taxonomy/taxdmp.zip"

SCHEMA = """
CREATE TABLE [gene] (
  [geneId] INTEGER NOT NULL, [symbol] TEXT NOT NULL, [ensemblId] TEXT,
  [taxId] INTEGER NOT NULL, [description] TEXT NOT NULL
);
CREATE INDEX [taxId] ON [gene] ([taxId] ASC, [geneId] ASC);
CREATE INDEX [symbol] ON [gene] ([symbol] ASC);
CREATE INDEX [ensemblId] ON [gene] ([ensemblId] ASC);
CREATE INDEX [geneId] ON [gene] ([geneId] ASC);
CREATE TABLE [taxonomy] ([taxId] INTEGER NOT NULL, [name] TEXT NOT NULL);
CREATE INDEX [name] ON [taxonomy] ([name] ASC);
CREATE TABLE [pathway] (
  [wpId] TEXT NOT NULL, [name] TEXT NOT NULL, [version] TEXT NOT NULL,
  [taxName] TEXT NOT NULL, [url] TEXT NOT NULL
);
CREATE INDEX [wpId] ON [pathway] ([wpId] ASC);
CREATE TABLE [members] ([geneId] INTEGER NOT NULL, [wpId] TEXT NOT NULL);
CREATE INDEX [memberId] ON [members] ([geneId] ASC);
CREATE INDEX [wpId2] ON [members] ([wpId] ASC);
"""

_HREF_RE = re.compile(r"href='\./(.+?)'>")


def list_files(kind: str) -> List[str]:
    """Scrape the WikiPathways directory listing for ``gmt`` or ``gpml`` files."""
    import urllib.request

    with urllib.request.urlopen("%s/%s" % (WP_BASE, kind)) as resp:
        html = resp.read().decode("utf-8", "replace")
    ext = kind
    return [m for m in _HREF_RE.findall(html) if m.endswith(ext)]


def parse_gmt(gmt_files: List[str], out_pathway: str, out_members: str,
              gpml_map: Optional[Dict[str, str]] = None) -> Tuple[int, int]:
    """Parse GMT files into pathway rows and gene-membership rows.

    :param gmt_files: Paths to ``.gmt`` files.
    :param out_pathway: TSV for the ``pathway`` table (wpId,name,version,taxName,url).
    :param out_members: TSV for the ``members`` table (geneId,wpId).
    :param gpml_map: Optional ``wpId -> gpml filename`` to build the asset URL.
    :returns: ``(pathway_count, member_count)``.
    """
    gpml_map = gpml_map or {}
    npath = nmemb = 0
    with open(out_pathway, "w", encoding="utf-8") as fp, \
            open(out_members, "w", encoding="utf-8") as fg:
        for path in gmt_files:
            with open(path, encoding="utf-8", errors="replace") as fin:
                for line in fin:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    fields = line.split("\t")
                    descriptor = fields[0]
                    parts = descriptor.split("%")
                    if len(parts) < 4:
                        continue
                    name, version, wp_id, tax_name = parts[0], parts[1], parts[2], parts[3]
                    url = "/assets/gpml/%s" % gpml_map.get(wp_id, "")
                    fp.write("\t".join([wp_id, name, version, tax_name, url]) + "\n")
                    npath += 1
                    for gene in fields[2:]:
                        if gene:
                            fg.write("%s\t%s\n" % (gene, wp_id))
                            nmemb += 1
    return npath, nmemb


def gpml_map_from_files(gpml_files: List[str]) -> Dict[str, str]:
    """Map ``wpId -> filename`` from a list of GPML filenames (``*_WP1234_*``)."""
    out = {}
    for f in gpml_files:
        m = re.search(r"_(WP\d+)_", f)
        if m:
            out[m.group(1)] = f
    return out


def filter_ncbi(gene_info: str, names_dmp: str, member_gene_ids: set,
                out_gene: str, out_tax: str) -> Tuple[int, int]:
    """Build gene.txt / tax.txt limited to the genes the pathways reference.

    :param gene_info: NCBI ``gene_info`` path (uncompressed).
    :param names_dmp: NCBI ``names.dmp`` path.
    :param member_gene_ids: Set of NCBI gene ids (strings) that appear in members.
    :param out_gene: TSV for the ``gene`` table (geneId,symbol,ensemblId,taxId,desc).
    :param out_tax: TSV for the ``taxonomy`` table (taxId,name).
    :returns: ``(gene_count, tax_count)``.
    """
    kept_taxa = set()
    ng = nt = 0
    with open(gene_info, encoding="utf-8", errors="replace") as fin, \
            open(out_gene, "w", encoding="utf-8") as fout:
        next(fin, None)
        for line in fin:
            f = line.rstrip("\n").split("\t")
            if len(f) < 9 or f[1] not in member_gene_ids:
                continue
            ens = ""
            m = re.search(r"Ensembl:(ENS\w*)", f[5])
            if m:
                ens = m.group(1)
            kept_taxa.add(f[0])
            fout.write("\t".join([f[1], f[2].upper(), ens, f[0], f[8]]) + "\n")
            ng += 1
    with open(names_dmp, encoding="utf-8", errors="replace") as fin, \
            open(out_tax, "w", encoding="utf-8") as fout:
        for line in fin:
            f = line.rstrip("\n").split("\t")
            if len(f) < 3 or f[0] not in kept_taxa:
                continue
            fout.write("%s\t%s\n" % (f[0], f[2].replace('"', "")))
            nt += 1
    return ng, nt


def build(outdir: str, keep: bool = False,
          gmt_files: Optional[List[str]] = None,
          gene_info: Optional[str] = None,
          names_dmp: Optional[str] = None,
          gpml_map: Optional[Dict[str, str]] = None) -> str:
    """Download (unless paths given), parse, and assemble ``wp.sqlite``.

    :param outdir: Working/output directory.
    :param gmt_files: Local ``.gmt`` files (else scrape + download all current ones).
    :param gene_info: Local NCBI ``gene_info`` (else download + gunzip).
    :param names_dmp: Local NCBI ``names.dmp`` (else download taxdmp.zip + unzip).
    :param gpml_map: ``wpId -> gpml filename`` (else derived from the GPML listing).
    :returns: Path to ``wp.sqlite``.
    """
    os.makedirs(outdir, exist_ok=True)

    if gmt_files is None:
        gmts = list_files("gmt")
        gmt_files = []
        for f in gmts:
            gmt_files.append(download("%s/gmt/%s" % (WP_BASE, f), os.path.join(outdir, f)))
        if gpml_map is None:
            gpml_map = gpml_map_from_files(list_files("gpml"))

    pathway_txt = os.path.join(outdir, "pathway.txt")
    members_txt = os.path.join(outdir, "members.txt")
    parse_gmt(gmt_files, pathway_txt, members_txt, gpml_map=gpml_map)

    member_ids = set()
    with open(members_txt, encoding="utf-8") as fh:
        for line in fh:
            member_ids.add(line.split("\t")[0])

    if gene_info is None:
        gz = download(GENE_INFO_URL, os.path.join(outdir, "gene_info.gz"))
        gene_info = gunzip(gz)
    if names_dmp is None:
        zpath = download(TAXDMP_URL, os.path.join(outdir, "taxdmp.zip"))
        tdir = os.path.join(outdir, "taxdmp")
        run(["unzip", "-o", zpath, "-d", tdir])
        names_dmp = os.path.join(tdir, "names.dmp")

    gene_txt = os.path.join(outdir, "gene.txt")
    tax_txt = os.path.join(outdir, "tax.txt")
    filter_ncbi(gene_info, names_dmp, member_ids, gene_txt, tax_txt)

    db_path = os.path.join(outdir, "wp.sqlite")
    build_sqlite(db_path, SCHEMA, [
        (gene_txt, "gene"), (tax_txt, "taxonomy"),
        (pathway_txt, "pathway"), (members_txt, "members"),
    ])

    if not keep:
        for f in (pathway_txt, members_txt, gene_txt, tax_txt):
            if os.path.exists(f):
                os.remove(f)
    return db_path
