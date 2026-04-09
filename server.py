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
import json
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

# ─── Usage Tracking (Upstash Redis — persistent across deploys) ──
FREE_DAILY_LIMIT = 3

# Upstash Redis connection (HTTP-based, no TCP pool needed)
_redis_client = None
_REDIS_AVAILABLE = False

try:
    _upstash_url = os.environ.get("UPSTASH_REDIS_REST_URL")
    _upstash_token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if _upstash_url and _upstash_token:
        from upstash_redis import Redis
        _redis_client = Redis(url=_upstash_url, token=_upstash_token)
        _redis_client.ping()  # Verify connection at startup
        _REDIS_AVAILABLE = True
        print("✅ Upstash Redis connected — usage tracking is persistent")
    else:
        print("⚠️  UPSTASH_REDIS env vars not set — falling back to in-memory usage tracking")
except Exception as e:
    print(f"⚠️  Redis connection failed ({e}) — falling back to in-memory usage tracking")

# In-memory fallback (same as before, used only when Redis is unavailable)
_mem_tracker: dict = defaultdict(lambda: {"date": "", "count": 0, "tickers": []})

_USAGE_TTL = 86400  # 24 hours in seconds
_PRO_TTL = 2592000  # 30 days in seconds

# ─── Stripe Integration (Pro verification) ───────────────────
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
TICKER_GRADER_PRO_PRICE_ID = "price_1TJIXV7XRFCkxuHsXEJykfwo"
_STRIPE_AVAILABLE = False

if STRIPE_SECRET_KEY:
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        _STRIPE_AVAILABLE = True
        print("✅ Stripe API connected — Pro verification enabled")
    except Exception as e:
        print(f"⚠️  Stripe setup failed ({e}) — Pro verification disabled")
else:
    print("⚠️  STRIPE_SECRET_KEY not set — Pro verification disabled")


def _redis_key(ip: str) -> str:
    """Build the Redis key for today's usage record."""
    return f"usage:{ip}:{datetime.date.today().isoformat()}"


def get_usage(ip: str) -> dict:
    """Get daily usage for an IP address. Reads from Redis if available, else in-memory."""
    today = datetime.date.today().isoformat()

    if _REDIS_AVAILABLE:
        try:
            raw = _redis_client.get(_redis_key(ip))
            if raw:
                data = json.loads(raw) if isinstance(raw, str) else raw
                return {"date": today, "count": data.get("count", 0), "tickers": data.get("tickers", [])}
            return {"date": today, "count": 0, "tickers": []}
        except Exception as e:
            print(f"⚠️  Redis read failed ({e}) — using in-memory fallback")

    # In-memory fallback
    record = _mem_tracker[ip]
    if record["date"] != today:
        record["date"] = today
        record["count"] = 0
        record["tickers"] = []
    return record


def increment_usage(ip: str, ticker: str) -> dict:
    """Increment usage count. Writes to Redis if available, else in-memory."""
    record = get_usage(ip)
    record["count"] += 1
    if ticker not in record["tickers"]:
        record["tickers"].append(ticker)

    if _REDIS_AVAILABLE:
        try:
            payload = json.dumps({"count": record["count"], "tickers": record["tickers"]})
            _redis_client.set(_redis_key(ip), payload, ex=_USAGE_TTL)
        except Exception as e:
            print(f"⚠️  Redis write failed ({e}) — count stored in-memory only")
            _mem_tracker[ip] = record

    else:
        _mem_tracker[ip] = record

    return record


def _get_client_ip(request: Request) -> str:
    """Extract the real client IP, handling Render's reverse proxy."""
    client_ip = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
    return client_ip


def _check_pro(ip: str) -> bool:
    """Check if an IP has verified Pro status in Redis."""
    if _REDIS_AVAILABLE:
        try:
            pro_email = _redis_client.get(f"pro:{ip}")
            if pro_email:
                return True
        except Exception:
            pass
    return False


# ─── Grading Engine ──────────────────────────────────────────────

# SPY benchmark cache — avoids redundant yfinance calls within the same minute
_spy_cache = {"closes": [], "fetched_at": 0}
_SPY_CACHE_TTL = 120  # 2 minutes


