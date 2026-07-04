FROM python:3.13-slim

# TradingView Multi-Market Screener MCP server (atilaahmettaner/tradingview-mcp)
# Package on PyPI: tradingview-mcp-server  ->  console script: tradingview-mcp
RUN pip install --no-cache-dir tradingview-mcp-server

WORKDIR /app
# launcher.py neutralises the SDK's DNS-rebinding Host/Origin check so the server
# works behind a public domain (otherwise every request gets HTTP 421). See launcher.py.
COPY launcher.py /app/launcher.py

# The host injects the public port via $PORT (Render, Railway, Koyeb...).
# Default 8000 for local runs / hosts that don't set it.
ENV PORT=8000
ENV HOST=0.0.0.0
EXPOSE 8000

# Serve in REMOTE mode (streamable-http). MCP endpoint is exposed at /mcp
# NOTE: /mcp is POST-only and requires  Accept: text/event-stream
#       (a plain browser GET returns 406/404 by design -> do NOT add an HTTP health check).
CMD ["sh", "-c", "python -u /app/launcher.py streamable-http --host 0.0.0.0 --port ${PORT}"]
