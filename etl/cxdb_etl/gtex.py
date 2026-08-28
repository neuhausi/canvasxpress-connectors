"""Build ``gtex.sqlite`` — GTEx v8 gene models, RNASEQ sample annotations, and the
per-gene expression (packed, ``;``-separated tpm) with a single-row sample template.

Python port of the GTEx build Perl. GTEx v8 is a **frozen** release, so — unlike CCLE
/ TCGA whose inputs drift — this is a full download→build pipeline. The expression is
packed like CCLE/TCGA but with two differences the serving side handles via config:
tpm is a ``;``-separated string (not a JSON array), and the sample template is a single
row in the ``json`` table's ``samples`` column (not keyed). See the connectors README
"Packed-matrix sources" (``value_encoding="delimited"``, ``template_key=None``).
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from .common import build_sqlite, download, gunzip

GTF_URL = "https://storage.googleapis.com/adult-gtex/references/v8/reference-tables/gencode.v26.GRCh38.genes.gtf"
SUBJECTS_URL = "https://storage.googleapis.com/adult-gtex/annotations/v8/metadata-files/GTEx_Analysis_v8_Annotations_SubjectPhenotypesDS.txt"
SAMPLES_URL = "https://storage.googleapis.com/adult-gtex/annotations/v8/metadata-files/GTEx_Analysis_v8_Annotations_SampleAttributesDS.txt"
TPM_URL = "https://storage.googleapis.com/adult-gtex/bulk-gex/v8/rna-seq/GTEx_Analysis_2017-06-05_v8_RNASeQCv1.1.9_gene_tpm.gct.gz"

_HARDY = {"0": "Ventilator case", "1": "Fast death - violent",
          "2": "Fast death - natural causes", "3": "Intermediate death",
          "4": "Slow death", "": "NA"}

SCHEMA = """
CREATE TABLE [genome] (
  [geneName] TEXT NOT NULL, [name] TEXT NOT NULL, [chrom] TEXT NOT NULL,
  [strand] TEXT NOT NULL, [txStart] INTEGER NOT NULL, [txEnd] INTEGER NOT NULL,
  [cdsStart] INTEGER NOT NULL, [cdsEnd] INTEGER NOT NULL, [exonCount] INTEGER NOT NULL,
  [exonStarts] BLOB NOT NULL, [exonEnds] BLOB NOT NULL
);
CREATE INDEX [chrom] ON [genome] ([chrom] ASC, [txStart] ASC);
CREATE INDEX [geneGenome] ON [genome] ([geneName] ASC);
CREATE TABLE [samples] (
  [sampleName] TEXT NOT NULL, [sex] TEXT NOT NULL, [age] TEXT NOT NULL,
  [hardy] TEXT NOT NULL, [tissue] TEXT NOT NULL
);
CREATE INDEX [sample] ON [samples] ([sampleName] ASC);
CREATE TABLE [json] ([samples] BLOB NOT NULL);
CREATE TABLE [expression] ([geneName] TEXT NOT NULL, [name] TEXT NOT NULL, [tpm] BLOB NOT NULL);
CREATE INDEX [geneExpression] ON [expression] ([geneName] ASC);
"""


def _attr(desc: str, key: str) -> Optional[str]:
    for field in desc.split(";"):
        field = field.strip()
        if field.startswith(key + " ") or field.startswith(key + '"'):
            return field[len(key):].replace('"', "").strip()
    return None


def parse_genome(gtf_path: str, out_path: str) -> int:
    """Parse a GENCODE GTF into one row **per gene** with all exons packed (sorted).

    Row: geneName, name(=gene_id), chrom, strand, txStart(=gene start), txEnd(=gene
    end), cdsStart(=transcript start), cdsEnd(=transcript end), exonCount, exonStarts,
    exonEnds.
    :returns: Number of genes.
    """
    cur = None
    g_start = g_end = t_start = t_end = None
    exon_start: List[str] = []
    exon_end: List[str] = []
    n = 0

    def flush(handle, cur, gs, ge, ts, te, es, ee):
        handle.write("\t".join([
            cur[0], cur[1], cur[2], cur[3], str(gs), str(ge), str(ts), str(te),
            str(len(es)),
            ",".join(str(x) for x in sorted(es, key=int)),
            ",".join(str(x) for x in sorted(ee, key=int)),
        ]) + "\n")

    with open(gtf_path, encoding="utf-8") as fin, open(out_path, "w", encoding="utf-8") as fout:
        for line in fin:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 9:
                continue
            chrom, _s, ftype, start, end, _sc, strand, _p, desc = parts[:9]
            if ftype == "exon":
                exon_start.append(start)
                exon_end.append(end)
            elif ftype == "gene":
                if cur:
                    flush(fout, cur, g_start, g_end, t_start, t_end, exon_start, exon_end)
                    exon_start, exon_end = [], []
                    g_start = g_end = t_start = t_end = None
                cur = [_attr(desc, "gene_name") or "", _attr(desc, "gene_id") or "", chrom, strand]
                g_start, g_end = start, end
                n += 1
            elif ftype == "transcript":
                t_start, t_end = start, end
        if cur:
            flush(fout, cur, g_start, g_end, t_start, t_end, exon_start, exon_end)
    return n


def parse_samples(subjects_path: str, attributes_path: str,
                  out_samples: str, out_json: str) -> List[str]:
    """Build samples.txt + samples.json (the single-row template) for RNASEQ samples.

    :returns: The RNASEQ sample ids in file order (== the template ``smps``).
    """
    subjects: Dict[str, dict] = {}
    with open(subjects_path, encoding="utf-8") as fin:
        next(fin, None)
        for line in fin:
            f = line.rstrip("\n").split("\t")
            if len(f) < 4:
                continue
            sex = "male" if f[1] == "1" else "female" if f[1] == "2" else ""
            subjects[f[0]] = {"sex": sex, "age": f[2], "hardy": _HARDY.get(f[3], "NA")}

    template = {"data": {"y": {"smps": [], "vars": [], "data": []},
                         "x": {"sex": [], "age": [], "hardy": [], "tissue": []}}}
    order: List[str] = []
    with open(attributes_path, encoding="utf-8") as fin, \
            open(out_samples, "w", encoding="utf-8") as fs:
        next(fin, None)
        for line in fin:
            f = line.rstrip("\n").split("\t")
            sid = f[0]
            sub = "-".join(sid.split("-")[:2])
            tissue = f[6] if len(f) > 6 else ""
            rna = f[16] if len(f) > 16 else ""
            if rna == "RNASEQ":
                s = subjects.get(sub, {"sex": "", "age": "", "hardy": "NA"})
                fs.write("\t".join([sid, s["sex"], s["age"], s["hardy"], tissue]) + "\n")
                template["data"]["x"]["sex"].append(s["sex"])
                template["data"]["x"]["age"].append(s["age"])
                template["data"]["x"]["hardy"].append(s["hardy"])
                template["data"]["x"]["tissue"].append(tissue)
                template["data"]["y"]["smps"].append(sid)
                order.append(sid)
    with open(out_json, "w", encoding="utf-8") as fj:
        fj.write(json.dumps(template))
    return order


def parse_data(gct_path: str, out_data: str, out_order: str,
               sample_order: Optional[List[str]] = None) -> int:
    """Parse the gene-tpm GCT into expression rows (``;``-joined tpm).

    Row: geneName(=Description/symbol), name(=Name/ensembl), tpm. When ``sample_order``
    is given, the GCT column order is validated against it (as the Perl did).
    :returns: Number of gene rows.
    """
    n = 0
    with open(gct_path, encoding="utf-8") as fin:
        fin.readline()  # version line
        fin.readline()  # dimensions line
        header = fin.readline().rstrip("\n").split("\t")[2:]   # drop Name, Description
        if sample_order is not None and header != sample_order:
            raise ValueError("GCT sample columns do not match the samples file order")
        with open(out_order, "w", encoding="utf-8") as fo:
            fo.write("\n".join(header))
        with open(out_data, "w", encoding="utf-8") as fd:
            for line in fin:
                parts = line.rstrip("\n").split("\t", 2)
                if len(parts) < 3:
                    continue
                gid, gname, vals = parts[0], parts[1], parts[2]
                fd.write("%s\t%s\t%s\n" % (gname, gid, vals.replace("\t", ";")))
                n += 1
    return n


def assemble(outdir: str, genome_txt: str, samples_txt: str,
             samples_json: str, data_txt: str) -> str:
    """Assemble ``gtex.sqlite`` from the four prepared files."""
    os.makedirs(outdir, exist_ok=True)
    db_path = os.path.join(outdir, "gtex.sqlite")
    build_sqlite(db_path, SCHEMA, [
        (genome_txt, "genome"), (samples_txt, "samples"),
        (samples_json, "json"), (data_txt, "expression"),
    ])
    return db_path


def build(outdir: str, keep: bool = False) -> str:
    """Full download→build pipeline for the frozen GTEx v8 release.

    :param outdir: Working/output directory (``gtex.sqlite`` is written here).
    :returns: Path to ``gtex.sqlite``.
    """
    os.makedirs(outdir, exist_ok=True)
    gtf = download(GTF_URL, os.path.join(outdir, "gencode.v26.GRCh38.genes.gtf"))
    subjects = download(SUBJECTS_URL, os.path.join(outdir, "subjects.txt"))
    attributes = download(SAMPLES_URL, os.path.join(outdir, "attributes.txt"))
    gct = gunzip(download(TPM_URL, os.path.join(outdir, "gene_tpm.gct.gz")))

    genome_txt = os.path.join(outdir, "genome.txt")
    samples_txt = os.path.join(outdir, "samples.txt")
    samples_json = os.path.join(outdir, "samples.json")
    data_txt = os.path.join(outdir, "data.txt")
    order_txt = os.path.join(outdir, "order.txt")

    parse_genome(gtf, genome_txt)
    order = parse_samples(subjects, attributes, samples_txt, samples_json)
    parse_data(gct, data_txt, order_txt, sample_order=order)

    db_path = assemble(outdir, genome_txt, samples_txt, samples_json, data_txt)

    if not keep:
        for f in (genome_txt, samples_txt, samples_json, data_txt, order_txt):
            if os.path.exists(f):
                os.remove(f)
    return db_path
