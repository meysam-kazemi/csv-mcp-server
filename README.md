# CSV MCP

A stdio MCP server for safely reading, analyzing, validating, and transforming CSV/TSV files. Values remain strings unless an operation explicitly needs a numeric or date type.

## Run

```bash
uv sync
CSV_MCP_ROOT=/absolute/path/to/csv-workspace uv run csv-mcp
```

MCP client configuration:

```json
{
  "mcpServers": {
    "csv": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/csv-mcp", "run", "csv-mcp"],
      "env": {"CSV_MCP_ROOT": "/absolute/path/to/csv-workspace"}
    }
  }
}
```

A useful workspace layout is:

```text
csv-workspace/
├── input/
├── output/
├── temporary/
└── backups/
```

All paths are resolved below `CSV_MCP_ROOT`. Absolute paths, traversal, symlink escapes, URLs, and extensions other than `.csv`/`.tsv` are rejected.

## Tools

| Tool | Purpose |
| --- | --- |
| `list_csv_files` | List available CSV/TSV files recursively |
| `inspect_csv` | Detect encoding/delimiter and report structure, samples, inferred types, and warnings |
| `preview_csv` | Read a bounded page and optional columns |
| `read_csv` | Backward-compatible alias for `preview_csv` |
| `query_csv` | Select, filter, and sort with controlled operators |
| `summarize_csv` | Group and aggregate with whitelisted functions |
| `validate_csv` | Check schemas, types, required values, uniqueness, categories, and malformed rows |
| `compare_csv` | Compare two files by unique key columns |
| `create_csv` | Create validated atomic output |
| `append_rows` | Append rows to a new output by default |
| `update_rows` | Preview or write filtered changes |
| `delete_rows` | Preview or write filtered deletions |
| `clean_csv` | Trim, change case, and deduplicate |
| `merge_csv` | Concatenate matching files or join two files |

Query operators are `=`, `!=`, `>`, `>=`, `<`, `<=`, `contains`, `starts_with`, `ends_with`, `is_null`, `not_null`, and `in`. Aggregations are `count`, `sum`, `mean`, `minimum`, `maximum`, `median`, and `unique_count`.

## Parsing options

Tools accept an optional `options` object:

```json
{
  "encoding": "windows-1252",
  "delimiter": ";",
  "decimal_separator": ",",
  "quotechar": "\"",
  "escapechar": "\\",
  "doublequote": true,
  "header_mode": "first_row",
  "null_values": ["", "NULL", "N/A"],
  "keep_empty_strings": false,
  "column_types": {"amount": "decimal"},
  "date_formats": {"created_at": "%Y-%m-%d"}
}
```

Supported encodings are UTF-8, UTF-8 with BOM, UTF-16, Latin-1, and Windows-1252. Supported delimiters are comma, semicolon, tab, and pipe. With `header_mode: "none"`, pass `column_names` or generated names such as `column_1` are used.

## Safe writes

Append, update, delete, and clean produce a file below `output/` unless `output_file` is supplied. Update and delete default to `dry_run: true`. Existing destinations require `overwrite: true`.

Writes:

1. Validate columns, rows, paths, and formula policy.
2. Serialize mutations inside the server process.
3. Write a temporary file beside the destination.
4. Parse and validate the temporary file.
5. Calculate SHA-256.
6. Atomically replace the destination.

`spreadsheet_formula_policy` accepts `escape` (default), `reject`, or `preserve`. Negative values are preserved only for columns explicitly typed as `integer` or `decimal`.

## Resources

```text
csv://files
csv://file/{name}/metadata
csv://file/{name}/schema
csv://file/{name}/preview
```

## Limits

Limits are configurable with environment variables:

| Variable | Default |
| --- | ---: |
| `CSV_MCP_MAX_FILE_SIZE` | 50 MiB |
| `CSV_MCP_MAX_ROWS` | 1,000,000 |
| `CSV_MCP_MAX_COLUMNS` | 500 |
| `CSV_MCP_MAX_FIELD_LENGTH` | 1 MiB |
| `CSV_MCP_MAX_RETURNED_ROWS` | 1,000 |
| `CSV_MCP_MAX_FILES` | 10,000 |

The bounded in-memory engine is intentional for this version. Use chunked pandas, PyArrow, DuckDB, or Parquet when files must exceed these limits.

## Test

```bash
uv run pytest
```
