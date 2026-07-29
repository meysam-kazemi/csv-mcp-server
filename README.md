# CSV MCP

A small stdio MCP server for safely reading and editing CSV files inside one configured directory.

## Run

```bash
uv sync
CSV_MCP_ROOT=/absolute/path/to/csv/files uv run csv-mcp
```

Example MCP client configuration:

```json
{
  "mcpServers": {
    "csv": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/csv-mcp",
        "run",
        "csv-mcp"
      ],
      "env": {
        "CSV_MCP_ROOT": "/absolute/path/to/csv/files"
      }
    }
  }
}
```

All tool paths are relative to `CSV_MCP_ROOT`; absolute paths, parent traversal, and non-CSV files are rejected.

## Tools

| Tool | Purpose |
| --- | --- |
| `list_csv_files` | List available CSV files recursively |
| `inspect_csv` | Return columns and row count |
| `read_csv` | Read a bounded page of rows |
| `create_csv` | Create a file with columns and optional rows |
| `append_rows` | Append rows matching the existing columns |
| `update_rows` | Update rows matching exact field values |
| `delete_rows` | Delete rows matching exact field values |

Writes validate every row and replace the destination atomically. `create_csv` does not overwrite existing files unless `overwrite` is explicitly true, and update/delete reject empty match criteria.

## Test

```bash
uv run pytest
```
