import csv
import codecs
import json
import os
import tempfile
import threading
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

import pandas as pd
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, field_validator


ROOT = Path(os.environ.get("CSV_MCP_ROOT", ".")).resolve()
MAX_FILE_SIZE = int(os.environ.get("CSV_MCP_MAX_FILE_SIZE", 50 * 1024 * 1024))
MAX_ROWS = int(os.environ.get("CSV_MCP_MAX_ROWS", 1_000_000))
MAX_COLUMNS = int(os.environ.get("CSV_MCP_MAX_COLUMNS", 500))
MAX_FIELD_LENGTH = int(os.environ.get("CSV_MCP_MAX_FIELD_LENGTH", 1024 * 1024))
MAX_RETURNED_ROWS = int(os.environ.get("CSV_MCP_MAX_RETURNED_ROWS", 1000))
MAX_FILES = int(os.environ.get("CSV_MCP_MAX_FILES", 10_000))
ENCODINGS = {"utf-8", "utf-8-sig", "utf-16", "latin-1", "windows-1252"}
DELIMITERS = {",", ";", "\t", "|"}
csv.field_size_limit(MAX_FIELD_LENGTH)
WRITE_LOCK = threading.RLock()
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
    decimal_separator: Literal[".", ","] = "."
    column_types: dict[str, Literal["string", "integer", "decimal", "boolean", "date"]] = Field(
        default_factory=dict
    )
    date_formats: dict[str, str] = Field(default_factory=dict)

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


class Filter(BaseModel):
    column: str
    operator: Literal[
        "=", "!=", ">", ">=", "<", "<=", "contains", "starts_with", "ends_with", "is_null", "not_null", "in"
    ]
    value: Any = None


class Sort(BaseModel):
    column: str
    direction: Literal["asc", "desc"] = "asc"


class Aggregation(BaseModel):
    column: str
    function: Literal["count", "sum", "mean", "minimum", "maximum", "median", "unique_count"]
    output_name: str


class ColumnRule(BaseModel):
    type: Literal["string", "integer", "decimal", "boolean", "date"] = "string"
    required: bool = False
    unique: bool = False
    format: str | None = None
    allowed_values: list[str] | None = None


class CleanOperation(BaseModel):
    operation: Literal["trim_whitespace", "lowercase", "uppercase", "drop_duplicates"]
    columns: list[str]


def _serialized(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        with WRITE_LOCK:
            return function(*args, **kwargs)

    return wrapper


def _path(name: str, *, must_exist: bool = True) -> Path:
    if "://" in name:
        raise ValueError("URLs are not allowed")
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


def _possible_type(
    values: list[str | None], decimal_separator: str = ".", date_format: str | None = None
) -> str:
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
            Decimal(value.replace(decimal_separator, "."))
        return "decimal"
    except InvalidOperation:
        pass
    try:
        for value in present:
            datetime.strptime(value, date_format) if date_format else date.fromisoformat(value)
        return "date"
    except ValueError:
        return "string"


def _validate_rows(fields: list[str], rows: list[dict[str, str | None]]) -> None:
    if not fields or any(not field for field in fields) or len(fields) != len(set(fields)):
        raise ValueError("columns must be non-empty and unique")
    for row in rows:
        if set(row) != set(fields):
            raise ValueError(f"each row must contain exactly these columns: {fields}")


def _formula_value(
    value: str | None,
    policy: Literal["preserve", "escape", "reject"],
    column_type: str,
) -> str | None:
    if not value or value[0] not in "=+-@":
        return value
    if value[0] == "-" and column_type in {"integer", "decimal"}:
        try:
            Decimal(value)
            return value
        except InvalidOperation:
            pass
    if policy == "reject":
        raise ValueError("potential spreadsheet formula value was rejected")
    return f"'{value}" if policy == "escape" else value


def _write(
    path: Path,
    fields: list[str],
    rows: list[dict[str, str | None]],
    options: CsvOptions | None = None,
    formula_policy: Literal["preserve", "escape", "reject"] = "escape",
    column_types: dict[str, str] | None = None,
    overwrite: bool = False,
) -> str:
    _validate_rows(fields, rows)
    if len(rows) > MAX_ROWS:
        raise ValueError(f"output exceeds the {MAX_ROWS}-row limit")
    options = options or CsvOptions()
    encoding = options.encoding or "utf-8"
    delimiter = options.delimiter or ("\t" if path.suffix.lower() == ".tsv" else ",")
    write_options = options.model_copy(
        update={"encoding": encoding, "delimiter": delimiter, "column_names": fields}
    )
    column_types = column_types or options.column_types
    safe_rows = [
        {
            field: _formula_value(row[field], formula_policy, column_types.get(field, "string"))
            for field in fields
        }
        for row in rows
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    # ponytail: process-local lock; use OS locks if multiple server processes write one workspace.
    with WRITE_LOCK:
        if path.exists() and not overwrite:
            raise FileExistsError(f"output file already exists: {path.relative_to(ROOT)}")
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding=encoding, newline="", dir=path.parent, delete=False
            ) as handle:
                temporary_name = handle.name
                writer = csv.DictWriter(
                    handle,
                    fieldnames=fields,
                    delimiter=delimiter,
                    quotechar=options.quotechar,
                    escapechar=options.escapechar,
                    doublequote=options.doublequote,
                )
                if options.header_mode == "first_row":
                    writer.writeheader()
                writer.writerows(safe_rows)
            temporary = Path(temporary_name)
            if temporary.stat().st_size > MAX_FILE_SIZE:
                raise ValueError(f"output exceeds the {MAX_FILE_SIZE}-byte limit")
            checked_fields, checked_rows, _ = _load(temporary, write_options)
            if checked_fields != fields or len(checked_rows) != len(rows):
                raise ValueError("temporary output validation failed")
            digest = sha256(temporary.read_bytes()).hexdigest()
            os.replace(temporary, path)
            return digest
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)


