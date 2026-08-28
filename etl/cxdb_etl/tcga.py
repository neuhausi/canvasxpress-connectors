"""Build ``tcga.sqlite`` — TCGA PanCanAtlas copy-number, thresholded copy-number,
expression, protein (RPPA), mutations, clinical samples, and the many json/indices
templates the packed-matrix source reads.

Python port of ``tcgaServices.pl``'s ``update`` pipeline. Unlike CCLE, the TCGA Xena
matrices are **genes-as-rows / samples-as-columns**, so the packed reshape only has to
sort the sample columns and reorder each gene row (no transpose). Each data type gets
four json templates — ``<type>1min``/``2min`` (2 annotations: cancer, abbr) and
``<type>1med``/``2med`` (5: cancer, abbr, age, gender, race) — where ``1`` puts samples
on the ``x`` (smps) axis and ``2`` on the ``z`` (vars) axis. ``indices`` pairs samples
across data types for correlation, and a ``survival`` template feeds the KM view.

NOTE ON INPUT SCHEMAS: the clinical sample layout (35 columns) and the mutation column
positions mirror the Xena PanCanAtlas files; confirm them against current downloads
before a real run. The reshape (``pack_rows``), templates, and ``assemble`` are
format-independent and covered by tests.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

from .ccle import pair_indices, single_indices
from .common import build_sqlite

SCHEMA = """
CREATE TABLE [sample] (
  [id] TEXT NOT NULL, [sample_type] TEXT, [primary_disease] TEXT, [patient] TEXT,
  [cancer_type_abbreviation] TEXT, [age_at_initial_pathologic_diagnosis] INTEGER,
  [gender] TEXT, [race] TEXT, [ajcc_pathologic_tumor_stage] TEXT, [clinical_stage] TEXT,
  [histological_type] TEXT, [histological_grade] TEXT, [initial_pathologic_dx_year] INTEGER,
  [menopause_status] TEXT, [birth_days_to] INTEGER, [vital_status] TEXT, [tumor_status] TEXT,
  [last_contact_days_to] INTEGER, [death_days_to] INTEGER, [cause_of_death] TEXT,
  [new_tumor_event_type] TEXT, [new_tumor_event_site] TEXT, [new_tumor_event_site_other] TEXT,
  [new_tumor_event_dx_days_to] INTEGER, [treatment_outcome_first_course] TEXT,
  [margin_status] TEXT, [residual_tumor] TEXT, [OS] INTEGER, [OS.time] INTEGER,
  [DSS] INTEGER, [DSS.time] INTEGER, [DFI] INTEGER, [DFI.time] INTEGER, [PFI] INTEGER,
  [PFI.time] INTEGER, [Redaction] TEXT, [idx] INTEGER NOT NULL
);
CREATE INDEX [sampleid] ON [sample] ([id] ASC);
CREATE INDEX [cancer] ON [sample] ([cancer_type_abbreviation] ASC);
CREATE TABLE [gene] (
  [id] TEXT NOT NULL, [name] TEXT NOT NULL, [chrom] TEXT NOT NULL, [start] INTEGER NOT NULL,
  [end] INTEGER NOT NULL, [strand] TEXT NOT NULL, [exonCount] INTEGER,
  [exonStarts] BLOB, [exonEnds] BLOB
);
CREATE INDEX [ensemblid] ON [gene] ([id] ASC);
CREATE INDEX [symbol] ON [gene] ([name] ASC);
CREATE TABLE [cnv] ([name] TEXT NOT NULL, [gistic2] BLOB NOT NULL);
CREATE INDEX [cnv_name] ON [cnv] ([name] ASC);
CREATE TABLE [cnvt] ([name] TEXT NOT NULL, [gistic2t] BLOB NOT NULL);
CREATE INDEX [cnvt_name] ON [cnvt] ([name] ASC);
CREATE TABLE [rnaseq] ([id] TEXT NOT NULL, [name] TEXT NOT NULL, [log2tpm] BLOB NOT NULL);
CREATE INDEX [rna_id] ON [rnaseq] ([id] ASC);
CREATE INDEX [rna_name] ON [rnaseq] ([name] ASC);
CREATE TABLE [rppa] ([name] TEXT NOT NULL, [value] BLOB NOT NULL);
CREATE INDEX [rppa_name] ON [rppa] ([name] ASC);
CREATE TABLE [mutation] (
  [sample] TEXT NOT NULL, [chrom] TEXT NOT NULL, [start] INTEGER NOT NULL, [end] INTEGER NOT NULL,
  [reference] TEXT, [alt] TEXT, [gene] TEXT NOT NULL, [effect] TEXT, [aa_change] TEXT,
  [dna_vaf] TEXT, [sift] TEXT, [polyphen] TEXT, [idx] INTEGER NOT NULL
);
CREATE INDEX [mut_sample] ON [mutation] ([sample] ASC);
CREATE INDEX [mut_gene] ON [mutation] ([gene] ASC);
CREATE TABLE [json] ([key] TEXT NOT NULL, [str] BLOB NOT NULL);
CREATE TABLE [indices] ([key] TEXT NOT NULL, [str] BLOB NOT NULL);
"""

# Sample-metadata positions within sample.txt (after the id column):
#   0 sample_type, 1 primary_disease, 2 patient, 3 cancer_abbr, 4 age, 5 gender, 6 race
_CANCER, _ABBR, _AGE, _GENDER, _RACE = 1, 3, 4, 5, 6


def pack_rows(path: str, sep: str = "\t", na: str = "NA") -> Tuple[List[str], List[str], List[str]]:
    """Reshape a genes-as-rows / samples-as-columns matrix into packed per-gene arrays.

    The header is ``<label><sep><sample1><sep>…``; each row is ``<gene><sep><v1>…``.
    Samples are sorted; each row's values are reordered to match and ``NA``/empty
    becomes ``null``.

    :returns: ``(row_labels, sorted_samples, packed)``.
    """
    with open(path, encoding="utf-8") as fin:
        header = fin.readline().rstrip("\n").split(sep)
    samples = header[1:]
    order = sorted(range(len(samples)), key=lambda i: samples[i])
    sorted_samples = [samples[i] for i in order]

    labels: List[str] = []
    packed: List[str] = []
    with open(path, encoding="utf-8") as fin:
        fin.readline()
        for line in fin:
            if not line.strip():
                continue
            row = line.rstrip("\n").split(sep)
            vals = row[1:]
            reordered = []
            for i in order:
                v = vals[i] if i < len(vals) else na
                reordered.append("null" if v == na or v == "" else v)
            labels.append(row[0])
            packed.append("[" + ",".join(reordered) + "]")
    return labels, sorted_samples, packed


def pack_rows_to_file(path: str, out_path: str, id_to_name: Optional[Dict[str, str]] = None,
                      sep: str = "\t", na: str = "NA") -> List[str]:
    """Stream a genes-as-rows matrix into a packed output file, returning only the
    sorted sample list (so the caller holds no per-gene arrays — essential at TCGA
    scale). When ``id_to_name`` is given (the RNA matrix keyed by gene id), rows are
    written ``id\\tname\\t[packed]``; otherwise ``name\\t[packed]``.

    :returns: The sorted sample columns.
    """
    with open(path, encoding="utf-8") as fin:
        header = fin.readline().rstrip("\n").split(sep)
    samples = header[1:]
    order = sorted(range(len(samples)), key=lambda i: samples[i])
    sorted_samples = [samples[i] for i in order]

    with open(path, encoding="utf-8") as fin, open(out_path, "w", encoding="utf-8") as fout:
        fin.readline()
        for line in fin:
            if not line.strip():
                continue
            row = line.rstrip("\n").split(sep)
            vals = row[1:]
            packed = "[" + ",".join(
                ("null" if (i >= len(vals) or vals[i] == na or vals[i] == "") else vals[i])
                for i in order) + "]"
            if id_to_name is not None:
                fout.write("%s\t%s\t%s\n" % (row[0], id_to_name.get(row[0], row[0]), packed))
            else:
                fout.write("%s\t%s\n" % (row[0], packed))
    return sorted_samples


def build_templates(samples: Dict[str, list], sorted_samples: List[str]) -> Dict[str, str]:
    """The four json templates for a data type's sorted sample list.

    :returns: ``{"1min":…, "2min":…, "1med":…, "2med":…}`` as JSON strings, where
        ``1`` places samples on the ``x`` (smps) axis and ``2`` on ``z`` (vars); ``min``
        carries cancer+abbr, ``med`` adds age+gender+race.
    """
    def ann(keys):
        out = {k: [] for k in keys}
        for sid in sorted_samples:
            m = samples.get(sid, [""] * 7)
            out["cancer"].append(m[_CANCER])
            out["abbr"].append(m[_ABBR])
            if "age" in out:
                out["age"].append(m[_AGE])
                out["gender"].append(m[_GENDER])
                out["race"].append(m[_RACE])
        return out

    min_keys = ["cancer", "abbr"]
    med_keys = ["cancer", "abbr", "age", "gender", "race"]

    def struct(compartment, keys):
        y = {"vars": [], "smps": [], "data": []}
        if compartment == "x":
            y["smps"] = list(sorted_samples)
        else:
            y["vars"] = list(sorted_samples)
        return json.dumps({"data": {"y": y, compartment: ann(keys)}})

    return {
        "1min": struct("x", min_keys),
        "2min": struct("z", min_keys),
        "1med": struct("x", med_keys),
        "2med": struct("z", med_keys),
    }


def survival_template(samples: Dict[str, list], global_sorted: List[str],
                      os_idx: int = 27, os_time_idx: int = 26) -> str:
    """Build the survival KM template: for each sample with an OS time, a
    ``[time, censor]`` row on ``y`` plus cancer/abbr/gender/race on ``z``.

    :param os_idx: sample-column index of the OS event flag.
    :param os_time_idx: sample-column index of the OS time.
    """
    z = {"cancer": [], "abbr": [], "gender": [], "race": []}
    y = {"vars": [], "smps": ["Survival", "Censor"], "data": []}
    for sid in global_sorted:
        m = samples.get(sid, [])
        if len(m) > os_idx and m[os_idx] not in ("", None):
            y["vars"].append(sid)
            z["cancer"].append(m[_CANCER])
            z["abbr"].append(m[_ABBR])
            z["gender"].append(m[_GENDER])
            z["race"].append(m[_RACE])
            y["data"].append([m[os_idx], m[os_time_idx] if len(m) > os_time_idx else None])
    return json.dumps({"data": {"y": y, "z": z}})


def _write_packed(labels, packed, out_path, ids=None):
    with open(out_path, "w", encoding="utf-8") as fout:
        for i, (lab, pk) in enumerate(zip(labels, packed)):
            if ids is not None:
                fout.write("%s\t%s\t%s\n" % (ids[i], lab, pk))
            else:
                fout.write("%s\t%s\n" % (lab, pk))


def assemble(outdir: str, sample_txt: str, gene_txt: str, mutation_txt: str,
             matrices: Dict[str, str], gene_name_by_id: Optional[Dict[str, str]] = None) -> str:
    """Assemble ``tcga.sqlite`` from prepared sample/gene/mutation files and the raw
    matrices, packing + templating each here.

    :param matrices: ``{"cnv": path, "cnvt": path, "rna": path, "prt": path}`` — any
        subset; each is a genes-as-rows / samples-as-columns matrix. The ``rna`` matrix
        is keyed by gene id (first column); ``gene_name_by_id`` maps id → symbol.
    :returns: Path to ``tcga.sqlite``.
    """
    os.makedirs(outdir, exist_ok=True)
    samples: Dict[str, list] = {}
    with open(sample_txt, encoding="utf-8") as fh:
        for line in fh:
            f = line.rstrip("\n").split("\t")
            samples[f[0]] = f[1:]
    global_sorted = sorted(samples)
    global_pos = {s: i for i, s in enumerate(global_sorted)}
    gene_name_by_id = gene_name_by_id or {}

    smps_by_key: Dict[str, List[str]] = {}   # only the sample lists are retained
    imports = [(sample_txt, "sample"), (gene_txt, "gene")]
    table_for = {"cnv": "cnv", "cnvt": "cnvt", "rna": "rnaseq", "prt": "rppa"}

    for key, path in matrices.items():
        out = os.path.join(outdir, "%s.txt" % key)
        id_map = gene_name_by_id if key == "rna" else None
        smps_by_key[key] = pack_rows_to_file(path, out, id_to_name=id_map)
        imports.append((out, table_for[key]))

    # json templates (per data type) + cross-type correlation templates.
    json_txt = os.path.join(outdir, "json.txt")
    with open(json_txt, "w", encoding="utf-8") as fj:
        for key in matrices:
            tpls = build_templates(samples, smps_by_key[key])
            for suffix, s in tpls.items():
                fj.write("%s%s\t%s\n" % (key, suffix, s))
        # Cross pairs present in both, on the z axis (correlation view).
        keys = list(matrices)
        for a in keys:
            for b in keys:
                if a < b:
                    pa = pair_indices(smps_by_key[a], smps_by_key[b])
                    paired = [smps_by_key[a][i] for i in pa[0]]
                    for suffix in ("2min", "2med"):
                        tpls = build_templates(samples, paired)
                        fj.write("%s-%s%s\t%s\n" % (a, b, suffix, tpls[suffix]))
        fj.write("survival\t%s\n" % survival_template(samples, global_sorted))

    index_txt = os.path.join(outdir, "index.txt")
    with open(index_txt, "w", encoding="utf-8") as fi:
        for key in matrices:
            fi.write("%s\t%s\n" % (key, json.dumps(single_indices(global_pos, smps_by_key[key]))))
        keys = list(matrices)
        for a in keys:
            for b in keys:
                if a < b:
                    fi.write("%s-%s\t%s\n" % (a, b, json.dumps(
                        pair_indices(smps_by_key[a], smps_by_key[b]))))

    imports += [(mutation_txt, "mutation"), (json_txt, "json"), (index_txt, "indices")]
    db_path = os.path.join(outdir, "tcga.sqlite")
    build_sqlite(db_path, SCHEMA, imports)
    return db_path


# --- Xena PanCanAtlas source files (frozen v8 / 2016-2017 releases) ---
_HUB_TCGA = "https://tcga-xena-hub.s3.us-east-1.amazonaws.com/download/"
_HUB_TOIL = "https://toil-xena-hub.s3.us-east-1.amazonaws.com/download/"
_HUB_PAN = "https://tcga-pancan-atlas-hub.s3.us-east-1.amazonaws.com/download/"
URLS = {
    "cnv": _HUB_TCGA + "TCGA.PANCAN.sampleMap%2FGistic2_CopyNumber_Gistic2_all_data_by_genes.gz",
    "cnvt": _HUB_TCGA + "TCGA.PANCAN.sampleMap%2FGistic2_CopyNumber_Gistic2_all_thresholded.by_genes.gz",
    "rna": _HUB_TOIL + "tcga_RSEM_gene_tpm.gz",
    "probemap": _HUB_TOIL + "probeMap%2Fgencode.v23.annotation.gene.probemap",
    "clinical": _HUB_PAN + "Survival_SupplementalTable_S1_20171025_xena_sp",
    "disease": _HUB_PAN + "TCGA_phenotype_denseDataOnlyDownload.tsv.gz",
    "protein": _HUB_PAN + "TCGA-RPPA-pancan-clean.xena.gz",
    "mutation": _HUB_PAN + "mc3.v0.2.8.PUBLIC.xena.gz",
    "probemap2": _HUB_PAN + "probeMap%2Fhugo_gencode_good_hg19_V24lift37_probemap",
}


def create_samples(disease_path: str, clinical_path: str, out_sample: str) -> None:
    """Merge the phenotype (sample_type, primary_disease) and clinical (Survival
    supplement) files into sample.txt: ``id`` + 35 columns + ``idx``. Only samples
    present in both (35 clinical columns filled) are written. Mirrors the Perl.
    """
    samples: Dict[str, list] = {}
    with open(disease_path, encoding="utf-8") as fin:
        next(fin, None)
        for line in fin:
            f = line.rstrip("\n").split("\t")
            if len(f) < 4:
                continue
            samples[f[0]] = [f[2], f[3]]            # [sample_type, primary_disease]
    with open(clinical_path, encoding="utf-8") as fin:
        header = fin.readline().rstrip("\n").split("\t")
        gcols = len(header) - 1
        for line in fin:
            f = line.rstrip("\n").split("\t")
            sid = f[0]
            if sid not in samples:
                continue
            for i in range(gcols):
                val = f[i + 1] if i + 1 < len(f) else ""
                # index 2 onward = clinical columns
                lst = samples[sid]
                while len(lst) <= i + 2:
                    lst.append("")
                lst[i + 2] = val
    complete = 2 + gcols   # samples with the full clinical row
    with open(out_sample, "w", encoding="utf-8") as fs:
        idx = 0
        for sid in sorted(samples):
            row = samples[sid]
            if len(row) != complete:
                continue
            fs.write("%s\t%s\t%d\n" % (sid, "\t".join(row), idx))
            idx += 1


def create_genes(probemap: str, probemap2: str, out_gene: str) -> None:
    """Build gene.txt (id, name, chrom, start, end, strand, exonCount, exonStarts,
    exonEnds) from the two Xena probemaps, mirroring the Perl (probemap2 supplies exon
    coordinates; probemap is the id/name spine)."""
    coords: Dict[str, list] = {}
    with open(probemap2, encoding="utf-8") as fin:
        next(fin, None)
        for line in fin:
            l = line.rstrip("\n").split("\t")
            if len(l) < 11:
                continue
            gene, chrom, start, end, strand, n = l[1], l[2], l[3], l[4], l[5], l[8]
            # Perl: @l=col9, @o=col10; o[i]+=start; l[i]+=o[i]; exonStarts=o, exonEnds=l
            l_raw = [x for x in l[9].split(",") if x != ""]
            o_raw = [x for x in l[10].split(",") if x != ""]
            try:
                s0 = int(start)
                o = [int(o_raw[i]) + s0 for i in range(len(o_raw))]
                ll = [int(l_raw[i]) + o[i] for i in range(min(len(l_raw), len(o)))]
            except ValueError:
                o, ll = [], []
            coords[gene] = [chrom, start, end, strand, n,
                            ",".join(str(x) for x in o), ",".join(str(x) for x in ll)]
    with open(probemap, encoding="utf-8") as fin, open(out_gene, "w", encoding="utf-8") as fout:
        next(fin, None)
        for line in fin:
            l = line.rstrip("\n").split("\t")
            if len(l) < 5:
                continue
            if l[1] in coords:
                fout.write("%s\t%s\t%s\n" % (l[0], l[1], "\t".join(coords[l[1]])))
            else:
                fout.write("%s\t1\t%s\t%s\n" % ("\t".join(l), l[3], l[4]))


def create_mutations(mc3_path: str, sample_txt: str, out_mutation: str) -> None:
    """Build mutation.txt from the mc3 file: every mc3 column plus the sample idx
    (from sample.txt's order). Mirrors the Perl."""
    pos = {}
    with open(sample_txt, encoding="utf-8") as fin:
        for i, line in enumerate(fin):
            pos[line.split("\t", 1)[0]] = i
    with open(mc3_path, encoding="utf-8") as fin, open(out_mutation, "w", encoding="utf-8") as fout:
        next(fin, None)
        for line in fin:
            f = line.rstrip("\n").split("\t")
            idx = pos.get(f[0], "")
            fout.write("\t".join(f + [str(idx)]) + "\n")


def _gene_name_by_id(gene_txt: str) -> Dict[str, str]:
    out = {}
    with open(gene_txt, encoding="utf-8") as fin:
        for line in fin:
            f = line.rstrip("\n").split("\t")
            if len(f) >= 2:
                out[f[0]] = f[1]
    return out


def build(outdir: str, keep: bool = False) -> str:
    """Full download→build pipeline for the frozen Xena PanCanAtlas release.

    Downloads the CNV / thresholded-CNV / expression / RPPA matrices, the phenotype +
    clinical + mutation files and the two probemaps, reshapes them, and assembles
    ``tcga.sqlite``.
    :returns: Path to ``tcga.sqlite``.
    """
    os.makedirs(outdir, exist_ok=True)

    def fetch(key, name, gz=False):
        dest = os.path.join(outdir, name)
        from .common import download, gunzip
        path = download(URLS[key], dest + (".gz" if gz else ""))
        return gunzip(path) if gz else path

    disease = fetch("disease", "disease.tsv", gz=True)
    clinical = fetch("clinical", "clinical.tsv")
    probemap = fetch("probemap", "probemap.txt")
    probemap2 = fetch("probemap2", "probemap2.txt")
    mutation_src = fetch("mutation", "mc3.xena", gz=True)
    cnv = fetch("cnv", "cnv.gct", gz=True)
    cnvt = fetch("cnvt", "cnvt.gct", gz=True)
    rna = fetch("rna", "rna.gct", gz=True)
    prt = fetch("protein", "rppa.xena", gz=True)

    sample_txt = os.path.join(outdir, "sample.txt")
    gene_txt = os.path.join(outdir, "gene.txt")
    mutation_txt = os.path.join(outdir, "mutation.txt")
    create_samples(disease, clinical, sample_txt)
    create_genes(probemap, probemap2, gene_txt)
    create_mutations(mutation_src, sample_txt, mutation_txt)

    db = assemble(outdir, sample_txt, gene_txt, mutation_txt,
                  matrices={"cnv": cnv, "cnvt": cnvt, "rna": rna, "prt": prt},
                  gene_name_by_id=_gene_name_by_id(gene_txt))

    if not keep:
        for f in (disease, clinical, probemap, probemap2, mutation_src, cnv, cnvt, rna, prt,
                  sample_txt, gene_txt, mutation_txt,
                  os.path.join(outdir, "cnv.txt"), os.path.join(outdir, "cnvt.txt"),
                  os.path.join(outdir, "rna.txt"), os.path.join(outdir, "prt.txt"),
                  os.path.join(outdir, "json.txt"), os.path.join(outdir, "index.txt")):
            if os.path.exists(f):
                os.remove(f)
    return db
