import csv_mcp


def test_read_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_mcp, "ROOT", tmp_path.resolve())
    (tmp_path / "people.csv").write_text("name,age\nAda,36\nLinus,54\n", encoding="utf-8")

    assert csv_mcp.list_csv_files() == ["people.csv"]
    assert csv_mcp.inspect_csv("people.csv") == {
        "path": "people.csv",
        "columns": ["name", "age"],
        "row_count": 2,
    }
    assert csv_mcp.read_csv("people.csv", offset=1)["rows"] == [{"name": "Linus", "age": "54"}]


def test_rejects_unsafe_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_mcp, "ROOT", tmp_path.resolve())

    try:
        csv_mcp.read_csv("../outside.csv")
    except ValueError as error:
        assert "inside CSV_MCP_ROOT" in str(error)
    else:
        raise AssertionError("unsafe path was accepted")
