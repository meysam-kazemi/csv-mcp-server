import csv_mcp
from concurrent.futures import ThreadPoolExecutor


def test_read_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_mcp, "ROOT", tmp_path.resolve())
    (tmp_path / "people.csv").write_text("name,age\nAda,36\nLinus,54\n", encoding="utf-8")

    assert csv_mcp.list_csv_files() == ["people.csv"]
    inspection = csv_mcp.inspect_csv("people.csv")
    assert inspection["columns"] == ["name", "age"]
    assert inspection["row_count"] == 2
    assert inspection["possible_types"] == {"name": "string", "age": "integer"}
    assert csv_mcp.read_csv("people.csv", offset=1)["rows"] == [{"name": "Linus", "age": "54"}]


def test_rejects_unsafe_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_mcp, "ROOT", tmp_path.resolve())

    try:
        csv_mcp.read_csv("../outside.csv")
    except ValueError as error:
        assert "inside CSV_MCP_ROOT" in str(error)
    else:
        raise AssertionError("unsafe path was accepted")

    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    for unsafe in ("escape/secret.csv", "https://example.com/data.csv", r"C:\Users\secret.csv"):
        try:
            csv_mcp.create_csv(unsafe, ["value"])
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe path was accepted: {unsafe}")


def test_write_and_edit_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_mcp, "ROOT", tmp_path.resolve())

    assert csv_mcp.create_csv(
        "people.csv",
        ["name", "age"],
        [{"name": "Ada", "age": "36"}],
    )["row_count"] == 1
    appended = csv_mcp.append_rows(
        "people.csv",
        [{"name": "Linus", "age": "54"}],
        output_file="people-appended.csv",
    )
    assert len(appended["sha256"]) == 64
    updated = csv_mcp.update_rows(
        "people-appended.csv",
        [csv_mcp.Filter(column="name", operator="=", value="Ada")],
        {"age": "37"},
        output_file="people-updated.csv",
        dry_run=False,
    )
    assert updated["changed_rows"] == 1
    deleted = csv_mcp.delete_rows(
        "people-updated.csv",
        [csv_mcp.Filter(column="name", operator="=", value="Linus")],
        output_file="people-final.csv",
        dry_run=False,
    )
    assert deleted["matched_rows"] == 1
    assert csv_mcp.read_csv("people-final.csv")["rows"] == [{"name": "Ada", "age": "37"}]
    assert csv_mcp.read_csv("people.csv")["rows"] == [{"name": "Ada", "age": "36"}]


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


def test_parsing_options_and_detection(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_mcp, "ROOT", tmp_path.resolve())
    (tmp_path / "europe.csv").write_bytes("code;amount\r\n00123;12,50\r\n".encode("windows-1252"))
    (tmp_path / "raw.tsv").write_text("Ada\t36\nLinus\t54\n", encoding="utf-8")

    inspection = csv_mcp.inspect_csv("europe.csv")
    assert inspection["delimiter"] == ";"
    assert inspection["possible_types"]["code"] == "string"
    assert csv_mcp.preview_csv(
        "raw.tsv",
        columns=["person"],
        options=csv_mcp.CsvOptions(header_mode="none", column_names=["person", "age"]),
    )["rows"] == [{"person": "Ada"}, {"person": "Linus"}]


def test_reports_malformed_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_mcp, "ROOT", tmp_path.resolve())
    (tmp_path / "bad.csv").write_text("name,age\nAda,36\nLinus\n", encoding="utf-8")

    inspection = csv_mcp.inspect_csv("bad.csv")
    assert inspection["malformed_rows"] == 1
    try:
        csv_mcp.preview_csv("bad.csv")
    except ValueError as error:
        assert "wrong number of fields" in str(error)
    else:
        raise AssertionError("malformed rows were accepted")


