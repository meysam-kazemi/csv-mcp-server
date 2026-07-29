import csv
import codecs
import os
import tempfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path, PureWindowsPath
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, field_validator


ROOT = Path(os.environ.get("CSV_MCP_ROOT", ".")).resolve()
MAX_FILE_SIZE = int(os.environ.get("CSV_MCP_MAX_FILE_SIZE", 50 * 1024 * 1024))
MAX_ROWS = int(os.environ.get("CSV_MCP_MAX_ROWS", 1_000_000))
MAX_COLUMNS = int(os.environ.get("CSV_MCP_MAX_COLUMNS", 500))
MAX_FIELD_LENGTH = int(os.environ.get("CSV_MCP_MAX_FIELD_LENGTH", 1024 * 1024))
MAX_RETURNED_ROWS = int(os.environ.get("CSV_MCP_MAX_RETURNED_ROWS", 1000))
ENCODINGS = {"utf-8", "utf-8-sig", "utf-16", "latin-1", "windows-1252"}
DELIMITERS = {",", ";", "\t", "|"}
csv.field_size_limit(MAX_FIELD_LENGTH)
mcp = FastMCP(
    "csv",
    instructions=f"Read and edit CSV files under {ROOT}. Paths are relative to this directory.",
)


class CsvOptions(BaseModel):
    """Controlled CSV parsing options."""

    encoding: str | None = None
    delimiter: str | None = None
    quotechar: str = '"'
    escapechar: str | None = None
    doublequote: bool = True
    header_mode: str = "first_row"
    column_names: list[str] | None = None
    null_values: list[str] = Field(default_factory=list)
    keep_empty_strings: bool = True

    @field_validator("encoding")
    @classmethod
    def valid_encoding(cls, value: str | None) -> str | None:
        if value and value.lower() not in ENCODINGS:
            raise ValueError(f"encoding must be one of {sorted(ENCODINGS)}")
        return value.lower() if value else None

    @field_validator("delimiter")
    @classmethod
    def valid_delimiter(cls, value: str | None) -> str | None:
        aliases = {"comma": ",", "semicolon": ";", "tab": "\t", "pipe": "|"}
        value = aliases.get(value or "", value)
        if value and value not in DELIMITERS:
            raise ValueError("delimiter must be comma, semicolon, tab, pipe, or its character")
        return value

    @field_validator("quotechar", "escapechar")
    @classmethod
    def single_character(cls, value: str | None) -> str | None:
        if value is not None and len(value) != 1:
            raise ValueError("quotechar and escapechar must be one character")
        return value

    @field_validator("header_mode")
    @classmethod
    def valid_header_mode(cls, value: str) -> str:
        if value not in {"first_row", "none"}:
            raise ValueError("header_mode must be first_row or none")
        return value


def _path(name: str, *, must_exist: bool = True) -> Path:
    if Path(name).is_absolute() or PureWindowsPath(name).is_absolute():
        raise ValueError("absolute paths are not allowed")
    path = (ROOT / name).resolve()
    if path == ROOT or ROOT not in path.parents:
        raise ValueError("path must point to a file inside CSV_MCP_ROOT")
    if path.suffix.lower() not in {".csv", ".tsv"}:
        raise ValueError("path must end in .csv or .tsv")
    if must_exist and not path.is_file():
        raise FileNotFoundError(f"CSV file not found: {name}")
    if must_exist and path.stat().st_size > MAX_FILE_SIZE:
        raise ValueError(f"file exceeds the {MAX_FILE_SIZE}-byte limit")
    return path


def _encoding(path: Path, requested: str | None) -> tuple[str, list[str]]:
    if requested:
        return requested, []
    with path.open("rb") as handle:
        sample = handle.read(65536)
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", []
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16", []
    try:
        sample.decode("utf-8")
        return "utf-8", []
    except UnicodeDecodeError:
        try:
            sample.decode("windows-1252")
            fallback = "windows-1252"
        except UnicodeDecodeError:
            fallback = "latin-1"
        return fallback, [f"Encoding is inferred as {fallback}; provide encoding to confirm."]


