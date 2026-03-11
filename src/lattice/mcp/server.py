"""Lattice MCP server — entry point and FastMCP instance."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

mcp = FastMCP("lattice")

# Register tools and resources by importing the modules (decorators run at import time)
import lattice.mcp.resources as _resources  # noqa: F401, E402
import lattice.mcp.tools as _tools  # noqa: F401, E402


def main() -> None:
    """Run the Lattice MCP server over stdio transport."""
    import sys

    # Windows UTF-8 guard: re-exec with -X utf8 if not already in UTF-8 mode
    if sys.platform == "win32" and not sys.flags.utf8_mode:
        import os

        os.environ["PYTHONUTF8"] = "1"
        os.execv(sys.executable, [sys.executable, "-X", "utf8"] + sys.argv)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
