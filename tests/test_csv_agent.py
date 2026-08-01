import asyncio
import sys

from langchain_mcp_adapters.tools import load_mcp_tools
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_langchain_adapter_loads_csv_tools(tmp_path):
    async def load():
        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "csv_mcp"],
            env={"CSV_MCP_ROOT": str(tmp_path)},
        )
        async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return {tool.name for tool in await load_mcp_tools(session)}

    assert {"list_csv_files", "inspect_csv", "query_csv", "create_csv"} <= asyncio.run(load())
