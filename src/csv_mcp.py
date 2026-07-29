import csv
import os
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


ROOT = Path(os.environ.get("CSV_MCP_ROOT", ".")).resolve()
mcp = FastMCP(
    "csv",
    instructions=f"Read and edit CSV files under {ROOT}. Paths are relative to this directory.",
)


def _path(name: str, *, must_exist: bool = True) -> Path:
    path = (ROOT / name).resolve()
    if path == ROOT or ROOT not in path.parents:
        raise ValueError("path must point to a file inside CSV_MCP_ROOT")
    if path.suffix.lower() != ".csv":
        raise ValueError("path must end in .csv")
    if must_exist and not path.is_file():
        raise FileNotFoundError(f"CSV file not found: {name}")
    return path


def _reader(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    # ponytail: in-memory rows; switch to streaming or a database when files outgrow RAM.
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        if not fields or any(not field for field in fields) or len(fields) != len(set(fields)):
            raise ValueError("CSV must have non-empty, unique column names")
        return fields, list(reader)


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
    """List CSV files below CSV_MCP_ROOT."""
    return sorted(str(path.relative_to(ROOT)) for path in ROOT.rglob("*") if path.is_file() and path.suffix.lower() == ".csv")


@mcp.tool()
def inspect_csv(path: str) -> dict[str, Any]:
    """Return column names and row count for a CSV file."""
    fields, rows = _reader(_path(path))
    return {"path": path, "columns": fields, "row_count": len(rows)}


@mcp.tool()
def read_csv(path: str, offset: int = 0, limit: int = 100) -> dict[str, Any]:
    """Read a page of rows from a CSV file (maximum 1000 rows)."""
    if offset < 0 or not 1 <= limit <= 1000:
        raise ValueError("offset must be non-negative and limit must be between 1 and 1000")
    fields, rows = _reader(_path(path))
    return {
        "path": path,
        "columns": fields,
        "rows": rows[offset : offset + limit],
        "offset": offset,
        "returned": len(rows[offset : offset + limit]),
        "total": len(rows),
    }


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