def _dialect(path: Path, encoding: str, options: CsvOptions) -> tuple[str, list[str]]:
    if options.delimiter:
        return options.delimiter, []
    if path.suffix.lower() == ".tsv":
        return "\t", []
    with path.open(encoding=encoding, newline="") as handle:
        sample = handle.read(65536)
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter, [
            "Delimiter was inferred heuristically; provide delimiter to confirm."
        ]
    except csv.Error:
        return ",", ["Delimiter detection failed; comma was used."]


def _load(
    path: Path, options: CsvOptions | None = None, *, allow_malformed: bool = False
) -> tuple[list[str], list[dict[str, str | None]], dict[str, Any]]:
    # ponytail: bounded in-memory rows; use chunking when configured limits become too restrictive.
    options = options or CsvOptions()
    encoding, warnings = _encoding(path, options.encoding)
    delimiter, delimiter_warnings = _dialect(path, encoding, options)
    warnings += delimiter_warnings
    with path.open(encoding=encoding, newline="") as handle:
        raw_rows = list(
            csv.reader(
                handle,
                delimiter=delimiter,
                quotechar=options.quotechar,
                escapechar=options.escapechar,
                doublequote=options.doublequote,
                strict=True,
            )
        )
    if len(raw_rows) > MAX_ROWS + 1:
        raise ValueError(f"file exceeds the {MAX_ROWS}-row limit")
    if not raw_rows:
        return [], [], {
            "encoding": encoding,
            "delimiter": delimiter,
            "quotechar": options.quotechar,
            "has_header": options.header_mode == "first_row",
            "malformed_rows": 0,
            "warnings": warnings + ["CSV is empty."],
        }

    if options.header_mode == "first_row":
        fields, data = raw_rows[0], raw_rows[1:]
    else:
        data = raw_rows
        width = max(map(len, raw_rows))
        fields = options.column_names or [f"column_{index}" for index in range(1, width + 1)]
    if not fields or any(not field for field in fields) or len(fields) != len(set(fields)):
        raise ValueError("CSV must have non-empty, unique column names")
    if len(fields) > MAX_COLUMNS:
        raise ValueError(f"CSV exceeds the {MAX_COLUMNS}-column limit")

    malformed = sum(len(row) != len(fields) for row in data)
    if malformed and not allow_malformed:
        raise ValueError(f"CSV contains {malformed} row(s) with the wrong number of fields")
    rows = []
    for raw in data:
        if len(raw) != len(fields):
            continue
        row = {}
        for field, value in zip(fields, raw, strict=True):
            row[field] = None if value in options.null_values and (value or not options.keep_empty_strings) else value
        rows.append(row)
    return fields, rows, {
        "encoding": encoding,
        "delimiter": delimiter,
        "quotechar": options.quotechar,
        "has_header": options.header_mode == "first_row",
        "malformed_rows": malformed,
        "warnings": warnings,
    }


def _reader(path: Path, options: CsvOptions | None = None) -> tuple[list[str], list[dict[str, str | None]]]:
    fields, rows, _ = _load(path, options)
    return fields, rows


def _possible_type(values: list[str | None]) -> str:
    present = [value for value in values if value not in {None, ""}]
    if not present:
        return "unknown"
    if all(value.lower() in {"true", "false"} for value in present):
        return "boolean"
    if any(len(value) > 1 and value[0] == "0" and value[1].isdigit() for value in present):
        return "string"
    if all(value.isdigit() and (value == "0" or not value.startswith("0")) for value in present):
        return "integer"
    try:
        for value in present:
            Decimal(value)
        return "decimal"
    except InvalidOperation:
        pass
    try:
        for value in present:
            date.fromisoformat(value)
        return "date"
    except ValueError:
        return "string"


def _validate_rows(fields: list[str], rows: list[dict[str, str]]) -> None:
    if not fields or any(not field for field in fields) or len(fields) != len(set(fields)):
        raise ValueError("columns must be non-empty and unique")
    for row in rows:
        if set(row) != set(fields):
            raise ValueError(f"each row must contain exactly these columns: {fields}")


