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
    "You are a CSV assistant. Use the CSV tools for every claim about workspace files; "
    "never guess their contents. Paths are relative to CSV_MCP_ROOT. "
    "Only write files when the user explicitly asks."
)


async def chat(prompt: str | None = None) -> None:
    one_shot = prompt is not None
    root = Path(os.environ.get("CSV_MCP_ROOT", ".")).resolve()
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "csv_mcp"],
        env={key: value for key, value in os.environ.items() if key.startswith("CSV_MCP_")}
        | {"CSV_MCP_ROOT": str(root)},
    )

    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        agent = create_agent(
            os.environ.get("CSV_AGENT_MODEL", "openai:gpt-4o-mini"),
            await load_mcp_tools(session),
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
            print("*"*40)
            print(f"[YOU]: {prompt}")
            for message in messages:
                message.pretty_print()
            print("*"*40)
            if one_shot:
                return
            prompt = None


def main() -> None:
    asyncio.run(chat(" ".join(sys.argv[1:]) or None))


if __name__ == "__main__":
    main()
