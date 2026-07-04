"""Remote (streamable-http) launcher for hosting behind a public domain (Render, etc.).

Why this exists
---------------
The MCP Python SDK enables DNS-rebinding protection for the streamable-http
transport: it validates the incoming ``Host`` (and ``Origin``) header and returns
HTTP 421 "Invalid Host header" for anything that isn't in its allow-list. That
protection is designed for servers bound to localhost and reached by a browser.
When the exact same server is deployed behind a public host such as
``*.onrender.com``, every legitimate MCP client request arrives with that public
Host header and gets rejected (421), so no tool ever runs.

This server exposes only read-only market data (no auth, no writes, no secrets;
see README "Seguridad"), so neutralising the Host/Origin checks is safe here.
Content-Type validation is left untouched. We patch the SDK middleware and then
hand off to the package's normal entrypoint, which reads the transport/--host/--port
arguments (and HOST/PORT env vars) exactly as usual.
"""
import mcp.server.transport_security as _ts

# Accept any Host / Origin header. Patching the class methods covers instances
# created later by FastMCP.streamable_http_app(), regardless of how it builds
# its TransportSecuritySettings.
_ts.TransportSecurityMiddleware._validate_host = lambda self, host: True
_ts.TransportSecurityMiddleware._validate_origin = lambda self, origin: True

from tradingview_mcp.server import main

if __name__ == "__main__":
    main()
