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
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