def _output_path(source: str, output_file: str | None, operation: str) -> Path:
    if output_file:
        return _path(output_file, must_exist=False)
    path = Path(source)
    return _path(f"output/{path.stem}-{operation}{path.suffix.lower()}", must_exist=False)


def _output_options(options: CsvOptions | None, metadata: dict[str, Any], fields: list[str]) -> CsvOptions:
    options = options or CsvOptions()
    return options.model_copy(
        update={
            "encoding": metadata["encoding"],
            "delimiter": metadata["delimiter"],
            "column_names": fields,
        }
    )


@mcp.tool()
def list_csv_files() -> list[str]:
    """List CSV and TSV files below CSV_MCP_ROOT."""
    files = []
    for path in ROOT.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".csv", ".tsv"}:
            try:
                _path(str(path.relative_to(ROOT)))
            except ValueError:
                continue
            files.append(str(path.relative_to(ROOT)))
            if len(files) >= MAX_FILES:
                break
    return sorted(files)


@mcp.tool()
def inspect_csv(path: str, sample_rows: int = 100, options: CsvOptions | None = None) -> dict[str, Any]:
    """Inspect encoding, dialect, columns, types, malformed rows, and samples."""
    if not 1 <= sample_rows <= MAX_RETURNED_ROWS:
        raise ValueError(f"sample_rows must be between 1 and {MAX_RETURNED_ROWS}")
    source = _path(path)
    options = options or CsvOptions()
    fields, rows, metadata = _load(source, options, allow_malformed=True)
    sample = rows[:sample_rows]
    return {
        "file": path,
        "size_bytes": source.stat().st_size,
        **metadata,
        "columns": fields,
        "row_count": len(rows) + metadata["malformed_rows"],
        "sample_rows": sample,
        "possible_types": {
            field: options.column_types.get(field)
            or _possible_type(
                [row[field] for row in sample],
                options.decimal_separator,
                options.date_formats.get(field),
            )
            for field in fields
        },
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


def _matches(
    row: dict[str, str | None], filters: list[Filter], decimal_separator: str = "."
) -> bool:
    for condition in filters:
        value = row[condition.column]
        expected = condition.value
        if condition.operator == "is_null":
            result = value is None
        elif condition.operator == "not_null":
            result = value is not None
        elif condition.operator == "in":
            if not isinstance(expected, list):
                raise ValueError("the in operator requires a list value")
            result = value in {str(item) for item in expected}
        elif value is None:
            result = False
        elif condition.operator in {"contains", "starts_with", "ends_with"}:
            method = {"contains": "__contains__", "starts_with": "startswith", "ends_with": "endswith"}[
                condition.operator
            ]
            result = getattr(value, method)(str(expected))
        elif condition.operator in {"=", "!="}:
            result = value == str(expected)
            if condition.operator == "!=":
                result = not result
        else:
            left: str | Decimal = value
            right: str | Decimal = str(expected)
            if isinstance(expected, (int, float)):
                try:
                    left, right = Decimal(value.replace(decimal_separator, ".")), Decimal(str(expected))
                except InvalidOperation:
                    result = False
                    if not result:
                        return False
            result = {
                ">": left > right,
                ">=": left >= right,
                "<": left < right,
                "<=": left <= right,
            }[condition.operator]
        if not result:
            return False
    return True


def _validate_columns(fields: list[str], names: set[str]) -> None:
    unknown = names - set(fields)
    if unknown:
        raise ValueError(f"unknown columns: {sorted(unknown)}")


@mcp.tool()
def query_csv(
    path: str,
    select: list[str] | None = None,
    filters: list[Filter] | None = None,
    sort: list[Sort] | None = None,
    limit: int = 100,
    options: CsvOptions | None = None,
) -> dict[str, Any]:
    """Select, filter, and sort rows with controlled operators."""
    if not 1 <= limit <= MAX_RETURNED_ROWS:
        raise ValueError(f"limit must be between 1 and {MAX_RETURNED_ROWS}")
    fields, rows = _reader(_path(path), options)
    selected, filters, sort = select or fields, filters or [], sort or []
    _validate_columns(fields, set(selected) | {item.column for item in filters + sort})
    decimal_separator = options.decimal_separator if options else "."
    matched = [row for row in rows if _matches(row, filters, decimal_separator)]
    for order in reversed(sort):
        values = [row[order.column] for row in matched if row[order.column] not in {None, ""}]
        decimal_separator = options.decimal_separator if options else "."
        numeric = _possible_type(values, decimal_separator) in {"integer", "decimal"}
        matched.sort(
            key=lambda row: (
                row[order.column] is None,
                Decimal(row[order.column].replace(decimal_separator, "."))
                if numeric and row[order.column]
                else row[order.column] or "",
            ),
            reverse=order.direction == "desc",
        )
    result = [{field: row[field] for field in selected} for row in matched[:limit]]
    return {"columns": selected, "rows": result, "returned_rows": len(result), "matched_rows": len(matched)}


def _json_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    return value.item() if hasattr(value, "item") else value


@mcp.tool()
def summarize_csv(
    path: str,
    aggregations: list[Aggregation],
    group_by: list[str] | None = None,
    options: CsvOptions | None = None,
) -> dict[str, Any]:
    """Group and aggregate with a fixed set of functions."""
    if not aggregations:
        raise ValueError("aggregations must not be empty")
    fields, rows = _reader(_path(path), options)
    group_by = group_by or []
    _validate_columns(fields, set(group_by) | {item.column for item in aggregations})
    if len({item.output_name for item in aggregations}) != len(aggregations):
        raise ValueError("aggregation output names must be unique")
    frame = pd.DataFrame(rows, columns=fields)
    numeric_functions = {"sum", "mean", "minimum", "maximum", "median"}
    for item in aggregations:
        if item.function in numeric_functions:
            values = frame[item.column]
            if options and options.decimal_separator != ".":
                values = values.str.replace(options.decimal_separator, ".", regex=False)
            frame[item.column] = pd.to_numeric(values, errors="raise")
    groups = frame.groupby(group_by, dropna=False, sort=False) if group_by else [((), frame)]
    output = []
    for key, group in groups:
        key = key if isinstance(key, tuple) else (key,)
        result = {column: _json_value(value) for column, value in zip(group_by, key, strict=True)}
        for item in aggregations:
            series = group[item.column]
            functions = {
                "count": series.count,
                "sum": series.sum,
                "mean": series.mean,
                "minimum": series.min,
                "maximum": series.max,
                "median": series.median,
                "unique_count": series.nunique,
            }
            result[item.output_name] = _json_value(functions[item.function]())
        output.append(result)
    return {"group_by": group_by, "rows": output}


def _valid_value(value: str | None, rule: ColumnRule) -> bool:
    if value is None or value == "":
        return not rule.required
    try:
        if rule.type == "integer":
            int(value)
        elif rule.type == "decimal":
            Decimal(value)
        elif rule.type == "boolean" and value.lower() not in {"true", "false", "0", "1"}:
            return False
        elif rule.type == "date":
            datetime.strptime(value, rule.format or "%Y-%m-%d")
    except (ValueError, InvalidOperation):
        return False
    return rule.allowed_values is None or value in rule.allowed_values


@mcp.tool()
def validate_csv(
    path: str,
    schema: dict[str, ColumnRule],
    options: CsvOptions | None = None,
    issue_limit: int = 100,
) -> dict[str, Any]:
    """Validate columns, row widths, types, required values, uniqueness, and categories."""
    if not 1 <= issue_limit <= MAX_RETURNED_ROWS:
        raise ValueError(f"issue_limit must be between 1 and {MAX_RETURNED_ROWS}")
    try:
        fields, rows, metadata = _load(_path(path), options, allow_malformed=True)
    except (ValueError, UnicodeError, csv.Error) as error:
        return {"valid": False, "issue_count": 1, "issues": [{"row": None, "message": str(error)}]}
    issues: list[dict[str, Any]] = []
    for column in set(schema) - set(fields):
        if schema[column].required:
            issues.append({"row": 1, "column": column, "message": "missing required column"})
    if metadata["malformed_rows"]:
        issues.append({"row": None, "message": f"{metadata['malformed_rows']} malformed row(s)"})
    for column, rule in schema.items():
        if column not in fields:
            continue
        seen: set[str] = set()
        for number, row in enumerate(rows, start=2):
            value = row[column]
            if not _valid_value(value, rule):
                issues.append({"row": number, "column": column, "message": f"invalid {rule.type} value"})
            if rule.unique and value not in {None, ""}:
                if value in seen:
                    issues.append({"row": number, "column": column, "message": "duplicate value"})
                seen.add(value)
    return {"valid": not issues, "issue_count": len(issues), "issues": issues[:issue_limit]}


@mcp.tool()
def compare_csv(
    left_path: str,
    right_path: str,
    key_columns: list[str],
    sample_limit: int = 20,
    options: CsvOptions | None = None,
) -> dict[str, Any]:
    """Compare two CSV files by unique key columns."""
    if not key_columns or not 0 <= sample_limit <= MAX_RETURNED_ROWS:
        raise ValueError(f"key_columns are required and sample_limit must be between 0 and {MAX_RETURNED_ROWS}")
    left_fields, left_rows = _reader(_path(left_path), options)
    right_fields, right_rows = _reader(_path(right_path), options)
    _validate_columns(left_fields, set(key_columns))
    _validate_columns(right_fields, set(key_columns))

    def indexed(rows: list[dict[str, str | None]]) -> dict[tuple[str | None, ...], dict[str, str | None]]:
        result = {tuple(row[column] for column in key_columns): row for row in rows}
        if len(result) != len(rows):
            raise ValueError("key columns must uniquely identify every row")
        return result

    left, right = indexed(left_rows), indexed(right_rows)
    common = left.keys() & right.keys()
    changed = [key for key in common if left[key] != right[key]]
    samples = [
        {"key": dict(zip(key_columns, key, strict=True)), "left": left[key], "right": right[key]}
        for key in changed[:sample_limit]
    ]
    return {
        "added_rows": len(right.keys() - left.keys()),
        "removed_rows": len(left.keys() - right.keys()),
        "changed_rows": len(changed),
        "unchanged_rows": len(common) - len(changed),
        "differences": samples,
    }


@mcp.tool()
@_serialized
def create_csv(
    output_file: str,
    columns: list[str],
    rows: list[dict[str, str | None]] | None = None,
    options: CsvOptions | None = None,
    spreadsheet_formula_policy: Literal["preserve", "escape", "reject"] = "escape",
    column_types: dict[str, str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create a CSV/TSV file with atomic output and formula-injection handling."""
    destination = _path(output_file, must_exist=False)
    rows = rows or []
    checksum = _write(
        destination,
        columns,
        rows,
        options,
        spreadsheet_formula_policy,
        column_types,
        overwrite,
    )
    return {"output_file": output_file, "row_count": len(rows), "sha256": checksum}


@mcp.tool()
@_serialized
def append_rows(
    path: str,
    rows: list[dict[str, str | None]],
    output_file: str | None = None,
    dry_run: bool = False,
    options: CsvOptions | None = None,
    spreadsheet_formula_policy: Literal["preserve", "escape", "reject"] = "escape",
    column_types: dict[str, str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Append validated rows to a new output file by default."""
    fields, existing, metadata = _load(_path(path), options)
    _validate_rows(fields, rows)
    destination = _output_path(path, output_file, "appended")
    result = {
        "appended_rows": len(rows),
        "row_count": len(existing) + len(rows),
        "output_file": str(destination.relative_to(ROOT)),
        "file_written": not dry_run,
    }
    if not dry_run:
        result["sha256"] = _write(
            destination,
            fields,
            existing + rows,
            _output_options(options, metadata, fields),
            spreadsheet_formula_policy,
            column_types,
            overwrite,
        )
    return result


@mcp.tool()
@_serialized
def update_rows(
    path: str,
    match: list[Filter],
    set: dict[str, str | None],
    output_file: str | None = None,
    dry_run: bool = True,
    options: CsvOptions | None = None,
    spreadsheet_formula_policy: Literal["preserve", "escape", "reject"] = "escape",
    column_types: dict[str, str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Update filtered rows, returning a bounded change preview before optional output."""
    if not match or not set:
        raise ValueError("match and set must not be empty")
    fields, rows, metadata = _load(_path(path), options)
    _validate_columns(fields, {item.column for item in match} | set.keys())
    changes = []
    matched = 0
    changed_rows = 0
    change_count = 0
    for number, row in enumerate(rows, start=2):
        if _matches(row, match, options.decimal_separator if options else "."):
            matched += 1
            row_changed = False
            for column, value in set.items():
                if row[column] != value:
                    row_changed = True
                    change_count += 1
                    if len(changes) < MAX_RETURNED_ROWS:
                        changes.append(
                            {
                                "row_number": number,
                                "column": column,
                                "old_value": row[column],
                                "new_value": value,
                            }
                        )
                    row[column] = value
            changed_rows += row_changed
    destination = _output_path(path, output_file, "updated")
    result = {
        "matched_rows": matched,
        "changed_rows": changed_rows,
        "changes": changes,
        "changes_truncated": change_count > len(changes),
        "output_file": str(destination.relative_to(ROOT)),
        "file_written": not dry_run,
    }
    if not dry_run:
        result["sha256"] = _write(
            destination,
            fields,
            rows,
            _output_options(options, metadata, fields),
            spreadsheet_formula_policy,
            column_types,
            overwrite,
        )
    return result


@mcp.tool()
@_serialized
def delete_rows(
    path: str,
    match: list[Filter],
    output_file: str | None = None,
    dry_run: bool = True,
    options: CsvOptions | None = None,
    spreadsheet_formula_policy: Literal["preserve", "escape", "reject"] = "escape",
    column_types: dict[str, str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Delete filtered rows, requiring a non-empty filter and defaulting to dry-run."""
    if not match:
        raise ValueError("match must not be empty")
    fields, rows, metadata = _load(_path(path), options)
    _validate_columns(fields, {item.column for item in match})
    kept = [
        row
        for row in rows
        if not _matches(row, match, options.decimal_separator if options else ".")
    ]
    destination = _output_path(path, output_file, "filtered")
    result = {
        "matched_rows": len(rows) - len(kept),
        "row_count": len(kept),
        "output_file": str(destination.relative_to(ROOT)),
        "file_written": not dry_run,
    }
    if not dry_run:
        result["sha256"] = _write(
            destination,
            fields,
            kept,
            _output_options(options, metadata, fields),
            spreadsheet_formula_policy,
            column_types,
            overwrite=overwrite,
        )
    return result


@mcp.tool()
@_serialized
def clean_csv(
    path: str,
    operations: list[CleanOperation],
    output_file: str | None = None,
    dry_run: bool = False,
    options: CsvOptions | None = None,
    spreadsheet_formula_policy: Literal["preserve", "escape", "reject"] = "escape",
    column_types: dict[str, str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Trim, change case, and deduplicate selected columns."""
    if not operations:
        raise ValueError("operations must not be empty")
    fields, rows, metadata = _load(_path(path), options)
    _validate_columns(fields, {column for operation in operations for column in operation.columns})
    before = len(rows)
    for operation in operations:
        if operation.operation == "drop_duplicates":
            seen = set()
            unique_rows = []
            for row in rows:
                key = tuple(row[column] for column in operation.columns)
                if key not in seen:
                    seen.add(key)
                    unique_rows.append(row)
            rows = unique_rows
            continue
        method = {
            "trim_whitespace": "strip",
            "lowercase": "lower",
            "uppercase": "upper",
        }[operation.operation]
        for row in rows:
            for column in operation.columns:
                if row[column] is not None:
                    row[column] = getattr(row[column], method)()
    destination = _output_path(path, output_file, "clean")
    result = {
        "input_rows": before,
        "output_rows": len(rows),
        "removed_duplicates": before - len(rows),
        "output_file": str(destination.relative_to(ROOT)),
        "file_written": not dry_run,
    }
    if not dry_run:
        result["sha256"] = _write(
            destination,
            fields,
            rows,
            _output_options(options, metadata, fields),
            spreadsheet_formula_policy,
            column_types,
            overwrite=overwrite,
        )
    return result


@mcp.tool()
@_serialized
def merge_csv(
    files: list[str],
    mode: Literal["concatenate", "join"],
    output_file: str,
    left_key: str | None = None,
    right_key: str | None = None,
    join_type: Literal["inner", "left", "right", "outer"] = "inner",
    options: CsvOptions | None = None,
    spreadsheet_formula_policy: Literal["preserve", "escape", "reject"] = "escape",
    column_types: dict[str, str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Concatenate matching files or join exactly two files by keys."""
    if len(files) < 2:
        raise ValueError("at least two files are required")
    loaded = [_load(_path(file), options) for file in files]
    if mode == "concatenate":
        fields = loaded[0][0]
        if any(item[0] != fields for item in loaded[1:]):
            raise ValueError("concatenated files must have identical columns and order")
        rows = [row for _, file_rows, _ in loaded for row in file_rows]
    else:
        if len(files) != 2 or not left_key or not right_key:
            raise ValueError("join requires exactly two files plus left_key and right_key")
        left_fields, left_rows, _ = loaded[0]
        right_fields, right_rows, _ = loaded[1]
        _validate_columns(left_fields, {left_key})
        _validate_columns(right_fields, {right_key})
        frame = pd.DataFrame(left_rows, columns=left_fields).merge(
            pd.DataFrame(right_rows, columns=right_fields),
            how=join_type,
            left_on=left_key,
            right_on=right_key,
            suffixes=("_left", "_right"),
        )
        fields = list(frame.columns)
        rows = [
            {field: None if pd.isna(value) else str(value) for field, value in row.items()}
            for row in frame.to_dict(orient="records")
        ]
    destination = _path(output_file, must_exist=False)
    checksum = _write(
        destination,
        fields,
        rows,
        options,
        spreadsheet_formula_policy,
        column_types,
        overwrite,
    )
    return {
        "output_file": output_file,
        "row_count": len(rows),
        "columns": fields,
        "sha256": checksum,
    }


@mcp.resource("csv://files")
def files_resource() -> str:
    """List available CSV files as JSON."""
    return json.dumps({"files": list_csv_files()})


@mcp.resource("csv://file/{name}/metadata")
def metadata_resource(name: str) -> str:
    """Expose CSV metadata as JSON."""
    inspection = inspect_csv(name, sample_rows=10)
    inspection.pop("sample_rows")
    return json.dumps(inspection)


@mcp.resource("csv://file/{name}/schema")
def schema_resource(name: str) -> str:
    """Expose CSV columns and conservative possible types as JSON."""
    inspection = inspect_csv(name, sample_rows=100)
    return json.dumps(
        {"file": name, "columns": inspection["columns"], "possible_types": inspection["possible_types"]}
    )


@mcp.resource("csv://file/{name}/preview")
def preview_resource(name: str) -> str:
    """Expose a ten-row CSV preview as JSON."""
    return json.dumps(preview_csv(name, limit=10))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
