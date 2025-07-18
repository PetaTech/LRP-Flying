import os
import json
import sys
import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional, Set
from fastapi import FastAPI, Request, HTTPException, Query
from pydantic import BaseModel
import httpx
import redis.asyncio as redis
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Or specify: ["http://localhost:3000"]
    allow_credentials=True,
    allow_methods=["*"],  # GET, POST, OPTIONS, etc.
    allow_headers=["*"],
)

# ─── REDIS CONNECTION ──────────────────────────────────────────────
redis_client = None

async def get_redis():
    global redis_client
    if redis_client is None:
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            raise Exception("REDIS_URL environment variable not set")
        redis_client = redis.from_url(redis_url, decode_responses=True)
    return redis_client

def dbg(*args):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{timestamp}]", *args, file=sys.stderr, flush=True)

# ─── CONFIG ────────────────────────────────────────────────────────
TP_ALT_URL = os.getenv("TP_ALT_URL")

# ─── PYDANTIC MODELS ───────────────────────────────────────────────
# (No models needed for GET-only price updates)

# ─── PRICE MONITOR ────────────────────────────────────────────────
class PriceMonitor:
    def __init__(self):
        self.monitored_symbols: Set[str] = set()
        self.position_manager = None
        self.price_history = {}
        
    async def add_symbol(self, symbol: str):
        self.monitored_symbols.add(symbol)
        dbg(f"📡 Added symbol to monitoring: {symbol}")
    
    async def remove_symbol(self, symbol: str):
        self.monitored_symbols.discard(symbol)
        if symbol in self.price_history:
            del self.price_history[symbol]
        dbg(f"📡 Removed symbol from monitoring: {symbol}")
    
    async def process_price_update(self, symbol: str, price: float, source: str = None):
        try:
            # Store price in history (keep last 10 prices)
            if symbol not in self.price_history:
                self.price_history[symbol] = []
            
            price_data = {
                "price": price,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": source or "external"
            }
            
            self.price_history[symbol].append(price_data)
            
            # Keep only last 10 prices
            if len(self.price_history[symbol]) > 10:
                self.price_history[symbol] = self.price_history[symbol][-10:]
            
            # Rich logging for price updates
            status_emoji = "🟢" if symbol in self.monitored_symbols else "🔵"
            source_info = f" [{source}]" if source else ""
            dbg(f"{status_emoji} PRICE UPDATE: {symbol} = ${price:,.2f}{source_info} | Monitored: {symbol in self.monitored_symbols}")
            
            # Check for auto-trail updates if symbol is monitored
            if symbol in self.monitored_symbols and self.position_manager:
                trail_result = await self.position_manager.calculate_auto_trail(symbol, price)
                
                if trail_result:
                    dbg(f"🔄 AUTO-TRAIL TRIGGERED: {symbol} -> {trail_result['action'].upper()}")
                    dbg(f"   └─ New Stop: ${trail_result['new_stop']:.2f} | Locked Profit: ${trail_result['locked_profit']:.2f}")
                    
                    return {
                        "price_processed": True,
                        "auto_trail_triggered": True,
                        "trail_result": trail_result
                    }
            
            return {
                "price_processed": True,
                "auto_trail_triggered": False
            }
            
        except Exception as e:
            dbg(f"❌ Error processing price update: {e}")
            raise

