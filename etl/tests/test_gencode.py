import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cxdb_etl import gencode  # noqa: E402

# Each GTF row is split across physical lines (adjacent-string concatenation) purely to
# stay within the 100-col lint limit; the assembled string content is unchanged.
GTF = (
    "##description: test\n"
    'chr17\tHAVANA\tgene\t100\t200\t.\t-\t.\t'
    'gene_id "ENSG1"; gene_name "TP53"; gene_type "protein_coding";\n'
    'chr17\tHAVANA\ttranscript\t100\t200\t.\t-\t.\t'
    'gene_id "ENSG1"; gene_name "TP53"; gene_type "protein_coding"; '
    'transcript_id "ENST1";\n'
    'chr17\tHAVANA\texon\t100\t150\t.\t-\t.\t'
    'gene_id "ENSG1"; gene_name "TP53"; gene_type "protein_coding"; '
    'transcript_id "ENST1"; exon_number 1;\n'
    'chr17\tHAVANA\texon\t160\t200\t.\t-\t.\t'
    'gene_id "ENSG1"; gene_name "TP53"; gene_type "protein_coding"; '
    'transcript_id "ENST1"; exon_number 2;\n'
    'chr12\tHAVANA\tgene\t300\t400\t.\t+\t.\t'
    'gene_id "ENSG2"; gene_name "KRAS"; gene_type "protein_coding";\n'
    'chr12\tHAVANA\ttranscript\t300\t400\t.\t+\t.\t'
    'gene_id "ENSG2"; gene_name "KRAS"; gene_type "protein_coding"; '
    'transcript_id "ENST2";\n'
    'chr12\tHAVANA\texon\t300\t400\t.\t+\t.\t'
    'gene_id "ENSG2"; gene_name "KRAS"; gene_type "protein_coding"; '
    'transcript_id "ENST2"; exon_number 1;\n'
)


def _gwas_tsv():
    cols = ["c%d" % i for i in range(35)]
    cols[1] = "12345678"
    cols[7] = "Body mass index"
    cols[11] = "17"
    cols[12] = "7676000"
    cols[20] = "rs1-A"
    cols[27] = "2.0e-10"
    cols[30] = "1.5"
    return "header\n" + "\t".join(cols) + "\n"


def test_gencode_build(tmp_path):
    gtf = tmp_path / "g.gtf"
    gtf.write_text(GTF)
    gwas = tmp_path / "gwas.tsv"
    gwas.write_text(_gwas_tsv())
    db = gencode.build(str(tmp_path), keep=False, gtf_path=str(gtf), gwas_path=str(gwas))
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT geneName, chrom, strand, txStart, txEnd, exonCount, exonStarts, exonEnds "
        "FROM genome ORDER BY geneName").fetchall()
    assert rows[0][0] == "KRAS"
    assert rows[1] == ("TP53", "chr17", "-", 100, 200, 2, "100,160", "150,200")
    g = conn.execute("SELECT diseaseTrait, chrom, start, pValue, ORorBeta FROM gwas").fetchone()
    assert g == ("Body mass index", "chr17", 7676000, 2e-10, 1.5)
    conn.close()
