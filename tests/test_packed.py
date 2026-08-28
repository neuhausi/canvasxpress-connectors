import json
import sqlite3

import pytest

from cx_connectors.sources.packed import PackedMatrixSource
from cx_connectors.store import Store, generate_key


def _packed_db(path):
    """Build a tiny CCLE-shaped packed DB: a json template + a packed rnaseq table."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE json (key TEXT, str BLOB)")
    conn.execute("CREATE TABLE rnaseq (name TEXT, log2tpm BLOB)")
    template = {
        "data": {
            "y": {"vars": [], "smps": ["c1", "c2", "c3"], "data": []},
            "x": {"disease": ["Lung", "Blood", "Lung"], "lineage": ["lung", "blood", "lung"]},
        }
    }
    conn.execute("INSERT INTO json VALUES ('rna1', ?)", (json.dumps(template),))
    conn.executemany("INSERT INTO rnaseq VALUES (?, ?)", [
        ("TP53", json.dumps([5.1, 2.2, 6.3])),
        ("KRAS", json.dumps([1.0, 9.0, 3.0])),
    ])
    conn.commit()
    conn.close()


def test_packed_assembles_single_gene(tmp_path):
    db = str(tmp_path / "ccle.sqlite")
    _packed_db(db)
    data = PackedMatrixSource("sqlite:///" + db, "rnaseq", "log2tpm", "rna1",
                              genes=["TP53"]).read_cx()
    assert data["y"]["vars"] == ["TP53"]
    assert data["y"]["smps"] == ["c1", "c2", "c3"]
    assert data["y"]["data"] == [[5.1, 2.2, 6.3]]
    assert data["x"]["disease"] == ["Lung", "Blood", "Lung"]   # annotations preserved


def test_packed_multi_gene_preserves_requested_order(tmp_path):
    db = str(tmp_path / "ccle.sqlite")
    _packed_db(db)
    data = PackedMatrixSource("sqlite:///" + db, "rnaseq", "log2tpm", "rna1",
                              genes=["KRAS", "TP53"]).read_cx()
    assert data["y"]["vars"] == ["KRAS", "TP53"]
    assert data["y"]["data"] == [[1.0, 9.0, 3.0], [5.1, 2.2, 6.3]]


def test_packed_missing_gene_skipped_no_genes_empty(tmp_path):
    db = str(tmp_path / "ccle.sqlite")
    _packed_db(db)
    data = PackedMatrixSource("sqlite:///" + db, "rnaseq", "log2tpm", "rna1",
                              genes=["NOPE", "TP53"]).read_cx()
    assert data["y"]["vars"] == ["TP53"]
    empty = PackedMatrixSource("sqlite:///" + db, "rnaseq", "log2tpm", "rna1", genes=[]).read_cx()
    assert empty["y"]["vars"] == [] and empty["y"]["data"] == []


def test_packed_rejects_bad_identifier(tmp_path):
    db = str(tmp_path / "ccle.sqlite")
    _packed_db(db)
    with pytest.raises(ValueError):
        PackedMatrixSource("sqlite:///" + db, "rnaseq; DROP TABLE json", "log2tpm", "rna1")


def test_store_roundtrips_packed_kind_and_config(tmp_path):
    store = Store(str(tmp_path / "app.db"), generate_key())
    store.create_user("alice", "secret1")
    cfg = {"table": "rnaseq", "value_col": "log2tpm", "template_key": "rna1", "gene_param": "genes"}
    store.save_source("alice", "ccle-rna", "sqlite:///x.db", "", kind="packed", config=cfg)
    rec = store.get_source("alice", "ccle-rna")
    assert rec["kind"] == "packed"
    assert rec["config"] == cfg
    # A plain sql source still defaults to kind 'sql'.
    store.save_source("alice", "plain", "sqlite:///x.db", "SELECT 1")
    assert store.get_source("alice", "plain")["kind"] == "sql"


def test_data_endpoint_serves_packed_source(tmp_path):
    from fastapi.testclient import TestClient
    from cx_connectors.web.byo_app import create_byo_app

    db = str(tmp_path / "ccle.sqlite")
    _packed_db(db)
    store = Store(str(tmp_path / "app.db"), generate_key())
    store.create_user("alice", "secret1")
    store.save_source("alice", "ccle-rna", "sqlite:///" + db, "", kind="packed",
                      config={"table": "rnaseq", "value_col": "log2tpm",
                              "template_key": "rna1", "gene_param": "genes"})
    app = create_byo_app(store=store, session_secret="test",
                         encryption_key=generate_key(), serve_static=False)
    client = TestClient(app)
    client.post("/auth/login", json={"username": "alice", "password": "secret1"})

    out = client.get("/api/data", params={"source": "ccle-rna", "genes": "TP53,KRAS"}).json()
    assert out["y"]["vars"] == ["TP53", "KRAS"]
    assert out["x"]["disease"] == ["Lung", "Blood", "Lung"]
