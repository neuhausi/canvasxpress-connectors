import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cxdb_etl import gtex  # noqa: E402
from cx_connectors.sources.packed import PackedMatrixSource  # noqa: E402

GTF = """\
##desc
chr17\tX\tgene\t100\t200\t.\t-\t.\tgene_id "ENSG1.1"; gene_name "TP53";
chr17\tX\ttranscript\t110\t190\t.\t-\t.\tgene_id "ENSG1.1"; gene_name "TP53"; transcript_id "ENST1";
chr17\tX\texon\t160\t190\t.\t-\t.\tgene_id "ENSG1.1"; gene_name "TP53"; exon_number 2;
chr17\tX\texon\t110\t150\t.\t-\t.\tgene_id "ENSG1.1"; gene_name "TP53"; exon_number 1;
"""


def _attr_row(sid, tissue):
    f = [""] * 17
    f[0] = sid; f[6] = tissue; f[16] = "RNASEQ"
    return "\t".join(f)


def test_gtex_build_and_roundtrip(tmp_path):
    (tmp_path / "g.gtf").write_text(GTF)
    (tmp_path / "subjects.txt").write_text(
        "SUBJID\tSEX\tAGE\tDTHHRDY\nGTEX-A\t1\t60-69\t2\nGTEX-B\t2\t40-49\t1\n")
    (tmp_path / "attr.txt").write_text(
        "SAMPID\t...\n" + _attr_row("GTEX-A-0001", "Blood") + "\n"
        + _attr_row("GTEX-B-0001", "Brain") + "\n")
    (tmp_path / "data.gct").write_text(
        "#1.2\n2\t2\nName\tDescription\tGTEX-A-0001\tGTEX-B-0001\n"
        "ENSG1.1\tTP53\t5.1\t2.2\n")

    order = gtex.parse_samples(str(tmp_path / "subjects.txt"), str(tmp_path / "attr.txt"),
                               str(tmp_path / "samples.txt"), str(tmp_path / "samples.json"))
    assert order == ["GTEX-A-0001", "GTEX-B-0001"]
    gtex.parse_genome(str(tmp_path / "g.gtf"), str(tmp_path / "genome.txt"))
    gtex.parse_data(str(tmp_path / "data.gct"), str(tmp_path / "data.txt"),
                    str(tmp_path / "order.txt"), sample_order=order)
    db = gtex.assemble(str(tmp_path), str(tmp_path / "genome.txt"), str(tmp_path / "samples.txt"),
                       str(tmp_path / "samples.json"), str(tmp_path / "data.txt"))

    url = "sqlite:///" + db
    d = PackedMatrixSource(url, "expression", "tpm", None, name_col="geneName",
                           template_col="samples", value_encoding="delimited",
                           value_sep=";", genes=["TP53"]).read_cx()
    assert d["y"]["vars"] == ["TP53"]
    assert d["y"]["smps"] == ["GTEX-A-0001", "GTEX-B-0001"]
    assert d["y"]["data"] == [[5.1, 2.2]]
    assert d["x"]["tissue"] == ["Blood", "Brain"]
    assert d["x"]["sex"] == ["male", "female"]

    import sqlite3
    row = sqlite3.connect(db).execute(
        "SELECT geneName,name,chrom,txStart,txEnd,cdsStart,cdsEnd,exonCount,exonStarts,exonEnds "
        "FROM genome").fetchone()
    assert row == ("TP53", "ENSG1.1", "chr17", 100, 200, 110, 190, 2, "110,160", "150,190")
