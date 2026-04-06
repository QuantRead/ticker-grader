"""
QuantRead Ticker Grader — Freemium SaaS Tool
==================================================
FastAPI backend that accepts a ticker symbol and returns a
QuantRead-style conviction grade using real market data.

Free tier: 3 grades/day (full grade + blurred indicators)
Pro tier:  Unlimited grades, full indicator breakdown

Run:  python server.py
Open: http://localhost:8000
"""

import os
import math
import datetime
import time
from collections import defaultdict
import numpy as np
import yfinance as yf
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="QuantRead Ticker Grader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Usage Tracking (In-Memory, resets on deploy) ────────────────
FREE_DAILY_LIMIT = 3

# { "ip_address": { "date": "2026-04-06", "count": 2, "tickers": ["NVDA", "AAPL"] } }
usage_tracker: dict = defaultdict(lambda: {"date": "", "count": 0, "tickers": []})


def get_usage(ip: str) -> dict:
    """Get or reset daily usage for an IP address."""
    today = datetime.date.today().isoformat()
    record = usage_tracker[ip]
    if record["date"] != today:
        record["date"] = today
        record["count"] = 0
        record["tickers"] = []
    return record


def increment_usage(ip: str, ticker: str) -> dict:
    """Increment usage count. Returns updated record."""
    record = get_usage(ip)
    record["count"] += 1
    if ticker not in record["tickers"]:
        record["tickers"].append(ticker)
    return record


# ─── Grading Engine ──────────────────────────────────────────────

