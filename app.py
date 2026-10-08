import os, time, threading
from datetime import datetime, timezone
from typing import Dict, Any
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Nexus Trading AI", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")

MODE = os.getenv("TRADING_MODE", "paper")
LIVE = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
lock = threading.Lock()
state: Dict[str, Any] = {
    "kill_switch": False,
    "balance": 10000.0,
    "positions": [],
    "last_signal": None,
    "events": [],
}

SYMBOLS = {
    "BTCUSD": {"id": "bitcoin", "fallback": 65000.0},
    "ETHUSD": {"id": "ethereum", "fallback": 2500.0},
    "XAUUSD": {"id": None, "fallback": 2650.0},
    "EURUSD": {"id": None, "fallback": 1.17},
}

def now():
    return datetime.now(timezone.utc).isoformat()

def log(message):
    state["events"].insert(0, {"time": now(), "message": message})
    state["events"] = state["events"][:30]

def price_for(symbol: str):
    meta = SYMBOLS[symbol]
    if not meta["id"]:
        return meta["fallback"], 0.0, "fallback adapter"
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": meta["id"], "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=6,
        )
        r.raise_for_status()
        d = r.json().get(meta["id"], {})
        price = float(d.get("usd", meta["fallback"]))
        change = float(d.get("usd_24h_change", 0.0))
        return price, change, "CoinGecko"
    except Exception:
        return meta["fallback"], 0.0, "fallback"

def analyze(symbol: str):
    price, change, source = price_for(symbol)
    trend = 50 + max(-20, min(20, change * 1.5))
    momentum = 50 + max(-20, min(20, change * 1.2))
    volatility = 70 if abs(change) < 4 else 48
    score = round(0.45 * trend + 0.35 * momentum + 0.20 * volatility, 1)
    direction = "BUY" if change > 0.35 else "SELL" if change < -0.35 else "HOLD"
    if score < 72:
        direction = "HOLD"
    confidence = min(99, max(20, round(score)))
    reasons = [
        f"24h momentum: {change:+.2f}%",
        f"Trend component: {trend:.1f}/100",
        f"Momentum component: {momentum:.1f}/100",
        f"Volatility quality: {volatility:.1f}/100",
    ]
    if source != "CoinGecko":
        reasons.append("Using safe fallback because a live adapter is unavailable.")
    signal = {
        "symbol": symbol, "price": round(price, 6), "direction": direction,
        "score": score, "confidence": confidence, "source": source,
        "reasons": reasons, "time": now(),
    }
    state["last_signal"] = signal
    return signal

@app.get("/", response_class=HTMLResponse)
def home():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/health")
def health():
    return {"ok": True, "mode": MODE, "live_trading_enabled": LIVE, "time": now()}

@app.get("/api/state")
def get_state():
    with lock:
        return {
            "kill_switch": state["kill_switch"],
            "balance": state["balance"],
            "positions": list(state["positions"]),
            "events": list(state["events"]),
            "mode": MODE,
            "live_trading_enabled": LIVE,
            "last_signal": state["last_signal"],
        }

@app.get("/api/analyze/{symbol}")
def analyze_api(symbol: str):
    symbol = symbol.upper()
    if symbol not in SYMBOLS:
        raise HTTPException(404, "Unsupported symbol")
    with lock:
        signal = analyze(symbol)
        log(f"{symbol}: {signal['direction']} | score {signal['score']}")
        return signal

@app.post("/api/execute/{symbol}")
def execute(symbol: str):
    symbol = symbol.upper()
    if symbol not in SYMBOLS:
        raise HTTPException(404, "Unsupported symbol")
    with lock:
        if state["kill_switch"]:
            raise HTTPException(409, "Kill switch is active")
        signal = analyze(symbol)
        if signal["direction"] == "HOLD" or signal["score"] < 72:
            raise HTTPException(409, "Entry gate says HOLD")
        if state["positions"]:
            raise HTTPException(409, "Maximum demo position count reached")
        risk_cash = round(state["balance"] * 0.005, 2)
        pos = {
            "id": "paper-" + str(int(time.time())),
            "symbol": symbol,
            "side": signal["direction"],
            "entry": signal["price"],
            "risk": risk_cash,
            "time": now(),
        }
        state["positions"].append(pos)
        log(f"PAPER {signal['direction']} opened on {symbol} @ {signal['price']}")
        return pos

@app.post("/api/close")
def close():
    with lock:
        if not state["positions"]:
            raise HTTPException(409, "No open position")
        pos = state["positions"].pop(0)
        price, _, _ = price_for(pos["symbol"])
        move = ((price - pos["entry"]) / pos["entry"]) if pos["side"] == "BUY" else ((pos["entry"] - price) / pos["entry"])
        pnl = round(pos["risk"] * move * 20, 2)
        state["balance"] = round(state["balance"] + pnl, 2)
        log(f"PAPER position closed. P&L {pnl:+.2f}")
        return {"closed": pos, "pnl": pnl, "balance": state["balance"]}

@app.post("/api/kill")
def kill():
    with lock:
        state["kill_switch"] = True
        log("EMERGENCY KILL SWITCH ACTIVATED")
        return {"kill_switch": True}

@app.post("/api/reset-kill")
def reset_kill():
    with lock:
        state["kill_switch"] = False
        log("Kill switch reset")
        return {"kill_switch": False}
