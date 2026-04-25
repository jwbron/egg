"""Orchestrator-side MCP tool module (issue #1962).

Hosts auxiliary MCP-tool definitions that aren't part of the core
``orchestrator/mcp_tools.py`` ``PIPELINE_TOOLS`` list. Tools defined
here can be plugged into the FastMCP server in ``mcp_server.py`` if
the orchestrator owner chooses to expose them; until then they are
importable from this package as plain Python helpers.
"""
