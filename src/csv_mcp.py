import csv
import os
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
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        if not fields or any(not field for field in fields) or len(fields) != len(set(fields)):
            raise ValueError("CSV must have non-empty, unique column names")
        return fields, list(reader)


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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