# ─── POSITION MANAGEMENT ────────────────────────────────────────────
class PositionManager:
    def __init__(self):
        self.redis = None
    
    async def init_redis(self):
        if self.redis is None:
            self.redis = await get_redis()
    
    async def get_position(self, ticker: str) -> Optional[Dict]:
        await self.init_redis()
        try:
            position_data = await self.redis.get(f"position:{ticker}")
            if position_data:
                return json.loads(position_data)
            return None
        except Exception as e:
            dbg(f"❌ Redis get error: {e}")
            return None
    
    async def save_position(self, ticker: str, position_data: Dict):
        await self.init_redis()
        try:
            await self.redis.set(f"position:{ticker}", json.dumps(position_data), ex=86400)
            dbg(f"💾 Position saved for {ticker}: {position_data}")
            await price_monitor.add_symbol(ticker)
        except Exception as e:
            dbg(f"❌ Redis save error: {e}")
    
    async def delete_position(self, ticker: str):
        await self.init_redis()
        try:
            await self.redis.delete(f"position:{ticker}")
            dbg(f"🗑️ Position deleted for {ticker}")
            await price_monitor.remove_symbol(ticker)
        except Exception as e:
            dbg(f"❌ Redis delete error: {e}")
    
    async def calculate_auto_trail(self, ticker: str, current_price: float) -> Optional[Dict]:
        position = await self.get_position(ticker)
        if not position:
            return None

        entry_price = position["entryPrice"]
        side = position["side"]
        arm_after_profit = position["armAfterProfit"]
        trail_step = position["trailStep"]

        # Calculate profit in dollars
        point_value = position["pointValue"]
        quantity = position["quantity"]

        if side == "buy":
            profit_dollars = (current_price - entry_price) * point_value * quantity
        else:
            profit_dollars = (entry_price - current_price) * point_value * quantity

        dbg(f"📈 Auto-trail check: {ticker} | Side: {side} | Entry: ${entry_price} | Current: ${current_price} | Profit: ${profit_dollars:.2f}")

        if profit_dollars >= arm_after_profit:
            # How far past the arm threshold?
            trailing_dollars = profit_dollars - arm_after_profit
            increments = int(trailing_dollars / trail_step)

            locked_profit = arm_after_profit + (increments * trail_step)

            # Compute new stop price
            if side == "buy":
                new_stop = entry_price + (locked_profit / (point_value * quantity))
            else:
                new_stop = entry_price - (locked_profit / (point_value * quantity))

            current_stop = position.get("currentStop")
            current_locked_profit = position.get("lockedProfit", 0)

            # Only send if this is a better stop
            if current_stop is None or locked_profit > current_locked_profit:
                position["currentStop"] = new_stop
                position["lockedProfit"] = locked_profit
                await self.save_position(ticker, position)

                stop_update_payload = {
                    "strategy_id": "Tiger-Alt",
                    "ticker": ticker,
                    "action": "sell" if side == "buy" else "buy",
                    "quantity": quantity,
                    "orderType": "stop",
                    "stopPrice": new_stop,
                    "timeInForce": "gtc"
                }

                await send_traderspost(stop_update_payload)

                return {
                    "action": "trail_updated",
                    "ticker": ticker,
                    "new_stop": new_stop,
                    "locked_profit": locked_profit,
                    "current_price": current_price
                }

        return None

# Initialize components
price_monitor = PriceMonitor()
position_manager = PositionManager()
price_monitor.position_manager = position_manager

# ─── SEND TO TRADERSPOST ───────────────────────────────────────────
async def send_traderspost(data: Dict):
    dbg(f"📤 Sending to TradersPost: {TP_ALT_URL}")
    dbg(f"📤 Payload: {json.dumps(data, indent=2)}")
    
    if not TP_ALT_URL:
        dbg("❌ TradersPost URL not configured")
        return {"error": "TradersPost URL not configured"}
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(TP_ALT_URL, json=data)
            response.raise_for_status()
            response_data = response.json() if response.content else {}
            
            dbg(f"✅ TradersPost response ({response.status_code}): {response_data}")
            return {"success": True, "response": response_data, "status_code": response.status_code}
            
    except Exception as e:
        dbg(f"❌ TradersPost error: {str(e)}")
        return {"error": str(e)}

# ─── BASIC ROUTES ──────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"message": "Tiger-Alt Bot API v2.0 - POST Price Updates"}

@app.get("/health")
async def health_check():
    try:
        r = await get_redis()
        await r.ping()
        return {
            "status": "healthy", 
            "redis": "connected",
            "monitored_symbols": list(price_monitor.monitored_symbols),
            "version": "2.0"
        }
    except Exception as e:
        return {"status": "unhealthy", "redis": f"error: {str(e)}"}

@app.get("/ping")
async def ping():
    dbg("🏓 PONG!")
    return {"pong": True, "timestamp": datetime.now(timezone.utc).isoformat()}