def calculate_ema(prices, period):
    """Calculate Exponential Moving Average."""
    ema = [prices[0]]
    multiplier = 2 / (period + 1)
    for price in prices[1:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema


def grade_ticker(symbol: str) -> dict:
    """
    Pull market data and calculate a QuantRead-style conviction grade.
    Returns a rich analysis object.
    """
    try:
        ticker = yf.Ticker(symbol.upper())
        hist = ticker.history(period="3mo", interval="1d")

        if hist.empty or len(hist) < 30:
            return None

        closes = hist["Close"].values.tolist()
        volumes = hist["Volume"].values.tolist()
        highs = hist["High"].values.tolist()
        lows = hist["Low"].values.tolist()

        current_price = closes[-1]
        prev_close = closes[-2] if len(closes) > 1 else current_price

        # ─── 1. EMA Ribbon (8/21/34/55) ──────────────────────
        ema_8 = calculate_ema(closes, 8)
        ema_21 = calculate_ema(closes, 21)
        ema_34 = calculate_ema(closes, 34)
        ema_55 = calculate_ema(closes, 55)

        # Check alignment: 8 > 21 > 34 > 55 = BULL
        e8, e21, e34, e55 = ema_8[-1], ema_21[-1], ema_34[-1], ema_55[-1]

        if e8 > e21 > e34 > e55:
            ribbon_status = "BULL"
            ribbon_score = 5
        elif e8 > e21 > e34:
            ribbon_status = "BULL"
            ribbon_score = 4
        elif e8 > e21:
            ribbon_status = "NEUTRAL"
            ribbon_score = 3
        elif e8 < e21 < e34 < e55:
            ribbon_status = "BEAR"
            ribbon_score = 0
        elif e8 < e21 < e34:
            ribbon_status = "BEAR"
            ribbon_score = 1
        else:
            ribbon_status = "NEUTRAL"
            ribbon_score = 2

        # ─── 2. Relative Volume (RVOL) ───────────────────────
        avg_vol_20 = np.mean(volumes[-21:-1]) if len(volumes) > 21 else np.mean(volumes[:-1])
        current_vol = volumes[-1]
        rvol = current_vol / avg_vol_20 if avg_vol_20 > 0 else 1.0

        if rvol >= 2.0:
            rvol_score = 5
        elif rvol >= 1.5:
            rvol_score = 4
        elif rvol >= 1.0:
            rvol_score = 3
        elif rvol >= 0.7:
            rvol_score = 2
        else:
            rvol_score = 1

        # ─── 3. RSI (14-period) ──────────────────────────────
        deltas = np.diff(closes[-15:])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains) if len(gains) else 0
        avg_loss = np.mean(losses) if len(losses) else 0.001
        rs = avg_gain / avg_loss if avg_loss > 0 else 100
        rsi = 100 - (100 / (1 + rs))

        if 40 <= rsi <= 65:
            rsi_score = 5  # Ideal momentum zone
        elif 30 <= rsi < 40 or 65 < rsi <= 75:
            rsi_score = 3
        elif rsi > 75:
            rsi_score = 1  # Overbought
            rsi_label = "OVERBOUGHT"
        elif rsi < 30:
            rsi_score = 1  # Oversold
            rsi_label = "OVERSOLD"
        else:
            rsi_score = 2

        if rsi > 70:
            rsi_label = "OVERBOUGHT"
        elif rsi < 30:
            rsi_label = "OVERSOLD"
        elif rsi >= 50:
            rsi_label = "BULLISH"
        else:
            rsi_label = "NEUTRAL"

        # ─── 4. ATR (14-period, volatility) ──────────────────
        trs = []
        for i in range(1, min(15, len(closes))):
            tr = max(
                highs[-(15-i)] - lows[-(15-i)],
                abs(highs[-(15-i)] - closes[-(15-i+1)]),
                abs(lows[-(15-i)] - closes[-(15-i+1)])
            )
            trs.append(tr)
        atr = np.mean(trs) if trs else 0
        atr_pct = (atr / current_price) * 100 if current_price > 0 else 0

        if 0.5 <= atr_pct <= 3.0:
            atr_score = 5  # Best range for day trading
        elif 3.0 < atr_pct <= 5.0:
            atr_score = 3
        elif atr_pct > 5.0:
            atr_score = 1  # Too volatile
        else:
            atr_score = 2  # Too sleepy

        # ─── 5. Momentum (5-day price change) ────────────────
        if len(closes) >= 6:
            momentum = ((closes[-1] - closes[-6]) / closes[-6]) * 100
        else:
            momentum = 0

        if momentum > 3:
            mom_score = 5
        elif momentum > 1:
            mom_score = 4
        elif momentum > -1:
            mom_score = 3
        elif momentum > -3:
            mom_score = 2
        else:
            mom_score = 1

        # ─── 6. Price vs 20-day SMA (trend confirmation) ────
        sma_20 = np.mean(closes[-20:]) if len(closes) >= 20 else np.mean(closes)
        above_sma = current_price > sma_20
        sma_score = 4 if above_sma else 1
        trend = "ABOVE" if above_sma else "BELOW"

        # ─── COMPOSITE GRADE ─────────────────────────────────
        weights = {
            "ribbon": 0.25,
            "rvol": 0.20,
            "rsi": 0.15,
            "atr": 0.10,
            "momentum": 0.15,
            "sma": 0.15,
        }

        raw_score = (
            ribbon_score * weights["ribbon"]
            + rvol_score * weights["rvol"]
            + rsi_score * weights["rsi"]
            + atr_score * weights["atr"]
            + mom_score * weights["momentum"]
            + sma_score * weights["sma"]
        )

        # Scale to 0-100
        final_score = round((raw_score / 5) * 100)

        if final_score >= 80:
            grade = "A"
            verdict = "Strong Setup — High Conviction"
        elif final_score >= 65:
            grade = "B"
            verdict = "Solid Setup — Moderate Conviction"
        elif final_score >= 50:
            grade = "C"
            verdict = "Neutral — Wait for Confirmation"
        elif final_score >= 35:
            grade = "D"
            verdict = "Weak — Proceed with Caution"
        else:
            grade = "F"
            verdict = "Avoid — Unfavorable Conditions"

        # Day change
        day_change = current_price - prev_close
        day_change_pct = (day_change / prev_close) * 100 if prev_close > 0 else 0

        # Get company info
        try:
            info = ticker.info
            company_name = info.get("shortName", info.get("longName", symbol.upper()))
            sector = info.get("sector", "—")
            market_cap = info.get("marketCap", 0)
        except:
            company_name = symbol.upper()
            sector = "—"
            market_cap = 0

        # Format market cap
        if market_cap >= 1_000_000_000_000:
            mc_str = f"${market_cap / 1_000_000_000_000:.1f}T"
        elif market_cap >= 1_000_000_000:
            mc_str = f"${market_cap / 1_000_000_000:.1f}B"
        elif market_cap >= 1_000_000:
            mc_str = f"${market_cap / 1_000_000:.1f}M"
        elif market_cap > 0:
            mc_str = f"${market_cap:,.0f}"
        else:
            mc_str = "—"

        return {
            "ticker": symbol.upper(),
            "company_name": company_name,
            "sector": sector,
            "market_cap": mc_str,
            "price": round(current_price, 2),
            "day_change": round(day_change, 2),
            "day_change_pct": round(day_change_pct, 2),
            "grade": grade,
            "score": final_score,
            "verdict": verdict,
            "indicators": {
                "ema_ribbon": {
                    "status": ribbon_status,
                    "score": ribbon_score,
                    "ema_8": round(e8, 2),
                    "ema_21": round(e21, 2),
                    "ema_34": round(e34, 2),
                    "ema_55": round(e55, 2),
                },
                "rvol": {
                    "value": round(rvol, 2),
                    "score": rvol_score,
                    "avg_volume": int(avg_vol_20),
                    "current_volume": int(current_vol),
                },
                "rsi": {
                    "value": round(rsi, 1),
                    "score": rsi_score,
                    "label": rsi_label,
                },
                "atr": {
                    "value": round(atr, 2),
                    "pct": round(atr_pct, 2),
                    "score": atr_score,
                },
                "momentum": {
                    "five_day_pct": round(momentum, 2),
                    "score": mom_score,
                },
                "trend": {
                    "sma_20": round(sma_20, 2),
                    "status": trend,
                    "score": sma_score,
                },
            },
            "timestamp": datetime.datetime.now().isoformat(),
        }

    except Exception as e:
        print(f"Error grading {symbol}: {e}")
        return None


