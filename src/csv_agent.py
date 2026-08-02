import asyncio
import os
import sys
from pathlib import Path

from langchain.agents import create_agent
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dotenv import load_dotenv
load_dotenv()


SYSTEM_PROMPT = (
    "You are a workspace assistant. Use the CSV tools for structured CSV work and the "
    "filesystem tools for other text files; never guess file contents. Tool paths are "
    "relative to their configured roots. Only write files when the user explicitly asks."
)


async def chat(prompt: str | None = None) -> None:
    one_shot = prompt is not None
    root = Path(os.environ.get("CSV_MCP_ROOT", ".")).resolve()
    filesystem_root = Path(os.environ.get("FILESYSTEM_MCP_ROOT", str(root))).resolve()
    csv_server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "csv_mcp"],
        env={key: value for key, value in os.environ.items() if key.startswith("CSV_MCP_")}
        | {"CSV_MCP_ROOT": str(root)},
    )
    filesystem_server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "filesystem_mcp"],
        env={
            key: value
            for key, value in os.environ.items()
            if key.startswith("FILESYSTEM_MCP_")
        }
        | {"FILESYSTEM_MCP_ROOT": str(filesystem_root)},
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
            agent = create_agent(
                os.environ.get("CSV_AGENT_MODEL", "openai:gpt-4o-mini"),
                [*csv_tools, *filesystem_tools],
                system_prompt=SYSTEM_PROMPT,
            )
            messages = []
            while True:
                if prompt is None:
                    try:
                        prompt = input("You: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        return
                if prompt in {"/exit", "/quit"}:
                    return
                if not prompt:
                    prompt = None
                    continue
                result = await agent.ainvoke(
                    {"messages": [*messages, {"role": "user", "content": prompt}]}
                )
                messages = result["messages"]
                print("*" * 40)
                print(f"[YOU]: {prompt}")
                for message in messages:
                    message.pretty_print()
                print("*" * 40)
                if one_shot:
                    return
                prompt = None


def main() -> None:
    asyncio.run(chat(" ".join(sys.argv[1:]) or None))


if __name__ == "__main__":
    main()