# ─── PRICE UPDATE ENDPOINT ─────────────────────────────────────────
@app.get("/price-update")
async def price_update(
    symbol: str = Query(..., description="Trading symbol (e.g., MNQU2025, NQ, BTCUSD)"),
    price: float = Query(..., description="Current price of the symbol"),
    source: str = Query(None, description="Source of the price data")
):
    """
    GET endpoint for receiving real-time price updates from external sources.
    
    Example: /price-update?symbol=MNQU2025&price=21450.75&source=polygon.io
    """
    try:
        if not symbol or symbol.strip() == "":
            raise HTTPException(status_code=400, detail="Symbol cannot be empty")
        
        if price <= 0:
            raise HTTPException(status_code=400, detail="Price must be greater than 0")
        
        symbol = symbol.strip().upper()
        result = await price_monitor.process_price_update(symbol, price, source)
        
        return {
            "status": "success",
            "symbol": symbol,
            "price": price,
            "source": source,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "monitored": symbol in price_monitor.monitored_symbols,
            "processing_result": result
        }
        
    except HTTPException:
        raise
    except Exception as e:
        dbg(f"❌ Price update error: {e}")
        raise HTTPException(status_code=500, detail=f"Error processing price update: {str(e)}")

# ─── PRICE HISTORY ENDPOINT ────────────────────────────────────────
@app.get("/price-history/{symbol}")
async def get_price_history(symbol: str):
    symbol = symbol.strip().upper()
    
    if symbol not in price_monitor.price_history:
        return {
            "symbol": symbol,
            "history": [],
            "message": "No price history available"
        }
    
    return {
        "symbol": symbol,
        "history": price_monitor.price_history[symbol],
        "count": len(price_monitor.price_history[symbol])
    }

