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


# ── microstructure: order book (bid/ask walls + imbalance) + tape (buy/sell delta) ──
# Returns (source, instrument, bids[(px,sz)], asks[(px,sz)], trades[(px,sz,side)]).

def _ms_binance(base):
    sym = f"{base}USDT"
    b = _get(f"https://fapi.binance.com/fapi/v1/depth?symbol={sym}&limit=25")
    t = _get(f"https://fapi.binance.com/fapi/v1/aggTrades?symbol={sym}&limit=100")
    if not b or "bids" not in b:
        return None
    bids = [(_num(p), _num(q)) for p, q in b.get("bids", [])]
    asks = [(_num(p), _num(q)) for p, q in b.get("asks", [])]
    trades = [(_num(x.get("p")), _num(x.get("q")), "sell" if x.get("m") else "buy")
              for x in (t or []) if isinstance(x, dict)]
    return ("binance-fapi", sym, bids, asks, trades)


def _ms_bybit(base):
    sym = f"{base}USDT"
    b = _get(f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={sym}&limit=25")
    t = _get(f"https://api.bybit.com/v5/market/recent-trade?category=linear&symbol={sym}&limit=60")
    res = (b or {}).get("result") or {}
    if not res.get("b"):
        return None
    bids = [(_num(p), _num(s)) for p, s in res.get("b", [])]
    asks = [(_num(p), _num(s)) for p, s in res.get("a", [])]
    trades = [(_num(x.get("price")), _num(x.get("size")), (x.get("side") or "").lower())
              for x in ((t or {}).get("result") or {}).get("list", [])]
    return ("bybit-v5", sym, bids, asks, trades)


def _ms_okx(base):
    inst = f"{base}-USDT-SWAP"
    b = _get(f"https://www.okx.com/api/v5/market/books?instId={inst}&sz=25")
    t = _get(f"https://www.okx.com/api/v5/market/trades?instId={inst}&limit=100")
    bd = (b or {}).get("data") or []
    if not bd:
        return None
    bids = [(_num(x[0]), _num(x[1])) for x in bd[0].get("bids", [])]
    asks = [(_num(x[0]), _num(x[1])) for x in bd[0].get("asks", [])]
    trades = [(_num(x.get("px")), _num(x.get("sz")), (x.get("side") or "").lower())
              for x in ((t or {}).get("data") or [])]
    return ("okx-v5", inst, bids, asks, trades)


def _ms_cryptocom(base):
    inst = f"{base}USD-PERP"
    b = _get(f"https://api.crypto.com/exchange/v1/public/get-book?instrument_name={inst}&depth=25")
    t = _get(f"https://api.crypto.com/exchange/v1/public/get-trades?instrument_name={inst}&count=100")
    rows = ((b or {}).get("result") or {}).get("data") or []
    if not rows:
        return None
    row = rows[0]
    bids = [(_num(x[0]), _num(x[1])) for x in row.get("bids", [])]
    asks = [(_num(x[0]), _num(x[1])) for x in row.get("asks", [])]
    trades = [(_num(x.get("p")), _num(x.get("q")), (x.get("s") or "").lower())
              for x in (((t or {}).get("result") or {}).get("data") or [])]
    return ("crypto.com", inst, bids, asks, trades)


def _ms_compute(source, inst, bids, asks, trades):
    bids = sorted([x for x in bids if x[0] and x[1] is not None], key=lambda x: -x[0])
    asks = sorted([x for x in asks if x[0] and x[1] is not None], key=lambda x: x[0])
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    mid = round((best_bid + best_ask) / 2, 6) if (best_bid and best_ask) else None
    spread = round(best_ask - best_bid, 6) if (best_bid and best_ask) else None
    spread_bps = round(spread / mid * 1e4, 2) if (spread is not None and mid) else None
    N = 20
    bid_vol = round(sum(s for _, s in bids[:N]), 2)
    ask_vol = round(sum(s for _, s in asks[:N]), 2)
    tot = bid_vol + ask_vol
    imb = round((bid_vol - ask_vol) / tot, 3) if tot else None
    top_bids = sorted(bids[:N], key=lambda x: -x[1])[:3]
    top_asks = sorted(asks[:N], key=lambda x: -x[1])[:3]
    buy_vol = round(sum(sz for _, sz, sd in trades if sd.startswith("b")), 2)
    sell_vol = round(sum(sz for _, sz, sd in trades if sd.startswith("s")), 2)
    ttot = buy_vol + sell_vol
    buy_pct = round(buy_vol / ttot * 100, 1) if ttot else None
    return {
        "source": source,
        "instrument": inst,
        "best_bid": best_bid, "best_ask": best_ask, "mid": mid,
        "spread": spread, "spread_bps": spread_bps,
        "bid_depth_vol": bid_vol, "ask_depth_vol": ask_vol, "book_imbalance": imb,
        "imbalance_read": (None if imb is None else
                           "compradores dominan el libro" if imb > 0.15 else
                           "vendedores dominan el libro" if imb < -0.15 else "libro equilibrado"),
        "top_bid_walls": [{"price": p, "size": s} for p, s in top_bids],
        "top_ask_walls": [{"price": p, "size": s} for p, s in top_asks],
        "tape_trades": len(trades),
        "tape_buy_vol": buy_vol, "tape_sell_vol": sell_vol,
        "tape_delta": round(buy_vol - sell_vol, 2), "tape_buy_pct": buy_pct,
        "tape_read": (None if buy_pct is None else
                      "presión compradora (agresión al ask)" if buy_pct >= 58 else
                      "presión vendedora (agresión al bid)" if buy_pct <= 42 else "tape equilibrado"),
        "note": "depth top-20 del perp; walls = mayores tamaños; delta = vol taker compra − venta; side = agresor.",
    }


def microstructure_snapshot(symbol: str = "WLDUSDT") -> dict:
    """Real-time MICROSTRUCTURE of a USDT-margined perp: order-book depth
    (bid/ask walls + imbalance + spread) and the TAPE (recent trades, buy/sell
    delta + aggressor pressure). Complements ``coin_analysis`` (indicators) and
    ``derivatives_snapshot`` (OI/funding). Tries Binance → Bybit → OKX →
    Crypto.com and returns the first reachable venue (``source``); resilient to
    a single exchange geo-blocking the server's IP.

    Args:
        symbol: Perp symbol without separator, e.g. "WLDUSDT", "BTCUSDT".
    """
    sym = (symbol or "WLDUSDT").upper().replace("-", "").replace("_", "").strip()
    base = sym[:-4] if sym.endswith("USDT") else (sym[:-3] if sym.endswith("USD") else sym)
    tried = []
    for name, fn in (("binance-fapi", _ms_binance), ("bybit-v5", _ms_bybit),
                     ("okx-v5", _ms_okx), ("crypto.com", _ms_cryptocom)):
        tried.append(name)
        try:
            r = fn(base)
        except Exception:
            r = None
        if r and (r[2] or r[3]):
            snap = _ms_compute(*r)
            snap["sources_tried"] = tried
            return snap
    return {
        "error": "No public perp venue reachable for order book / tape from the server.",
        "symbol": sym, "sources_tried": tried,
    }


import sys as _sys

# Register on the SAME FastMCP instance the package built, guarded so a
# registration hiccup (e.g. a FastMCP version skew on the host) can never crash
# the server — worst case it serves the upstream tools and logs a warning. The
# stderr lines show up in the Render deploy logs so a live deploy is verifiable.
for _fn in (derivatives_snapshot, microstructure_snapshot):
    try:
        tvs.mcp.tool()(_fn)
        _sys.stderr.write(f"[launcher] {_fn.__name__} registered OK\n")
    except Exception as _e:  # pragma: no cover
        _sys.stderr.write(f"[launcher] WARN could not register {_fn.__name__}: {_e!r}\n")
try:
    _n = len(tvs.mcp._tool_manager._tools)
    _sys.stderr.write(f"[launcher] total tools now: {_n}\n")
except Exception:
    pass
_sys.stderr.flush()

from tradingview_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