def _get_spy_closes() -> list:
    """Fetch SPY daily closes with a short-lived cache."""
    now = time.time()
    if _spy_cache["closes"] and (now - _spy_cache["fetched_at"]) < _SPY_CACHE_TTL:
        return _spy_cache["closes"]
    try:
        spy = yf.Ticker("SPY")
        spy_hist = spy.history(period="3mo", interval="1d")
        if not spy_hist.empty and len(spy_hist) >= 21:
            _spy_cache["closes"] = spy_hist["Close"].values.tolist()
            _spy_cache["fetched_at"] = now
    except Exception as e:
        print(f"SPY fetch failed: {e}")
    return _spy_cache["closes"]


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

        # ─── Intraday Data (5-min) ────────────────────────────
        # During market hours, fetch 5-minute candles for RSI and RVOL.
        # This matches the Pine Script's candle-by-candle calculations.
        # Off-hours: fall back to daily data (labeled accordingly).
        intraday_rsi = None
        intraday_rvol = None
        data_source = "daily"
        try:
            # Fresh ticker object avoids session/cache conflicts with the daily fetch
            intra_ticker = yf.Ticker(symbol)
            intra = intra_ticker.history(period="5d", interval="5m", prepost=False)
            print(f"[Intraday] {symbol}: {len(intra)} candles fetched")
            if not intra.empty and len(intra) >= 5:
                intra_closes = intra["Close"].dropna().values.tolist()
                intra_volumes = intra["Volume"].dropna().values.tolist()
                if len(intra_closes) >= 15:
                    # RSI on 5-min candles (matches Pine Script)
                    intra_deltas = [intra_closes[i] - intra_closes[i-1] for i in range(1, len(intra_closes))]
                    intra_gains = [d if d > 0 else 0 for d in intra_deltas[-14:]]
                    intra_losses = [-d if d < 0 else 0 for d in intra_deltas[-14:]]
                    _ig = sum(intra_gains) / max(len(intra_gains), 1)
                    _il = sum(intra_losses) / max(len(intra_losses), 1)
                    _irs = _ig / _il if _il > 0 else 100
                    intraday_rsi = 100 - (100 / (1 + _irs))
                    print(f"[Intraday] {symbol}: RSI={intraday_rsi:.1f}")
                # RVOL on 5-min candles
                if len(intra_volumes) >= 2:
                    _recent_vol = intra_volumes[-1]
                    _avg_vol = sum(intra_volumes[:-1]) / max(len(intra_volumes) - 1, 1)
                    if _avg_vol > 0:
                        intraday_rvol = _recent_vol / _avg_vol
                data_source = "intraday_5m"
            else:
                print(f"[Intraday] {symbol}: insufficient candles ({len(intra)}), using daily")
        except Exception as e:
            print(f"[Intraday] {symbol}: fetch failed: {e}")

        # ─── DATA SOURCE SWITCH ──────────────────────────────
        # The agent (source of truth) uses 5-minute candles for ALL
        # factor calculations. When intraday data is available, switch
        # the primary data arrays to intraday so ribbon, RVOL, trend,
        # and momentum all match the agent's assessment.
        # Keep daily data for RS vs SPY (needs 21-day lookback).
        daily_closes = closes  # Preserve for RS vs SPY
        if data_source == "intraday_5m" and len(intra) >= 20:
            try:
                closes = intra["Close"].dropna().values.tolist()
                volumes = intra["Volume"].dropna().values.tolist()
                highs = intra["High"].dropna().values.tolist()
                lows = intra["Low"].dropna().values.tolist()
                current_price = closes[-1]
                print(f"[Intraday] {symbol}: switched to 5m data ({len(closes)} candles)")
            except Exception as e:
                print(f"[Intraday] {symbol}: switch failed, using daily: {e}")


        # ─── 1. EMA Ribbon (8/21/34/55) ──────────────────────
        ema_8 = calculate_ema(closes, 8)
        ema_21 = calculate_ema(closes, 21)
        ema_34 = calculate_ema(closes, 34)
        ema_55 = calculate_ema(closes, 55)

        # Check alignment: 8 > 21 > 34 > 55 = BULL
        # Gap-aware: If EMAs are within 0.1% of each other, treat as aligned.
        # This prevents false BEAR grades on trending stocks where EMAs
        # differ by pennies (e.g. AMD: EMA8=$231.24 vs EMA21=$231.33 = 0.04%).
        e8, e21, e34, e55 = ema_8[-1], ema_21[-1], ema_34[-1], ema_55[-1]

        def _near(a, b, pct=0.001):
            """True if a and b are within pct of each other."""
            return abs(a - b) / max(abs(b), 1e-9) < pct

        # Count how many consecutive pairs are in bull order (or near-equal)
        _pairs_bull = sum([
            e8 > e21 or _near(e8, e21),
            e21 > e34 or _near(e21, e34),
            e34 > e55 or _near(e34, e55),
        ])
        _pairs_bear = sum([
            e8 < e21 or _near(e8, e21),
            e21 < e34 or _near(e21, e34),
            e34 < e55 or _near(e34, e55),
        ])

        if e8 > e21 > e34 > e55:
            ribbon_status = "BULL"
            ribbon_score = 5
        elif _pairs_bull >= 3 and e8 > e55:
            # Near-perfect alignment (gaps < 0.1%) — still bullish
            ribbon_status = "BULL"
            ribbon_score = 4
        elif e8 > e21 > e34:
            ribbon_status = "BULL"
            ribbon_score = 4
        elif e8 > e21 or (_near(e8, e21) and e8 > e55):
            ribbon_status = "NEUTRAL"
            ribbon_score = 3
        elif e8 < e21 < e34 < e55:
            ribbon_status = "BEAR"
            ribbon_score = 0
        elif _pairs_bear >= 3 and e8 < e55:
            ribbon_status = "BEAR"
            ribbon_score = 1
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
        # Use intraday RSI when available (matches Pine Script)
        if intraday_rsi is not None:
            rsi = intraday_rsi
        else:
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
            rsi_score = 1  # Overbought — matches agent RSI_EXHAUSTED
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

        # ─── 4. ATR (14-period, Wilder's RMA) ─────────────────
        # Switched from simple mean to Wilder's smoothing to match
        # TradingView's ta.atr(14) and the trading agent's computation.
        trs = []
        if len(closes) >= 2:
            trs.append(highs[0] - lows[0])  # First TR = High - Low
            for i in range(1, len(closes)):
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]),
                    abs(lows[i] - closes[i-1])
                )
                trs.append(tr)
        if len(trs) >= 14:
            atr = sum(trs[:14]) / 14  # First ATR = SMA
            for tr_val in trs[14:]:
                atr = (atr * 13 + tr_val) / 14  # Wilder's smoothing
        elif trs:
            atr = np.mean(trs)
        else:
            atr = 0
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

        # ─── 6. Trend Alignment (EMA slope) ────────────────
        sma_20 = np.mean(closes[-20:]) if len(closes) >= 20 else np.mean(closes)
        above_sma = current_price > sma_20
        # Trend uses EMA fast > EMA slow + positive slope (matches agent)
        ema_slope_positive = len(ema_8) >= 3 and ema_8[-1] > ema_8[-3]
        if e8 > e21 and ema_slope_positive:
            trend_score = 5
            trend = "ALIGNED"
        elif e8 > e21:
            trend_score = 3
            trend = "ABOVE"
        elif above_sma:
            trend_score = 2
            trend = "NEUTRAL"
        else:
            trend_score = 1
            trend = "BELOW"

        # ─── 7. Relative Strength vs SPY (agent's #1 factor) ──
        # Uses DAILY closes (not intraday) for 21-day comparison
        spy_closes = _get_spy_closes()
        rs_vs_spy = 1.0
        rs_label = "NEUTRAL"
        if len(daily_closes) >= 21 and len(spy_closes) >= 21:
            stock_return = daily_closes[-1] / max(daily_closes[-21], 0.01)
            spy_return = spy_closes[-1] / max(spy_closes[-21], 0.01)
            rs_vs_spy = stock_return / spy_return if spy_return > 0 else 1.0

        if rs_vs_spy >= 1.15:
            rs_score = 5
            rs_label = "LEADER"
        elif rs_vs_spy >= 1.05:
            rs_score = 4
            rs_label = "STRONG"
        elif rs_vs_spy >= 0.95:
            rs_score = 3
            rs_label = "NEUTRAL"
        elif rs_vs_spy >= 0.85:
            rs_score = 2
            rs_label = "LAGGING"
        else:
            rs_score = 1
            rs_label = "WEAK"

        # ─── COMPOSITE GRADE ─────────────────────────────────
        # Weights aligned with the trading agent's public factors:
        # Agent: RS=35%, Liquidity=20%, Trend=15%, ATR=10%
        # TG:    RS=30%, Ribbon=20%, RVOL=15%, Trend=15%, RSI=10%, ATR=10%
        # (Liquidity omitted — requires bid/ask spread, not available via yfinance)
        weights = {
            "rs_vs_spy": 0.30,
            "ribbon": 0.20,
            "rvol": 0.15,
            "trend": 0.15,
            "rsi": 0.10,
            "atr": 0.10,
        }

        raw_score = (
            rs_score * weights["rs_vs_spy"]
            + ribbon_score * weights["ribbon"]
            + rvol_score * weights["rvol"]
            + trend_score * weights["trend"]
            + rsi_score * weights["rsi"]
            + atr_score * weights["atr"]
        )

        # Scale to 0-100
        final_score = round((raw_score / 5) * 100)

        # ─── RSI OVERBOUGHT HARD PENALTY ──────────────────────
        # Matches the agent's RSI_EXHAUSTED gate and Pine Script behavior.
        # If RSI > 75, cap the grade at D regardless of other factors.
        # This prevents the Ticker Grader from showing "B" while the
        # Pine Script shows "D" on the same stock (RIOT incident).
        rsi_penalty_applied = False
        if rsi > 75:
            final_score = min(final_score, 40)  # Cap at D territory
            rsi_penalty_applied = True

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
            if rsi_penalty_applied:
                verdict = "RSI Overextended — Wait for Pullback"
            else:
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

        # ─── SECTOR HEADWIND CHECK ────────────────────────────
        # If the stock's sector ETF is down > 1% today, warn subscribers.
        # This is a lightweight version of the agent's macro alignment.
        # Maps GICS sectors to their SPDR ETFs.
        sector_headwind = False
        sector_etf_change = None
        _sector_etf_map = {
            "Technology": "XLK", "Healthcare": "XLV", "Financial Services": "XLF",
            "Financials": "XLF", "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP",
            "Energy": "XLE", "Industrials": "XLI", "Basic Materials": "XLB",
            "Real Estate": "XLRE", "Utilities": "XLU", "Communication Services": "XLC",
        }
        _etf_sym = _sector_etf_map.get(sector)
        if _etf_sym:
            try:
                _etf = yf.Ticker(_etf_sym)
                _etf_hist = _etf.history(period="2d", interval="1d")
                if not _etf_hist.empty and len(_etf_hist) >= 2:
                    _etf_prev = _etf_hist["Close"].values[-2]
                    _etf_now = _etf_hist["Close"].values[-1]
                    sector_etf_change = round(((_etf_now - _etf_prev) / _etf_prev) * 100, 2)
                    if sector_etf_change < -1.0:
                        sector_headwind = True
            except Exception as e:
                print(f"[Sector] {symbol}: ETF check failed for {_etf_sym}: {e}")

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
                    "score": trend_score,
                },
                "rs_vs_spy": {
                    "value": round(rs_vs_spy, 3),
                    "score": rs_score,
                    "label": rs_label,
                },
            },
            "sector_headwind": {
                "active": sector_headwind,
                "sector_etf": _etf_sym if _etf_sym else None,
                "etf_change_pct": sector_etf_change,
            } if _etf_sym else None,
            "data_source": data_source,
            "rsi_penalty": rsi_penalty_applied,
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
    client_ip = _get_client_ip(request)

    record = get_usage(client_ip)
    is_pro = _check_pro(client_ip)

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


