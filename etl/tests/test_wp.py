import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cxdb_etl import wp  # noqa: E402


def test_wp_build(tmp_path):
    # descriptor = name%version%wpId%taxName ; then url ; then gene ids
    gmt = tmp_path / "Homo_sapiens.gmt"
    gmt.write_text("Cell Cycle%20220310%WP179%Homo sapiens\thttp://x\t7157\t3845\n")
    gene_info = tmp_path / "gene_info"
    gene_info.write_text(
        "#header\n"
        "9606\t7157\ttp53\t-\t-\tEnsembl:ENSG00000141510\t-\t-\ttumor protein p53\n"
        "9606\t3845\tkras\t-\t-\tEnsembl:ENSG00000133703\t-\t-\tKRAS proto-oncogene\n"
        "9606\t9999\tzzz\t-\t-\t-\t-\t-\tnot a member\n"   # not referenced -> dropped
    )
    names = tmp_path / "names.dmp"
    names.write_text("9606\t|\tHomo sapiens\t|\t\t|\tscientific name\t|\n")

    db = wp.build(str(tmp_path), keep=False,
                  gmt_files=[str(gmt)], gene_info=str(gene_info), names_dmp=str(names),
                  gpml_map={"WP179": "Hs_Cell_Cycle_WP179_1.gpml"})
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT wpId,name,version,taxName,url FROM pathway").fetchone() == (
        "WP179", "Cell Cycle", "20220310", "Homo sapiens",
        "/assets/gpml/Hs_Cell_Cycle_WP179_1.gpml")
    assert sorted(conn.execute("SELECT geneId,wpId FROM members").fetchall()) == \
        [(3845, "WP179"), (7157, "WP179")]
    genes = dict(conn.execute("SELECT geneId,symbol FROM gene").fetchall())
    assert genes == {7157: "TP53", 3845: "KRAS"}   # uppercased, non-member dropped
    assert conn.execute("SELECT taxId,name FROM taxonomy").fetchone() == (9606, "Homo sapiens")
    conn.close()
