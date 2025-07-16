import os
import json
import sys
import asyncio
from fastapi import FastAPI, Request, HTTPException, WebSocket
import httpx

app = FastAPI()

def dbg(*args):
    print(*args, file=sys.stderr, flush=True)

@app.get("/")
async def root():
    return {"message": "Root is working"}

@app.get("/health")
async def health_check():
    return {"status": "alive"}

@app.get("/ping")
async def ping():
    dbg("\ud83c\udfd3 PONG!")
    return {"pong": True}

TP_CORE_URL   = os.getenv("TP_CORE_URL")
TP_RUNNER_URL = os.getenv("TP_RUNNER_URL")
TP_ALT_URL    = os.getenv("TP_ALT_URL")

connected_websockets = set()

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_websockets.add(ws)
    dbg("\ud83d\udd0c WebSocket connected")
    try:
        while True:
            await ws.receive_text()
    except:
        pass
    finally:
        connected_websockets.discard(ws)
        dbg("\u274c WebSocket disconnected")

async def broadcast_ws(payload):
    for ws in list(connected_websockets):
        try:
            await ws.send_text(json.dumps(payload))
        except:
            connected_websockets.discard(ws)

async def send_alt_ws(data):
    dbg(f"\u25b6\u25b6 Sending to ALT URL: {TP_ALT_URL}")
    dbg(f"\u25b6\u25b6 ALT payload: {json.dumps(data, indent=2)}")
    if not TP_ALT_URL:
        dbg("\u274c ALT URL not configured.")
        return
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(TP_ALT_URL, json=data)
            resp.raise_for_status()
            dbg(f"\u2705 ALT HTTP response: {resp.text}")
    except Exception as e:
        dbg(f"\u274c Error sending ALT HTTP message: {e}")

@app.post("/pine-entry")
async def pine_entry(req: Request):
    raw = await req.body()
    dbg(f"\ud83d\udeb0 Raw body: {raw}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        text = raw.decode("utf-8", errors="ignore").strip('"')
        data = json.loads(text)
    dbg(f"\ud83d\udd0d PINE PAYLOAD: {json.dumps(data, indent=2)}")

    sid    = data.get("strategy_id")
    action = data.get("action", "").lower()
    ticker = data.get("ticker")
    dbg(f"\ud83d\udcfc Received strategy_id={sid}, action={action}, ticker={ticker}")

    if not sid or action not in ("buy", "sell", "exit") or not ticker:
        raise HTTPException(status_code=400, detail="Missing or invalid strategy_id/action/ticker")

    asyncio.create_task(broadcast_ws(data))

    if sid == "Tiger-Alt" and action in ("buy", "sell"):
        dbg("\ud83d\udd4e\ufe0f Entering auto-trail branch for Tiger-Alt entry")
        try:
            qty = data.get("quantity")
            if not qty:
                raise ValueError("Missing quantity")

            price = (data.get("price") or 
                     data.get("params", {}).get("entryVersion", {}).get("price") or
                     data.get("close"))

            if not price:
                dbg("\u274c Missing entry price - cannot proceed.")
                return {"error": "Missing price in payload. You must include price for signal-based orders."}

            order_type = (data.get("orderType") or 
                          data.get("params", {}).get("entryVersion", {}).get("orderType") or 
                          "market")

            tp_payload = {
                "ticker": ticker,
                "action": action,
                "orderType": order_type,
                "quantityType": "fixed_quantity",
                "quantity": qty,
                "timeInForce": (data.get("timeInForce") or 
                                data.get("params", {}).get("entryVersion", {}).get("timeInForce") or 
                                "gtc"),
                "signalPrice": float(price)
            }

            if data.get("strategy_id"):
                tp_payload["strategy_id"] = data["strategy_id"]
            if data.get("sentiment"):
                tp_payload["sentiment"] = data["sentiment"]
            if data.get("orderStrategyTypeId"):
                tp_payload["orderStrategyTypeId"] = data["orderStrategyTypeId"]

            if order_type == "limit":
                tp_payload["limitPrice"] = float(price)

            stop_loss_config = data.get("stopLoss", {})
            extras_config = data.get("extras", {}).get("autoTrail", {})

            trail_amt = extras_config.get("stopLoss")
            trigger_dist = extras_config.get("trigger")

            if trail_amt and trigger_dist:
                dbg(f"\ud83d\udd4e\ufe0f Configuring trailing stop: trail_amt={trail_amt}, trigger_dist={trigger_dist}")
                tp_payload.update({
                    "stopLoss": {
                        "type": "trailing_stop",
                        "trailAmount": float(trail_amt)
                    },
                    "triggerDistance": float(trigger_dist)
                })
            elif stop_loss_config:
                stop_price = stop_loss_config.get("stopPrice")
                stop_amount = stop_loss_config.get("amount")
                stop_percent = stop_loss_config.get("percent")
                stop_type = stop_loss_config.get("type", "stop")

                if stop_price:
                    tp_payload["stopLoss"] = {
                        "type": stop_type,
                        "stopPrice": float(stop_price)
                    }
                elif stop_amount:
                    tp_payload["stopLoss"] = {
                        "type": stop_type,
                        "amount": float(stop_amount)
                    }
                elif stop_percent:
                    tp_payload["stopLoss"] = {
                        "type": stop_type,
                        "percent": float(stop_percent)
                    }

            dbg(f"\u2192 Final TradersPost payload: {json.dumps(tp_payload, indent=2)}")
            await send_alt_ws(tp_payload)

            return {
                "status": "alt_http_sent",
                "order_type": order_type,
                "has_stop_loss": "stopLoss" in tp_payload,
                "stop_loss_type": tp_payload.get("stopLoss", {}).get("type"),
                "has_signal_price": "signalPrice" in tp_payload,
                "tp_payload": tp_payload
            }

        except Exception as e:
            dbg(f"\u274c Error building Tiger-Alt payload: {e}")
            raise HTTPException(status_code=400, detail=f"Malformed bracket data: {str(e)}")

    if sid == "Tiger-Alt" and action == "exit":
        dbg("\ud83d\udd4e\ufe0f Handling exit for Tiger-Alt")
        exit_payload = {
            "ticker": ticker,
            "action": "exit"
        }
        if data.get("strategy_id"):
            exit_payload["strategy_id"] = data["strategy_id"]
        if data.get("sentiment"):
            exit_payload["sentiment"] = data["sentiment"]
        if data.get("quantity"):
            exit_payload["quantity"] = data["quantity"]
            exit_payload["quantityType"] = "fixed_quantity"

        price = (data.get("price") or 
                 data.get("close") or 
                 data.get("params", {}).get("entryVersion", {}).get("price"))
        if price:
            exit_payload["signalPrice"] = float(price)
            dbg(f"\ud83d\udd4e\ufe0f Added signalPrice {price} to exit signal")

        dbg(f"\u2192 Exit payload: {json.dumps(exit_payload, indent=2)}")
        await send_alt_ws(exit_payload)
        return {"status": "alt_http_sent", "payload": exit_payload}

    url = TP_CORE_URL if sid == "Tiger-Core" else TP_RUNNER_URL if sid == "Tiger-Runner" else None
    if not url:
        raise HTTPException(status_code=400, detail=f"Unknown strategy_id: {sid}")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=data)
            resp.raise_for_status()
            dbg(f"\u2705 Forwarded to {sid}: {resp.status_code}")
            return {"status": "forwarded", "to": sid}
    except Exception as e:
        dbg(f"\u274c Error forwarding to CORE/RUNNER: {e}")
        raise HTTPException(status_code=500, detail="Forwarding error")
