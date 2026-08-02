import asyncio
import sys

from langchain_mcp_adapters.tools import load_mcp_tools
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_langchain_adapter_loads_agent_tools(tmp_path):
    async def load():
        csv_server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "csv_mcp"],
            env={"CSV_MCP_ROOT": str(tmp_path)},
        )
        filesystem_server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "filesystem_mcp"],
            env={"FILESYSTEM_MCP_ROOT": str(tmp_path)},
        )
        async with stdio_client(csv_server) as (csv_read, csv_write), stdio_client(
            filesystem_server
        ) as (filesystem_read, filesystem_write):
            async with ClientSession(csv_read, csv_write) as csv_session, ClientSession(
                filesystem_read, filesystem_write
            ) as filesystem_session:
                await asyncio.gather(csv_session.initialize(), filesystem_session.initialize())
                csv_tools, filesystem_tools = await asyncio.gather(
                    load_mcp_tools(csv_session), load_mcp_tools(filesystem_session)
                )
                return {tool.name for tool in [*csv_tools, *filesystem_tools]}

    assert {
        "list_csv_files",
        "inspect_csv",
        "query_csv",
        "create_csv",
        "list_directory",
        "read_text_file",
        "write_text_file",
        "replace_text",
    } <= asyncio.run(load())
