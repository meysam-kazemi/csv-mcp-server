import filesystem_mcp


def test_filesystem_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(filesystem_mcp, "ROOT", tmp_path.resolve())

    written = filesystem_mcp.write_text_file("docs/note.txt", "hello world")
    assert written["size_bytes"] == 11
    assert filesystem_mcp.read_text_file("docs/note.txt", offset=6, limit=5)["content"] == "world"
    assert filesystem_mcp.list_directory(".", recursive=True)["entries"] == [
        {"path": "docs", "type": "directory", "size_bytes": None},
        {"path": "docs/note.txt", "type": "file", "size_bytes": 11},
    ]
    replaced = filesystem_mcp.replace_text("docs/note.txt", "world", "MCP")
    assert replaced["replacements"] == 1
    assert (tmp_path / "docs" / "note.txt").read_text(encoding="utf-8") == "hello MCP"
    try:
        filesystem_mcp.write_text_file("docs/note.txt", "lost")
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing file was overwritten")
    assert (tmp_path / "docs" / "note.txt").read_text(encoding="utf-8") == "hello MCP"

    for unsafe in ("../outside.txt", "https://example.com/file.txt", r"C:\Users\file.txt"):
        try:
            filesystem_mcp.read_text_file(unsafe)
        except (ValueError, FileNotFoundError):
            pass
        else:
            raise AssertionError(f"unsafe path was accepted: {unsafe}")

    outside = tmp_path.parent / "outside-filesystem-mcp"
    outside.mkdir(exist_ok=True)
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    try:
        filesystem_mcp.write_text_file("escape/file.txt", "secret")
    except ValueError:
        pass
    else:
        raise AssertionError("symlink escape was accepted")
