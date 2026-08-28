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

    packed_sets: Dict[str, Tuple[List[str], List[str], List[str]]] = {}
    imports = [(sample_txt, "sample"), (gene_txt, "gene")]
    table_for = {"cnv": "cnv", "cnvt": "cnvt", "rna": "rnaseq", "prt": "rppa"}

    for key, path in matrices.items():
        labels, smps, packed = pack_rows(path)
        packed_sets[key] = (labels, smps, packed)
        out = os.path.join(outdir, "%s.txt" % key)
        if key == "rna":
            ids = labels
            names = [gene_name_by_id.get(i, i) for i in labels]
            _write_packed(names, packed, out, ids=ids)
        else:
            _write_packed(labels, packed, out)
        imports.append((out, table_for[key]))

    # json templates (per data type) + cross-type correlation templates.
    json_txt = os.path.join(outdir, "json.txt")
    with open(json_txt, "w", encoding="utf-8") as fj:
        for key in matrices:
            smps = packed_sets[key][1]
            tpls = build_templates(samples, smps)
            for suffix, s in tpls.items():
                fj.write("%s%s\t%s\n" % (key, suffix, s))
        # Cross pairs present in both, on the z axis (correlation view).
        keys = list(matrices)
        for a in keys:
            for b in keys:
                if a < b:
                    pa = pair_indices(packed_sets[a][1], packed_sets[b][1])
                    paired = [packed_sets[a][1][i] for i in pa[0]]
                    for suffix in ("2min", "2med"):
                        tpls = build_templates(samples, paired)
                        fj.write("%s-%s%s\t%s\n" % (a, b, suffix, tpls[suffix]))
        fj.write("survival\t%s\n" % survival_template(samples, global_sorted))

    index_txt = os.path.join(outdir, "index.txt")
    with open(index_txt, "w", encoding="utf-8") as fi:
        for key in matrices:
            fi.write("%s\t%s\n" % (key, json.dumps(single_indices(global_pos, packed_sets[key][1]))))
        keys = list(matrices)
        for a in keys:
            for b in keys:
                if a < b:
                    fi.write("%s-%s\t%s\n" % (a, b, json.dumps(
                        pair_indices(packed_sets[a][1], packed_sets[b][1]))))

    imports += [(json_txt, "json"), (index_txt, "indices")]
    db_path = os.path.join(outdir, "tcga.sqlite")
    build_sqlite(db_path, SCHEMA, imports)
    return db_path


def build(outdir: str, keep: bool = False) -> str:  # pragma: no cover - needs downloads
    """Full download→build pipeline. Not exercised in tests (needs the large Xena
    dumps). Wire the Xena URLs + confirm the clinical/mutation column layouts against
    current files, then call ``assemble``; see the SCHEMA note above."""
    raise NotImplementedError(
        "tcga.build downloads the current Xena PanCanAtlas files; wire the URLs and "
        "the sample/mutation column layouts to current files, then call assemble()."
    )
