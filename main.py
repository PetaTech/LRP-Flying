import os
import json
import sys
import asyncio
from fastapi import FastAPI, Request, HTTPException, WebSocket
import httpx

app = FastAPI()

def dbg(*args):
    # Print debug messages directly to stderr for Heroku logging
    print(*args, file=sys.stderr, flush=True)

# ─── BASIC ROUTES ──────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"message": "Root is working"}

@app.get("/health")
async def health_check():
    return {"status": "alive"}

# ─── PING FOR DEBUG ─────────────────────────────────────────────────
@app.get("/ping")
async def ping():
    dbg("🏓 PONG!")
    return {"pong": True}

# ─── CONFIG ────────────────────────────────────────────────────────
TP_CORE_URL   = os.getenv("TP_CORE_URL")
TP_RUNNER_URL = os.getenv("TP_RUNNER_URL")
TP_ALT_URL    = os.getenv("TP_ALT_URL")

# ─── WEBSOCKET HUB ─────────────────────────────────────────────────
connected_websockets = set()

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_websockets.add(ws)
    dbg("🔌 WebSocket connected")
    try:
        while True:
            await ws.receive_text()
    except:
        pass
    finally:
        connected_websockets.discard(ws)
        dbg("❌ WebSocket disconnected")

async def broadcast_ws(payload):
    for ws in list(connected_websockets):
        try:
            await ws.send_text(json.dumps(payload))
        except:
            connected_websockets.discard(ws)

# ─── SEND TO ALT (TradersPost) ──────────────────────────────────────
async def send_alt_ws(data):
    dbg(f"▶▶ Sending to ALT URL: {TP_ALT_URL}")
    dbg(f"▶▶ ALT payload: {data}")
    if not TP_ALT_URL:
        dbg("❌ ALT URL not configured.")
        return
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(TP_ALT_URL, json=data)
            resp.raise_for_status()
            dbg(f"✅ ALT HTTP response: {resp.text}")
    except Exception as e:
        dbg(f"❌ Error sending ALT HTTP message: {e}")

# ─── PINE ENTRY ENDPOINT ──────────────────────────────────────────
@app.post("/pine-entry")
async def pine_entry(req: Request):
    raw = await req.body()
    dbg(f"🛠️ Raw body: {raw}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        text = raw.decode("utf-8", errors="ignore").strip('"')
        data = json.loads(text)
    dbg(f"🔍 PINE PAYLOAD: {data}")

    sid    = data.get("strategy_id")
    action = data.get("action", "").lower()
    ticker = data.get("ticker")
    dbg(f"🛈 Received strategy_id={sid}, action={action}, ticker={ticker}")
    if not sid or action not in ("buy", "sell", "exit") or not ticker:
        raise HTTPException(status_code=400, detail="Missing or invalid strategy_id/action/ticker")

    # broadcast raw payload to websockets
    asyncio.create_task(broadcast_ws(data))

    # ── Tiger-Alt entry (auto-trail) ─────────────────────────────
    if sid == "Tiger-Alt" and action in ("buy", "sell"):
        dbg("🛎️ Entering auto-trail branch for Tiger-Alt entry")
        try:
            qty          = data.get("quantity")
            initial_amt  = data.get("stopLoss", {}).get("amount") or data.get("stopLoss", {}).get("stopPrice")
            trail_amt    = data.get("extras", {}).get("autoTrail", {}).get("stopLoss")
            trigger_dist = data.get("extras", {}).get("autoTrail", {}).get("trigger")
            
            dbg(f"🛎️ Parsed bracket qty={qty}, initial_amt={initial_amt}, trail_amt={trail_amt}, trigger_dist={trigger_dist}")
        except Exception as e:
            dbg(f"❌ Error extracting bracket data: {e}")
            raise HTTPException(status_code=400, detail="Malformed bracket JSON")

        tp_payload = {
            "strategy_id":         sid,
            "action":              data.get("action"),
            "sentiment":           data.get("sentiment"),
            "ticker":              ticker,
            "orderStrategyTypeId": data.get("orderStrategyTypeId"),
            "quantityType":        "fixed_quantity",
            "quantity":            qty,
            "orderType":           data.get("orderType") or data.get("params", {}).get("entryVersion", {}).get("orderType"),
            "timeInForce":         data.get("timeInForce") or data.get("params", {}).get("entryVersion", {}).get("timeInForce"),
            **({"price": data["price"]} if data.get("orderType") != "market" and data.get("price") is not None else {}),
            "stopLoss":            {"type": "stop", "amount": initial_amt},
            "trailingStop":        True,
            "trailPriceType":      "Absolute",
            "trailAmount":         trail_amt,
            "triggerDistance":     trigger_dist
        }
        dbg(f"→ TradersPost payload: {tp_payload}")
        await send_alt_ws(tp_payload)
        return {
            "status": "alt_http_sent",
            "parsed": {"qty": qty, "initial_amt": initial_amt, "trail_amt": trail_amt, "trigger_dist": trigger_dist},
            "tp_payload": tp_payload
        }

    # ── Tiger-Alt exit (flat) ─────────────────────────────────────
    if sid == "Tiger-Alt" and action == "exit":
        dbg("🛎️ Handling exit for Tiger-Alt")
        await send_alt_ws(data)
        return {"status": "alt_http_sent", "payload": data}

    # ── Core/Runner fallback ──────────────────────────────────────
    url = TP_CORE_URL if sid == "Tiger-Core" else TP_RUNNER_URL if sid == "Tiger-Runner" else None
    if not url:
        raise HTTPException(status_code=400, detail=f"Unknown strategy_id: {sid}")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=data)
            resp.raise_for_status()
            dbg(f"✅ Forwarded to {sid}: {resp.status_code}")
            return {"status": "forwarded", "to": sid}
    except Exception as e:
        dbg(f"❌ Error forwarding to CORE/RUNNER: {e}")
        raise HTTPException(status_code=500, detail="Forwarding error")