# ─── API Routes ──────────────────────────────────────────────────

@app.get("/api/grade/{symbol}")
async def api_grade(symbol: str, request: Request):
    """Grade a ticker symbol and return the analysis."""
    symbol = symbol.upper().strip()
    if not symbol or len(symbol) > 10:
        raise HTTPException(status_code=400, detail="Invalid ticker symbol")

    # ─── Usage tracking ──────────────────────────────────────
    client_ip = request.client.host if request.client else "unknown"
    # Check for proxy headers (Render uses reverse proxy)
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()

    record = get_usage(client_ip)
    is_pro = False  # TODO: check subscription status via Stripe/Whop

    remaining = max(0, FREE_DAILY_LIMIT - record["count"])
    limit_reached = remaining <= 0 and not is_pro

    if limit_reached:
        # Still return grade letter + score, but strip indicators
        result = grade_ticker(symbol)
        if result is None:
            raise HTTPException(status_code=404, detail=f"No data found for '{symbol}'.")

        # Strip detailed indicators for free users over limit
        result["indicators"] = None
        result["usage"] = {
            "remaining": 0,
            "limit": FREE_DAILY_LIMIT,
            "is_pro": False,
            "limit_reached": True,
        }
        return JSONResponse(content=result)

    result = grade_ticker(symbol)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No data found for '{symbol}'. Check the ticker symbol and try again.")

    # Increment usage after successful grade
    increment_usage(client_ip, symbol)
    new_remaining = max(0, FREE_DAILY_LIMIT - get_usage(client_ip)["count"])

    result["usage"] = {
        "remaining": new_remaining,
        "limit": FREE_DAILY_LIMIT,
        "is_pro": is_pro,
        "limit_reached": False,
    }

    return JSONResponse(content=result)


# ─── Static Files & Frontend ────────────────────────────────────

static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(static_dir, "index.html"))


# ─── Startup ────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"\n🚀 QuantRead Ticker Grader running at http://localhost:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port)
