import pytest

from cx_connectors.reshape import rows_to_cx

HEADER = ["sample", "GeneA", "GeneB", "Category", "Group"]
ROWS = [
    ["Sample1", 11, 13, "A", "X"],
    ["Sample2", 25, 16, "A", "X"],
    ["Sample3", 12, 9, "B", "Y"],
]


def test_shapes_numeric_and_annotations():
    cx = rows_to_cx(HEADER, ROWS)
    assert cx["y"]["vars"] == ["GeneA", "GeneB"]
    assert cx["y"]["smps"] == ["Sample1", "Sample2", "Sample3"]
    assert cx["y"]["data"] == [[11.0, 25.0, 12.0], [13.0, 16.0, 9.0]]
    assert cx["x"] == {"Category": ["A", "A", "B"], "Group": ["X", "X", "Y"]}


def test_no_annotations_key_when_all_numeric():
    cx = rows_to_cx(["sample", "v"], [["s1", 1], ["s2", 2]])
    assert "x" not in cx
    assert cx["y"]["vars"] == ["v"]


def test_sample_ids_coerced_to_str():
    cx = rows_to_cx(["id", "v"], [[1, 10], [2, 20]])
    assert cx["y"]["smps"] == ["1", "2"]


def test_empty_rows_raises():
    with pytest.raises(ValueError):
        rows_to_cx(HEADER, [])
