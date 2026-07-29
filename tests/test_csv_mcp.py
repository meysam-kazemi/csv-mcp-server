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


def test_write_and_edit_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_mcp, "ROOT", tmp_path.resolve())

    assert csv_mcp.create_csv(
        "people.csv",
        ["name", "age"],
        [{"name": "Ada", "age": "36"}],
    )["row_count"] == 1
    csv_mcp.append_rows("people.csv", [{"name": "Linus", "age": "54"}])
    assert csv_mcp.update_rows("people.csv", {"name": "Ada"}, {"age": "37"})["updated"] == 1
    assert csv_mcp.delete_rows("people.csv", {"name": "Linus"})["deleted"] == 1
    assert csv_mcp.read_csv("people.csv")["rows"] == [{"name": "Ada", "age": "37"}]


def test_write_validation_preserves_file(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_mcp, "ROOT", tmp_path.resolve())
    path = tmp_path / "people.csv"
    path.write_text("name,age\nAda,36\n", encoding="utf-8")

    try:
        csv_mcp.append_rows("people.csv", [{"name": "Linus"}])
    except ValueError as error:
        assert "exactly these columns" in str(error)
    else:
        raise AssertionError("invalid row was accepted")

    assert path.read_text(encoding="utf-8") == "name,age\nAda,36\n"
