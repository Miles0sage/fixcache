"""
lore_memory.mcp — MCP server for lore-memory.

Exposes 6 tools over stdio JSON-RPC (no external MCP SDK required):
  lore_remember        — attested storage with trust scoring
  lore_recall          — verified retrieval with trust threshold
  lore_fix             — store error recipes (procedural memory)
  lore_match_procedure — pattern-matched procedure retrieval
  lore_teach           — store conventions / rules as facts
  lore_stats           — memory system statistics
"""
