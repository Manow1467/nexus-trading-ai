import os, time, threading
from datetime import datetime, timezone
from typing import Dict, Any
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Nexus Trading AI", version="1.1.0")
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
    "last_prices": {},
}

def now():
    return datetime.now(timezone.utc).isoformat()

def log(message):
    state["events"].insert(0, {"time": now(), "message": message})
    state["events"] = state["events"][:40]

def get_json(url, params=None):
    r = requests.get(url, params=params, timeout=8, headers={"User-Agent": "NexusTradingAI/1.1"})
    r.raise_for_status()
    return r.json()

def crypto_quote(symbol: str):
    binance_symbol = symbol.replace("USD", "USDT")
    try:
        d = get_json("https://api.binance.com/api/v3/ticker/24hr", {"symbol": binance_symbol})
        return float(d["lastPrice"]), float(d["priceChangePercent"]), "Binance spot", "fresh"
    except Exception:
        coin = {"BTCUSD": "bitcoin", "ETHUSD": "ethereum"}[symbol]
        try:
            d = get_json(
                "https://api.coingecko.com/api/v3/simple/price",
                {"ids": coin, "vs_currencies": "usd", "include_24hr_change": "true"},
            )[coin]
            return float(d["usd"]), float(d.get("usd_24h_change") or 0.0), "CoinGecko", "fresh"
        except Exception as exc:
            raise RuntimeError(f"Crypto market data unavailable: {exc}")

def gold_quote():
    d = get_json("https://xaus.com/api/v1/spot", {"currency": "USD", "unit": "oz", "compact": "1"})
    data_state = d.get("data_state", {})
    status = data_state.get("status", "unknown")
    price = float(d["spot_usd_oz"])
    change = 0.0
    try:
        intraday = get_json("https://xaus.com/api/v1/intraday", {"symbol": "xau", "hours": "24"})
        points = intraday.get("points") or []
        if len(points) >= 2:
            first = float(points[0]["p"])
            last = float(points[-1]["p"])
            if first:
                change = (last - first) / first * 100.0
    except Exception:
        pass
    return price, change, "XAUS XAU/USD spot", status

def eurusd_quote():
    d = get_json("https://xaus.com/api/v1/spot", {"currency": "EUR", "unit": "oz", "compact": "1"})
    eur_gold = float(d["xau"]["price"])
    usd_gold = float(d["spot_usd_oz"])
    if not eur_gold:
        raise RuntimeError("EUR reference feed returned zero")
    eurusd = usd_gold / eur_gold
    status = "stale" if d.get("fx_stale") else d.get("data_state", {}).get("status", "fresh")
    return eurusd, 0.0, "XAUS FX reference", status

def quote(symbol: str):
    if symbol in ("BTCUSD", "ETHUSD"):
        return crypto_quote(symbol)
    if symbol == "XAUUSD":
        return gold_quote()
    return eurusd_quote()

def analyze(symbol: str):
    price, change, source, data_status = quote(symbol)
    trend = max(0.0, min(100.0, 50.0 + change * 1.5))
    momentum = max(0.0, min(100.0, 50.0 + change * 1.2))
    quality = 82.0 if data_status == "fresh" else 62.0
    score = round(0.45 * trend + 0.35 * momentum + 0.20 * quality, 1)
    direction = "BUY" if change > 0.35 else "SELL" if change < -0.35 else "HOLD"
    if data_status != "fresh" or score < 72:
        direction = "HOLD"
    confidence = min(99, max(20, round(score)))
    reasons = [
        f"24h / recent momentum: {change:+.2f}%",
        f"Trend component: {trend:.1f}/100",
        f"Momentum component: {momentum:.1f}/100",
        f"Data quality: {quality:.1f}/100",
    ]
    if data_status != "fresh":
        reasons.append(f"Feed status: {data_status}. Entry blocked until a fresh quote is available.")
    else:
        reasons.append("Fresh public market reference feed is available.")
    signal = {
        "symbol": symbol,
        "price": round(price, 6),
        "direction": direction,
        "score": score,
        "confidence": confidence,
        "source": source,
        "data_status": data_status,
        "reasons": reasons,
        "time": now(),
    }
    state["last_signal"] = signal
    state["last_prices"][symbol] = price
    return signal

@app.get("/", response_class=HTMLResponse)
def home():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "mode": MODE,
        "live_trading_enabled": LIVE,
        "time": now(),
        "symbols": ["BTCUSD", "ETHUSD", "XAUUSD", "EURUSD"],
    }

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
    if symbol not in {"BTCUSD", "ETHUSD", "XAUUSD", "EURUSD"}:
        raise HTTPException(404, "Unsupported symbol")
    with lock:
        try:
            signal = analyze(symbol)
            log(f"{symbol}: {signal['direction']} | score {signal['score']} | {signal['source']}")
            return signal
        except Exception as exc:
            log(f"{symbol}: data feed error handled safely")
            return {
                "symbol": symbol,
                "price": None,
                "direction": "HOLD",
                "score": 0,
                "confidence": 0,
                "source": "unavailable",
                "data_status": "unavailable",
                "reasons": [f"No trusted market quote is available right now. Entry is disabled. ({exc})"],
                "time": now(),
            }

@app.post("/api/execute/{symbol}")
def execute(symbol: str):
    symbol = symbol.upper()
    if symbol not in {"BTCUSD", "ETHUSD", "XAUUSD", "EURUSD"}:
        raise HTTPException(404, "Unsupported symbol")
    with lock:
        if state["kill_switch"]:
            raise HTTPException(409, "Kill switch is active")
        signal = analyze(symbol)
        if signal["data_status"] != "fresh" or signal["direction"] == "HOLD" or signal["score"] < 72:
            raise HTTPException(409, "Entry gate blocked this trade")
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
        try:
            price, _, _, _ = quote(pos["symbol"])
        except Exception:
            price = pos["entry"]
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