@app.post("/api/verify-pro")
async def verify_pro(request: Request):
    """Verify Pro subscription status by checking Stripe for the user's email."""
    if not _STRIPE_AVAILABLE:
        return JSONResponse(
            status_code=503,
            content={"is_pro": False, "message": "Subscription verification is temporarily unavailable."}
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    email = body.get("email", "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Please enter a valid email address")

    client_ip = _get_client_ip(request)

    try:
        # Step 1: Find Stripe customer(s) by email
        customers = stripe.Customer.list(email=email, limit=5)

        for customer in customers.data:
            # Step 2: Check for active subscription to Ticker Grader Pro
            subscriptions = stripe.Subscription.list(
                customer=customer.id,
                price=TICKER_GRADER_PRO_PRICE_ID,
                status="active",
                limit=1,
            )
            if subscriptions.data:
                # Active subscription found — cache Pro status in Redis
                if _REDIS_AVAILABLE:
                    try:
                        _redis_client.set(f"pro:{client_ip}", email, ex=_PRO_TTL)
                    except Exception as e:
                        print(f"⚠️  Redis write failed for Pro cache ({e})")

                print(f"✅ Pro verified: {email} → {client_ip}")
                return JSONResponse(content={"is_pro": True, "email": email})

        return JSONResponse(content={
            "is_pro": False,
            "message": "No active Ticker Grader Pro subscription found for this email."
        })

    except Exception as e:
        print(f"❌ Stripe verification error: {e}")
        return JSONResponse(
            status_code=500,
            content={"is_pro": False, "message": "Verification failed. Please try again."}
        )


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
