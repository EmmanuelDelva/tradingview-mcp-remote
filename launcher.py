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

Extra tool: ``derivatives_snapshot``
------------------------------------
The upstream ``tradingview-mcp-server`` (backed by tradingview-screener) exposes
price + indicators + volume, but NOT perpetual open interest / funding rate.
On the claude.ai chat surface the *only* invokable market-data connectors are this
remote TradingView MCP and LunarCrush — the Crypto.com connector's tools are NOT
callable in chat there (they are artifact-only). So to make OI/funding appear on
claude.ai too, we register one additional read-only tool here that pulls those
metrics from public perpetual endpoints (Binance → Bybit → OKX → Crypto.com,
first reachable one wins — resilient to a datacenter IP being geo-blocked on any
single venue). Cowork/Claude Code keep using the local Crypto.com MCP; the skill
uses whichever OI source is available on the current surface.
"""
import json
import urllib.request
import urllib.error

import mcp.server.transport_security as _ts

# Accept any Host / Origin header. Patching the class methods covers instances
# created later by FastMCP.streamable_http_app(), regardless of how it builds
# its TransportSecuritySettings.
_ts.TransportSecurityMiddleware._validate_host = lambda self, host: True
_ts.TransportSecurityMiddleware._validate_origin = lambda self, origin: True

# Import the package module so all its @mcp.tool() handlers register on tvs.mcp,
# then add our own tool to that SAME FastMCP instance before main() runs it.
import tradingview_mcp.server as tvs


# ── helpers ─────────────────────────────────────────────────────────────────────
_UA = "Mozilla/5.0 (compatible; tradingview-mcp-remote/1.0; +derivatives_snapshot)"


def _get(url: str, timeout: float = 5.0):
    """GET url -> parsed JSON, or None on any error (HTTP, timeout, geo-block, parse)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _pack(source, instrument, oi, funding, mark, index, last, ts, oi_usd=None):
    """Normalise one venue's fields into the common snapshot shape."""
    basis = round(mark - index, 8) if (mark is not None and index is not None) else None
    basis_pct = round((mark - index) / index * 100, 4) if (basis is not None and index) else None
    if oi_usd is None and oi is not None and mark is not None:
        oi_usd = round(oi * mark, 2)
    return {
        "source": source,
        "instrument": instrument,
        "open_interest": oi,                 # in base coin (e.g. WLD) unless noted
        "open_interest_usd": oi_usd,         # notional ≈ OI * mark
        "funding_rate": funding,             # decimal, e.g. 0.0001 = 0.01%
        "funding_rate_pct": round(funding * 100, 5) if funding is not None else None,
        "funding_direction": (None if funding is None else
                              ("longs pagan (alcista/crowded long)" if funding > 0 else
                               "shorts pagan (bajista/crowded short)" if funding < 0 else "neutro")),
        "mark_price": mark,
        "index_price": index,
        "basis": basis,                      # mark - index
        "basis_pct": basis_pct,              # basis as % of index (funding pressure sign)
        "last": last,                        # last traded (for price cross-check vs TradingView)
        "timestamp": ts,
    }


def _binance(sym):
    oi = _get(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={sym}")
    pi = _get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}")
    if not oi or not pi or "openInterest" not in oi or "markPrice" not in pi:
        return None
    pr = _get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={sym}")
    last = _num(pr.get("price")) if pr else None
    return _pack("binance-fapi", sym, _num(oi.get("openInterest")), _num(pi.get("lastFundingRate")),
                 _num(pi.get("markPrice")), _num(pi.get("indexPrice")), last, pi.get("time"))


def _bybit(sym):
    d = _get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}")
    lst = (d or {}).get("result", {}).get("list") or []
    if not lst:
        return None
    t = lst[0]
    return _pack("bybit-v5", sym, _num(t.get("openInterest")), _num(t.get("fundingRate")),
                 _num(t.get("markPrice")), _num(t.get("indexPrice")), _num(t.get("lastPrice")),
                 (d or {}).get("time"), oi_usd=_num(t.get("openInterestValue")))


def _okx(sym):
    base = sym[:-4] if sym.endswith("USDT") else sym
    inst = f"{base}-USDT-SWAP"
    oi = _get(f"https://www.okx.com/api/v5/public/open-interest?instId={inst}")
    fr = _get(f"https://www.okx.com/api/v5/public/funding-rate?instId={inst}")
    mk = _get(f"https://www.okx.com/api/v5/public/mark-price?instType=SWAP&instId={inst}")
    ix = _get(f"https://www.okx.com/api/v5/market/index-tickers?instId={base}-USDT")
    oi_d = (oi or {}).get("data") or []
    if not oi_d:
        return None
    fr_d = (fr or {}).get("data") or [{}]
    mk_d = (mk or {}).get("data") or [{}]
    ix_d = (ix or {}).get("data") or [{}]
    return _pack("okx-v5", inst, _num(oi_d[0].get("oiCcy")), _num(fr_d[0].get("fundingRate")),
                 _num(mk_d[0].get("markPx")), _num(ix_d[0].get("idxPx")), _num(ix_d[0].get("idxPx")),
                 oi_d[0].get("ts"), oi_usd=_num(oi_d[0].get("oiUsd")))


def _cryptocom(sym):
    base = sym[:-4] if sym.endswith("USDT") else sym
    inst = f"{base}USD-PERP"
    d = _get(f"https://api.crypto.com/exchange/v1/public/get-tickers?instrument_name={inst}")
    rows = (d or {}).get("result", {}).get("data") or []
    if not rows:
        return None
    t = rows[0]
    return _pack("crypto.com", inst, _num(t.get("oi")), None, None, None, _num(t.get("a")), t.get("t"))


@tvs.mcp.tool()
def derivatives_snapshot(symbol: str = "WLDUSDT") -> dict:
    """Real-time perpetual DERIVATIVES metrics — open interest, funding rate,
    mark & index price (basis) — for a USDT-margined perp. Complements
    ``coin_analysis`` (which returns price/indicators/volume but NOT OI/funding).

    Data comes from public perp endpoints, trying Binance → Bybit → OKX →
    Crypto.com and returning the first reachable venue (resilient to any single
    exchange geo-blocking the server's IP). The chosen venue is reported in
    ``source``; ``basis`` = mark − index (positive ⇒ funding pressure long).

    Args:
        symbol: Perp symbol without separator, e.g. "WLDUSDT", "BTCUSDT", "ETHUSDT".
    """
    sym = (symbol or "WLDUSDT").upper().replace("-", "").replace("_", "").strip()
    if not sym.endswith("USDT"):
        sym = sym + "USDT" if not sym.endswith("USD") else sym
    tried = []
    for name, fn in (("binance-fapi", _binance), ("bybit-v5", _bybit),
                     ("okx-v5", _okx), ("crypto.com", _cryptocom)):
        tried.append(name)
        try:
            snap = fn(sym)
        except Exception:
            snap = None
        if snap and snap.get("open_interest") is not None:
            snap["sources_tried"] = tried
            snap["note"] = "OI en coin base; funding decimal (×100 = %); basis = mark−index."
            return snap
    return {
        "error": "No public perp venue reachable for this symbol from the server.",
        "symbol": sym,
        "sources_tried": tried,
        "hint": "Verifica el símbolo (p.ej. WLDUSDT) o reintenta; alguna venue puede geo-bloquear la IP.",
    }


from tradingview_mcp.server import main  # noqa: E402  (import after tool registration)

if __name__ == "__main__":
    main()