def test_query_and_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_mcp, "ROOT", tmp_path.resolve())
    (tmp_path / "sales.csv").write_text(
        "region,customer,amount\nwest,Acme,125.50\neast,Beta,75\nwest,Apex,200\n",
        encoding="utf-8",
    )

    result = csv_mcp.query_csv(
        "sales.csv",
        select=["customer", "amount"],
        filters=[csv_mcp.Filter(column="amount", operator=">", value=100)],
        sort=[csv_mcp.Sort(column="amount", direction="desc")],
    )
    assert [row["customer"] for row in result["rows"]] == ["Apex", "Acme"]
    summary = csv_mcp.summarize_csv(
        "sales.csv",
        group_by=["region"],
        aggregations=[csv_mcp.Aggregation(column="amount", function="sum", output_name="total")],
    )
    assert summary["rows"] == [{"region": "west", "total": 325.5}, {"region": "east", "total": 75.0}]


def test_validation_and_comparison(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_mcp, "ROOT", tmp_path.resolve())
    (tmp_path / "old.csv").write_text("id,date,name\n01,2026-01-01,Ada\n02,bad,Bob\n", encoding="utf-8")
    (tmp_path / "new.csv").write_text(
        "id,date,name\n01,2026-01-01,Ada Lovelace\n03,2026-02-01,Linus\n",
        encoding="utf-8",
    )

    validation = csv_mcp.validate_csv(
        "old.csv",
        {
            "id": csv_mcp.ColumnRule(required=True, unique=True),
            "date": csv_mcp.ColumnRule(type="date", required=True),
        },
    )
    assert not validation["valid"]
    assert validation["issues"][0]["column"] == "date"
    comparison = csv_mcp.compare_csv("old.csv", "new.csv", ["id"])
    assert comparison["added_rows"] == 1
    assert comparison["removed_rows"] == 1
    assert comparison["changed_rows"] == 1


def test_dry_run_formula_policy_and_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_mcp, "ROOT", tmp_path.resolve())
    csv_mcp.create_csv(
        "values.csv",
        ["label", "amount"],
        [{"label": "=cmd()", "amount": "-12.5"}],
        column_types={"amount": "decimal"},
    )
    assert csv_mcp.preview_csv("values.csv")["rows"] == [{"label": "'=cmd()", "amount": "-12.5"}]
    result = csv_mcp.update_rows(
        "values.csv",
        [csv_mcp.Filter(column="amount", operator="<", value=0)],
        {"label": "negative"},
    )
    assert result["changed_rows"] == 1
    assert not result["file_written"]
    assert not (tmp_path / "output" / "values-updated.csv").exists()

    try:
        csv_mcp.create_csv("values.csv", ["name"])
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing output was overwritten")


def test_clean_and_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_mcp, "ROOT", tmp_path.resolve())
    (tmp_path / "one.csv").write_text("id,email\n1, ADA@EXAMPLE.COM \n1, ADA@EXAMPLE.COM \n", encoding="utf-8")
    (tmp_path / "two.csv").write_text("id,email\n2,bob@example.com\n", encoding="utf-8")

    cleaned = csv_mcp.clean_csv(
        "one.csv",
        [
            csv_mcp.CleanOperation(operation="trim_whitespace", columns=["email"]),
            csv_mcp.CleanOperation(operation="lowercase", columns=["email"]),
            csv_mcp.CleanOperation(operation="drop_duplicates", columns=["id"]),
        ],
    )
    assert cleaned["removed_duplicates"] == 1
    merged = csv_mcp.merge_csv(
        ["output/one-clean.csv", "two.csv"],
        "concatenate",
        "output/all.csv",
    )
    assert merged["row_count"] == 2
    assert csv_mcp.preview_csv("output/all.csv")["rows"][0]["email"] == "ada@example.com"


def test_limits_and_simultaneous_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_mcp, "ROOT", tmp_path.resolve())
    monkeypatch.setattr(csv_mcp, "MAX_FILE_SIZE", 10)
    (tmp_path / "large.csv").write_text("name\nlong-value\n", encoding="utf-8")
    try:
        csv_mcp.preview_csv("large.csv")
    except ValueError as error:
        assert "file exceeds" in str(error)
    else:
        raise AssertionError("oversized file was accepted")

    monkeypatch.setattr(csv_mcp, "MAX_FILE_SIZE", 1024)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(csv_mcp.create_csv, "same.csv", ["id"], [{"id": str(index)}]) for index in range(2)]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except FileExistsError:
            outcomes.append("collision")
    assert len([result for result in outcomes if result == "collision"]) == 1