# ─── MAIN WEBHOOK ENDPOINT ─────────────────────────────────────────
@app.post("/pine-entry")
async def pine_entry(req: Request):
    raw_body = await req.body()
    dbg(f"🔄 Raw webhook body: {raw_body}")
    
    try:
        # Parse JSON from body
        if raw_body.startswith(b'"') and raw_body.endswith(b'"'):
            text = raw_body.decode("utf-8", errors="ignore").strip('"')
            data = json.loads(text)
        else:
            data = json.loads(raw_body)
        
        dbg(f"🔍 Parsed webhook data: {json.dumps(data, indent=2)}")
        
    except json.JSONDecodeError as e:
        dbg(f"❌ JSON decode error: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    
    # Extract basic fields
    strategy_id = data.get("strategy_id")
    action = data.get("action", "").lower()
    ticker = data.get("ticker")
    
    # Validate required fields
    if not strategy_id or not action or not ticker:
        dbg(f"❌ Missing required fields: strategy_id={strategy_id}, action={action}, ticker={ticker}")
        raise HTTPException(status_code=400, detail="Missing required fields: strategy_id, action, ticker")
    
    dbg(f"🎯 Processing webhook: {strategy_id} | {action} | {ticker}")
    
    # Handle Tiger-Alt strategy only
    if strategy_id == "Tiger-Alt":
        return await handle_tiger_alt(data)
    else:
        dbg(f"❌ Unsupported strategy: {strategy_id}")
        raise HTTPException(status_code=400, detail=f"Unsupported strategy_id: {strategy_id}. Only Tiger-Alt is supported.")

async def handle_tiger_alt(data: Dict) -> Dict:
    action = data.get("action", "").lower()
    ticker = data.get("ticker")
    
    dbg(f"🐅 Tiger-Alt handler: {action} | {ticker}")
    
    if action in ["buy", "sell"]:
        return await handle_tiger_alt_entry(data)
    elif action == "exit":
        return await handle_tiger_alt_exit(data)
    else:
        dbg(f"❌ Unknown action for Tiger-Alt: {action}")
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

async def handle_tiger_alt_entry(data: Dict) -> Dict:
    ticker = data.get("ticker")
    action = data.get("action")
    quantity = data.get("quantity")
    price = data.get("price")
    
    dbg(f"🚀 Tiger-Alt ENTRY: {ticker} | {action} | qty:{quantity} | price:{price}")
    
    # Extract auto-trail parameters
    auto_trail = data.get("autoTrail", {})
    arm_after_profit = auto_trail.get("armAfterProfit")
    trail_step = auto_trail.get("trailStep")
    hard_stop = auto_trail.get("hardStop")  # Used for reference, not order submission

    # Extract point value
    extras = data.get("extras", {})
    point_value = extras.get("pointValue")
    
    # Validate required fields
    if not all([quantity, price, arm_after_profit, trail_step, hard_stop]):
        missing = [k for k, v in {
            "quantity": quantity, "price": price, "arm_after_profit": arm_after_profit,
            "trail_step": trail_step, "hard_stop": hard_stop
        }.items() if v is None]
        dbg(f"❌ Missing required fields: {missing}")
        raise HTTPException(status_code=400, detail=f"Missing required fields: {missing}")
    
    # Save position to Redis for auto-trail tracking
    position_data = {
        "ticker": ticker,
        "side": action,
        "entryPrice": price,
        "quantity": quantity,
        "armAfterProfit": arm_after_profit,
        "trailStep": trail_step,
        "hardStop": hard_stop,  # For internal reference, not used in an order
        "pointValue": point_value or 5.0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "currentStop": None,
        "lockedProfit": 0
    }
    
    await position_manager.save_position(ticker, position_data)

    # Build TradersPost market entry payload (NO autoTrail, NO initial stop)
    tp_payload = {
        "strategy_id": "Tiger-Alt",
        "ticker": ticker,
        "action": action,
        "quantity": quantity,
        "orderType": "market",
        "timeInForce": "gtc",
        "sentiment": data.get("sentiment", "bullish" if action == "buy" else "bearish"),
        "price": price,
        "signalPrice": data.get("signalPrice", price),
        "extras": {
            "strategy": "Tiger-Alt-AutoTrail-POST",
            "version": "2.0"
        }
    }
    
    data.pop("autoTrail", None)

    result = await send_traderspost(tp_payload)

    return {
        "status": "tiger_alt_entry_processed",
        "ticker": ticker,
        "action": action,
        "autoTrail": "enabled",
        "position_saved": True,
        "traderspost_result": result
    }

async def handle_tiger_alt_exit(data: Dict) -> Dict:
    ticker = data.get("ticker")
    price = data.get("price")
    
    dbg(f"🚪 Tiger-Alt EXIT: {ticker} | price:{price}")
    
    # Get position info before deleting
    position = await position_manager.get_position(ticker)
    
    # Clean up position tracking
    await position_manager.delete_position(ticker)
    
    # Build exit payload
    tp_payload = {
        "strategy_id": "Tiger-Alt",
        "ticker": ticker,
        "action": "exit",
        "orderType": "market",
        "timeInForce": "gtc",
        "price": price,
        "signalPrice": data.get("signalPrice", price),
        "extras": {
            "exitReason": "pine_signal",
            "strategy": "Tiger-Alt-AutoTrail-POST",
            "version": "2.0"
        }
    }
    
    # Send to TradersPost
    result = await send_traderspost(tp_payload)
    
    return {
        "status": "tiger_alt_exit_processed",
        "ticker": ticker,
        "position_cleaned": position is not None,
        "traderspost_result": result
    }

# ─── MONITORING ENDPOINTS ──────────────────────────────────────────
@app.get("/monitor/status")
async def monitor_status():
    return {
        "version": "2.0",
        "monitored_symbols": list(price_monitor.monitored_symbols),
        "price_history_count": {symbol: len(history) for symbol, history in price_monitor.price_history.items()}
    }

@app.get("/monitor/symbols")
async def get_monitored_symbols():
    return {
        "symbols": list(price_monitor.monitored_symbols),
        "count": len(price_monitor.monitored_symbols)
    }

# ─── STARTUP/SHUTDOWN EVENTS ───────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    try:
        await get_redis()
        dbg("✅ Redis connection established")
        
        # Load existing positions and start monitoring them
        r = await get_redis()
        keys = await r.keys("position:*")
        
        for key in keys:
            ticker = key.replace("position:", "")
            await price_monitor.add_symbol(ticker)
        
        dbg("🚀 Tiger-Alt Bot API v2.0 started successfully")
        dbg(f"📊 Monitoring symbols: {list(price_monitor.monitored_symbols)}")
        
    except Exception as e:
        dbg(f"❌ Startup error: {e}")
        raise

@app.on_event("shutdown")
async def shutdown_event():
    try:
        if redis_client:
            await redis_client.close()
        dbg("✅ Clean shutdown completed")
    except Exception as e:
        dbg(f"❌ Shutdown error: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))