def _write(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    _validate_rows(fields, rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
            temporary_name = handle.name
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


@mcp.tool()
def list_csv_files() -> list[str]:
    """List CSV and TSV files below CSV_MCP_ROOT."""
    return sorted(
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*")
        if path.is_file() and path.suffix.lower() in {".csv", ".tsv"}
    )


@mcp.tool()
def inspect_csv(path: str, sample_rows: int = 100, options: CsvOptions | None = None) -> dict[str, Any]:
    """Inspect encoding, dialect, columns, types, malformed rows, and samples."""
    if not 1 <= sample_rows <= MAX_RETURNED_ROWS:
        raise ValueError(f"sample_rows must be between 1 and {MAX_RETURNED_ROWS}")
    source = _path(path)
    fields, rows, metadata = _load(source, options, allow_malformed=True)
    sample = rows[:sample_rows]
    return {
        "file": path,
        "size_bytes": source.stat().st_size,
        **metadata,
        "columns": fields,
        "row_count": len(rows) + metadata["malformed_rows"],
        "sample_rows": sample,
        "possible_types": {field: _possible_type([row[field] for row in sample]) for field in fields},
    }


@mcp.tool()
def preview_csv(
    path: str,
    offset: int = 0,
    limit: int = 50,
    columns: list[str] | None = None,
    options: CsvOptions | None = None,
) -> dict[str, Any]:
    """Read a bounded page and optional subset of columns."""
    if offset < 0 or not 1 <= limit <= MAX_RETURNED_ROWS:
        raise ValueError(f"offset must be non-negative and limit must be between 1 and {MAX_RETURNED_ROWS}")
    fields, rows = _reader(_path(path), options)
    selected = columns or fields
    unknown = set(selected) - set(fields)
    if unknown:
        raise ValueError(f"unknown columns: {sorted(unknown)}")
    page = [{field: row[field] for field in selected} for row in rows[offset : offset + limit]]
    return {
        "file": path,
        "columns": selected,
        "rows": page,
        "offset": offset,
        "returned_rows": len(page),
        "has_more": offset + len(page) < len(rows),
    }


@mcp.tool()
def read_csv(
    path: str,
    offset: int = 0,
    limit: int = 100,
    columns: list[str] | None = None,
    options: CsvOptions | None = None,
) -> dict[str, Any]:
    """Backward-compatible alias for preview_csv."""
    return preview_csv(path, offset, limit, columns, options)


@mcp.tool()
def create_csv(
    path: str,
    columns: list[str],
    rows: list[dict[str, str]] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create a CSV file. Existing files are protected unless overwrite is true."""
    destination = _path(path, must_exist=False)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"CSV file already exists: {path}")
    rows = rows or []
    _write(destination, columns, rows)
    return {"path": path, "created": True, "row_count": len(rows)}


@mcp.tool()
def append_rows(path: str, rows: list[dict[str, str]]) -> dict[str, Any]:
    """Append rows whose keys exactly match the CSV columns."""
    destination = _path(path)
    fields, existing = _reader(destination)
    _write(destination, fields, existing + rows)
    return {"path": path, "appended": len(rows), "row_count": len(existing) + len(rows)}


@mcp.tool()
def update_rows(path: str, match: dict[str, str], changes: dict[str, str]) -> dict[str, Any]:
    """Update every row whose values exactly match all supplied match fields."""
    if not match or not changes:
        raise ValueError("match and changes must not be empty")
    destination = _path(path)
    fields, rows = _reader(destination)
    unknown = (set(match) | set(changes)) - set(fields)
    if unknown:
        raise ValueError(f"unknown columns: {sorted(unknown)}")
    updated = 0
    for row in rows:
        if all(row[key] == value for key, value in match.items()):
            row.update(changes)
            updated += 1
    _write(destination, fields, rows)
    return {"path": path, "updated": updated}


@mcp.tool()
def delete_rows(path: str, match: dict[str, str]) -> dict[str, Any]:
    """Delete every row whose values exactly match all supplied match fields."""
    if not match:
        raise ValueError("match must not be empty")
    destination = _path(path)
    fields, rows = _reader(destination)
    unknown = set(match) - set(fields)
    if unknown:
        raise ValueError(f"unknown columns: {sorted(unknown)}")
    kept = [row for row in rows if not all(row[key] == value for key, value in match.items())]
    _write(destination, fields, kept)
    return {"path": path, "deleted": len(rows) - len(kept), "row_count": len(kept)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
