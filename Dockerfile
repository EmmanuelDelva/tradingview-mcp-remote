FROM python:3.13-slim

# TradingView Multi-Market Screener MCP server (atilaahmettaner/tradingview-mcp)
# Package on PyPI: tradingview-mcp-server  ->  console script: tradingview-mcp
RUN pip install --no-cache-dir tradingview-mcp-server

# The host injects the public port via $PORT (Render, Railway, Koyeb...).
# Default 8000 for local runs / hosts that don't set it.
ENV PORT=8000
EXPOSE 8000

# Serve in REMOTE mode (streamable-http). MCP endpoint is exposed at /mcp
# NOTE: this endpoint is POST-only and requires  Accept: text/event-stream
#       (a plain browser GET returns 406/404 by design -> do NOT add an HTTP health check).
CMD ["sh", "-c", "tradingview-mcp streamable-http --host 0.0.0.0 --port ${PORT}"]
