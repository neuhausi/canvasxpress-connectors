import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cxdb_etl import tcga  # noqa: E402
from cx_connectors.sources.packed import PackedMatrixSource  # noqa: E402


def _sample_row(sid, cancer, abbr, age, gender, race, idx):
    # id + 36 fields (35 clinical + idx). Fill positions 0..6, pad, idx last.
    m = [""] * 36
    m[0] = "Tumor"; m[1] = cancer; m[2] = "pat"; m[3] = abbr
    m[4] = str(age); m[5] = gender; m[6] = race
    m[35] = str(idx)
    return "\t".join([sid] + m) + "\n"


def test_tcga_pack_and_roundtrip(tmp_path):
    (tmp_path / "rna.tsv").write_text("gene\tS2\tS1\nENSG1\t2.0\t5.0\n")
    (tmp_path / "cnv.tsv").write_text("gene\tS2\tS1\nTP53\t0.2\t0.5\n")
    (tmp_path / "sample.txt").write_text(
        _sample_row("S1", "Breast Cancer", "BRCA", 50, "Female", "White", 0) +
        _sample_row("S2", "Lung Cancer", "LUAD", 61, "Male", "Asian", 1))
    (tmp_path / "gene.txt").write_text("ENSG1\tTP53\tchr17\t100\t200\t-\t1\t100\t200\n")
    (tmp_path / "mutation.txt").write_text(
        "S1\tchr17\t100\t101\tA\tT\tTP53\tMissense\tp.X1Y\t0.3\tdel\tprob\t0\n")

    db = tcga.assemble(
        str(tmp_path), str(tmp_path / "sample.txt"), str(tmp_path / "gene.txt"),
        str(tmp_path / "mutation.txt"),
        matrices={"rna": str(tmp_path / "rna.tsv"), "cnv": str(tmp_path / "cnv.tsv")},
        gene_name_by_id={"ENSG1": "TP53"})

    url = "sqlite:///" + db
    d = PackedMatrixSource(url, "rnaseq", "log2tpm", "rna1min",
                           name_col="name", genes=["TP53"]).read_cx()
    assert d["y"]["vars"] == ["TP53"]
    assert d["y"]["smps"] == ["S1", "S2"]
    assert d["y"]["data"] == [[5.0, 2.0]]
    assert d["x"]["cancer"] == ["Breast Cancer", "Lung Cancer"]
    m = PackedMatrixSource(url, "rnaseq", "log2tpm", "rna1med",
                           name_col="name", genes=["TP53"]).read_cx()
    assert m["x"]["gender"] == ["Female", "Male"]
    c = PackedMatrixSource(url, "cnv", "gistic2", "cnv1min", genes=["TP53"]).read_cx()
    assert c["y"]["data"] == [[0.5, 0.2]]
