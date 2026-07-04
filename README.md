# TradingView MCP — remote (self-hosted) connector

Tu propia copia del servidor **TradingView Multi-Market Screener** (paquete PyPI
`tradingview-mcp-server`, de `atilaahmettaner/tradingview-mcp`) corriendo en modo
**remoto** para poder añadirla como *connector* en **claude.ai** (web + móvil) y que
además aparezca en **Cowork / Claude Desktop** — una sola entrada para las 3 superficies.

- **Transporte:** `streamable-http`
- **Endpoint MCP:** `/mcp`  (POST-only, requiere `Accept: text/event-stream`)
- **Auth:** ninguna por defecto (ver nota de seguridad abajo)
- **Herramientas:** 30+ (coin_analysis, multi_timeframe_analysis, combined_analysis,
  top_gainers/losers, bollinger_scan, volume_breakout_scanner, market_sentiment,
  financial_news, backtest_strategy, egx_*, yahoo_price, market_snapshot, …)

---

## Opción A — Render (recomendada: gratis, sin tarjeta, HTTPS estable, sigue viva con la PC apagada)

1. Sube esta carpeta a un repo de GitHub (público o privado).
2. En https://render.com  ->  **New +**  ->  **Blueprint**  ->  elige el repo  ->  **Apply**.
   (Render lee `render.yaml` y crea un Web Service Docker en plan **Free**.)
3. Espera el primer build (~2-4 min). La URL será algo como
   `https://tradingview-mcp-XXXX.onrender.com`.
4. Tu endpoint MCP es esa URL **+ `/mcp`**:
   `https://tradingview-mcp-XXXX.onrender.com/mcp`
5. Pruébalo (debe responder HTTP 200 con `mcp-session-id`):

   ```bash
   curl -i -X POST https://TU-URL.onrender.com/mcp \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'
   ```

> **Cold start del plan Free:** Render duerme el servicio tras 15 min sin tráfico y la
> primera petición tarda ~50 s en despertar. Para trading en vivo conviene mantenerlo
> caliente — ver *Keep-warm* abajo.

## Conectarlo en claude.ai

1. claude.ai  ->  **Settings / Ajustes**  ->  **Connectors / Conectores**  ->
   **Add custom connector**.
2. Nombre: `TradingView`  ·  URL: `https://TU-URL.onrender.com/mcp`
3. Guardar  ->  claude.ai hará el handshake y listará las herramientas.
   Al añadirlo aquí, también aparece en Cowork/Desktop.

## Keep-warm (evitar el cold start, gratis)

Crea un cron en https://cron-job.org (gratis) que haga **POST** al `/mcp` cada 10 min
con el mismo body `initialize` de arriba (headers incluidos). Mantiene el servicio
despierto 24/7 sin costo.

---

## Opción B — Hugging Face Spaces (alternativa gratis sin tarjeta)

Igual de válida si ya tienes cuenta HF. Crea un **Space** tipo *Docker*, sube este
`Dockerfile` **cambiando el puerto a 7860** (HF expone 7860):

```dockerfile
CMD ["sh", "-c", "tradingview-mcp streamable-http --host 0.0.0.0 --port 7860"]
```

La URL pública será `https://TU-USUARIO-tradingview-mcp.hf.space/mcp`.

---

## Seguridad

El endpoint queda **abierto** (sin token). Es solo lectura de datos de mercado —sin
claves, sin órdenes, sin escritura— así que el riesgo es bajo, pero cualquiera con la
URL puede consumir tu cuota del host. Si quieres cerrarlo, pon el servicio detrás de
Cloudflare Access o un reverse-proxy con cabecera secreta.

## Local (referencia)

Modo stdio (lo que ya usa tu Claude Desktop hoy):

```bash
uvx --python 3.13 --from tradingview-mcp-server tradingview-mcp
```

Modo remoto local (para probar):

```bash
uvx --python 3.13 --from tradingview-mcp-server tradingview-mcp streamable-http --host 0.0.0.0 --port 8000
```
