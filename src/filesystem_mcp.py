import os
import tempfile
import threading
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from typing import Any

from mcp.server.fastmcp import FastMCP


ROOT = Path(os.environ.get("FILESYSTEM_MCP_ROOT", ".")).resolve()
MAX_FILE_SIZE = int(os.environ.get("FILESYSTEM_MCP_MAX_FILE_SIZE", 10 * 1024 * 1024))
MAX_RETURNED_CHARS = int(os.environ.get("FILESYSTEM_MCP_MAX_RETURNED_CHARS", 100_000))
MAX_ENTRIES = int(os.environ.get("FILESYSTEM_MCP_MAX_ENTRIES", 10_000))
WRITE_LOCK = threading.RLock()
mcp = FastMCP(
    "filesystem",
    instructions=f"Read and edit UTF-8 text files under {ROOT}. Paths are relative to this directory.",
)


def _path(name: str, *, must_exist: bool = True) -> Path:
    if "://" in name:
        raise ValueError("URLs are not allowed")
    if Path(name).is_absolute() or PureWindowsPath(name).is_absolute():
        raise ValueError("absolute paths are not allowed")
    path = (ROOT / name).resolve()
    if path != ROOT and ROOT not in path.parents:
        raise ValueError("path must stay inside FILESYSTEM_MCP_ROOT")
    if must_exist and not path.exists():
        raise FileNotFoundError(f"path not found: {name}")
    return path


def _read(path: Path) -> str:
    if not path.is_file():
        raise ValueError("path must point to a file")
    if path.stat().st_size > MAX_FILE_SIZE:
        raise ValueError(f"file exceeds the {MAX_FILE_SIZE}-byte limit")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("file must be UTF-8 text") from error


def _write(path: Path, content: str, overwrite: bool) -> dict[str, Any]:
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_FILE_SIZE:
        raise ValueError(f"content exceeds the {MAX_FILE_SIZE}-byte limit")
    temporary_name = ""
    # ponytail: process-local lock; use OS locks if multiple servers write one workspace.
    with WRITE_LOCK:
        if path.exists() and not overwrite:
            raise FileExistsError(f"file already exists: {path.relative_to(ROOT)}")
        if path.exists() and not path.is_file():
            raise ValueError("path must point to a file")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
                temporary_name = handle.name
                handle.write(encoded)
            os.replace(temporary_name, path)
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
    return {
        "path": str(path.relative_to(ROOT)),
        "size_bytes": len(encoded),
        "sha256": sha256(encoded).hexdigest(),
    }


@mcp.tool()
def list_directory(path: str = ".", recursive: bool = False) -> dict[str, Any]:
    """List files and directories without following links outside the workspace."""
    directory = _path(path)
    if not directory.is_dir():
        raise ValueError("path must point to a directory")
    entries = []
    for entry in directory.rglob("*") if recursive else directory.iterdir():
        relative = str(entry.relative_to(ROOT))
        try:
            _path(relative)
        except (FileNotFoundError, ValueError):
            continue
        entries.append(
            {
                "path": relative,
                "type": "symlink"
                if entry.is_symlink()
                else "directory"
                if entry.is_dir()
                else "file",
                "size_bytes": entry.stat().st_size if entry.is_file() else None,
            }
        )
        if len(entries) > MAX_ENTRIES:
            break
    return {
        "path": path,
        "entries": sorted(entries[:MAX_ENTRIES], key=lambda entry: entry["path"]),
        "truncated": len(entries) > MAX_ENTRIES,
    }


@mcp.tool()
def read_text_file(path: str, offset: int = 0, limit: int = 10_000) -> dict[str, Any]:
    """Read a bounded page of a UTF-8 text file."""
    if offset < 0 or not 1 <= limit <= MAX_RETURNED_CHARS:
        raise ValueError(
            f"offset must be non-negative and limit must be between 1 and {MAX_RETURNED_CHARS}"
        )
    content = _read(_path(path))
    page = content[offset : offset + limit]
    return {
        "path": path,
        "content": page,
        "offset": offset,
        "returned_chars": len(page),
        "total_chars": len(content),
        "has_more": offset + len(page) < len(content),
    }


@mcp.tool()
def write_text_file(path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    """Atomically write a UTF-8 text file, creating parent directories as needed."""
    return _write(_path(path, must_exist=False), content, overwrite)


@mcp.tool()
def replace_text(
    path: str,
    old_text: str,
    new_text: str,
    expected_replacements: int = 1,
) -> dict[str, Any]:
    """Atomically replace text only when it occurs the expected number of times."""
    if not old_text or expected_replacements < 1:
        raise ValueError("old_text must not be empty and expected_replacements must be positive")
    source = _path(path)
    with WRITE_LOCK:
        content = _read(source)
        replacements = content.count(old_text)
        if replacements != expected_replacements:
            raise ValueError(
                f"expected {expected_replacements} replacement(s), found {replacements}"
            )
        result = _write(source, content.replace(old_text, new_text), overwrite=True)
    return {**result, "replacements": replacements}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
