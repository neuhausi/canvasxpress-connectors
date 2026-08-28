import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cxdb_etl import ccle  # noqa: E402
from cx_connectors.sources.packed import PackedMatrixSource  # noqa: E402


def test_ccle_pack_and_roundtrip(tmp_path):
    # samples-as-rows, genes-as-cols; ACH2 before ACH1 to prove sorting.
    (tmp_path / "rna.csv").write_text(
        "sample,TP53 (7157),KRAS (3845)\nACH2,2.0,3.0\nACH1,5.0,1.0\n")
    (tmp_path / "cnv.csv").write_text(
        "sample,TP53 (7157),KRAS (3845)\nACH2,0.2,0.3\nACH1,0.5,0.1\n")
    (tmp_path / "sample.txt").write_text(
        "ACH1\tLine1\ts1\tc1\tFemale\tovary\tPrimary\tOvarian Cancer\tovary\tsub1\t0\n"
        "ACH2\tLine2\ts2\tc2\tMale\tblood\tPrimary\tLeukemia\tblood\tsub2\t1\n")
    (tmp_path / "gene.txt").write_text("ENSG1\tTP53\tchr17\t100\t200\t-\n")
    (tmp_path / "mutation.txt").write_text(
        "ACH1\tchr17\t100\t101\tA\tT\tTP53\tENST1\tMissense\tg.1A>T\tp.X1Y\t0\n")

    db = ccle.assemble(str(tmp_path), str(tmp_path / "sample.txt"),
                       str(tmp_path / "gene.txt"), str(tmp_path / "mutation.txt"),
                       str(tmp_path / "rna.csv"), str(tmp_path / "cnv.csv"))

    url = "sqlite:///" + db
    d = PackedMatrixSource(url, "rnaseq", "log2tpm", "rna1", genes=["TP53"]).read_cx()
    assert d["y"]["vars"] == ["TP53"]
    assert d["y"]["smps"] == ["ACH1", "ACH2"]          # sorted
    assert d["y"]["data"] == [[5.0, 2.0]]              # ACH1's 5.0 first
    assert d["x"]["disease"] == ["Ovarian Cancer", "Leukemia"]
    # cnv packs + templates too
    c = PackedMatrixSource(url, "cnv", "cnratio", "cnv1", genes=["KRAS"]).read_cx()
    assert c["y"]["data"] == [[0.1, 0.3]]
