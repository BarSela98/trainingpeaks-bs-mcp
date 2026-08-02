#!/usr/bin/env python3
"""Drive the tp-mcp server with an SDK 1.x or 2.x client.

Run under a venv with ``mcp==1.26.0`` installed while the server env has SDK
2.x - proves the v2 server still serves legacy clients end-to-end through the
v1 ``ClientSession`` (initialize handshake, tools/list, tools/call), not just
at the raw JSON-RPC level (tests/test_v2_migration.py covers that). It also
supports the SDK 2.x field names so release builds can reuse it as a packaged
server protocol smoke test.

Usage:  <legacy-venv>/bin/python scripts/legacy_client_check.py <server-command> [args...]
e.g.:   /tmp/legacy/bin/python scripts/legacy_client_check.py tp-mcp serve
Exit 0 on success.
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _model_field(value: object, snake_case: str, camel_case: str) -> object:
    """Read a field across the SDK 2.x and legacy SDK 1.x model names."""
    if hasattr(value, snake_case):
        return getattr(value, snake_case)
    return getattr(value, camel_case)


async def main() -> int:
    cmd, args = sys.argv[1], sys.argv[2:]
    env = dict(os.environ, TP_MCP_SKIP_STARTUP_VALIDATION="1")
    async with (
        stdio_client(StdioServerParameters(command=cmd, args=args, env=env)) as (read, write),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        server_info = _model_field(init, "server_info", "serverInfo")
        protocol_version = _model_field(init, "protocol_version", "protocolVersion")
        print(
            f"initialize ok: server={server_info.name} {server_info.version} "
            f"protocol={protocol_version}"
        )

        tools = await session.list_tools()
        assert len(tools.tools) >= 80, f"expected >=80 tools, got {len(tools.tools)}"
        t0 = tools.tools[0]
        assert t0.title and t0.annotations is not None, "tool metadata missing over legacy wire"
        print(f"tools/list ok: {len(tools.tools)} tools, first={t0.name!r} title={t0.title!r}")

        # offline call: must return the structured INVALID_ARGS payload
        result = await session.call_tool("tp_get_workouts", {})
        payload = json.loads(result.content[0].text)
        assert payload.get("error_code") == "INVALID_ARGS", f"unexpected payload: {payload}"
        print("tools/call ok: structured INVALID_ARGS returned to a legacy client")
    print("LEGACY CLIENT CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
