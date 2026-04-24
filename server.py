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
        print("[OK] Upstash Redis connected -- usage tracking is persistent")
    else:
        print("[WARN] UPSTASH_REDIS env vars not set -- falling back to in-memory usage tracking")
except Exception as e:
    print(f"[WARN] Redis connection failed ({e}) -- falling back to in-memory usage tracking")

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
        print("[OK] Stripe API connected -- Pro verification enabled")
    except Exception as e:
        print(f"[WARN] Stripe setup failed ({e}) -- Pro verification disabled")
else:
    print("[WARN] STRIPE_SECRET_KEY not set -- Pro verification disabled")


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
            print(f"[WARN] Redis read failed ({e}) -- using in-memory fallback")

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
            print(f"[WARN] Redis write failed ({e}) -- count stored in-memory only")
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
    Aligned with Pine Script (saty_conviction_histogram.pine) factor model.

    Architecture: Additive base score + multiplicative penalty gates.
    Factors: RS vs SPY, 1H EMA Ribbon, RVOL, RSI, ATR Expansion, Ichimoku.
    Penalties: Cloud position, Ichimoku gate, RSI exhaustion, Catalyst bonus.
    """
    try:
        ticker = yf.Ticker(symbol.upper())
        hist = ticker.history(period="3mo", interval="1d")

        if hist.empty or len(hist) < 30:
            return None

        # ── DAILY DATA (preserved for daily-dependent factors) ────────
        daily_closes = hist["Close"].values.tolist()
        daily_volumes = hist["Volume"].values.tolist()
        daily_highs = hist["High"].values.tolist()
        daily_lows = hist["Low"].values.tolist()
        daily_opens = hist["Open"].values.tolist()

        current_price = daily_closes[-1]
        prev_close = daily_closes[-2] if len(daily_closes) > 1 else current_price

        # Override with live price (includes pre/post market)
        try:
            live_price = ticker.fast_info.get("last_price", None)
            if live_price and live_price > 0:
                current_price = float(live_price)
        except Exception:
            pass  # Fall back to daily close

        # ─── 1. DAILY ATR (Wilder's 14-period) ──────────────────────
        trs = []
        trs.append(daily_highs[0] - daily_lows[0])
        for i in range(1, len(daily_closes)):
            tr = max(
                daily_highs[i] - daily_lows[i],
                abs(daily_highs[i] - daily_closes[i-1]),
                abs(daily_lows[i] - daily_closes[i-1])
            )
            trs.append(tr)

        if len(trs) >= 14:
            daily_atr = sum(trs[:14]) / 14
            for tr_val in trs[14:]:
                daily_atr = (daily_atr * 13 + tr_val) / 14
        elif trs:
            daily_atr = np.mean(trs)
        else:
            daily_atr = 0

        atr_pct = (daily_atr / current_price) * 100 if current_price > 0 else 0

        # ─── 2. ATR EXPANSION RATIO (Pine: ATR / SMA(ATR, 20)) ──────
        atr_series = []
        if len(trs) >= 14:
            _running = sum(trs[:14]) / 14
            atr_series.append(_running)
            for tr_val in trs[14:]:
                _running = (_running * 13 + tr_val) / 14
                atr_series.append(_running)

        if len(atr_series) >= 20:
            atr_sma_20 = np.mean(atr_series[-20:])
            atr_ratio = daily_atr / atr_sma_20 if atr_sma_20 > 0 else 1.0
        else:
            atr_ratio = 1.0

        if atr_ratio >= 1.4:
            atr_score = 5
        elif atr_ratio >= 1.25:
            atr_score = 4
        elif atr_ratio >= 1.0:
            atr_score = 3
        elif atr_ratio >= 0.9:
            atr_score = 2
        else:
            atr_score = 1

        # ─── 3. SATY ATR CLOUD (Pine's strongest factor: 1.618x) ────
        today_open = daily_opens[-1]
        upper_cloud_lo = today_open + (1.25 * daily_atr)
        upper_cloud_hi = today_open + (1.50 * daily_atr)
        lower_cloud_hi = today_open - (1.25 * daily_atr)
        lower_cloud_lo = today_open - (1.50 * daily_atr)

        in_upper_cloud = upper_cloud_lo <= current_price <= upper_cloud_hi
        in_lower_cloud = lower_cloud_lo <= current_price <= lower_cloud_hi
        in_cloud = in_upper_cloud or in_lower_cloud

        if in_cloud:
            cloud_status = "IN ZONE"
        elif current_price > upper_cloud_hi:
            cloud_status = "EXTENDED"
        elif current_price < lower_cloud_lo:
            cloud_status = "EXTENDED SHORT"
        else:
            cloud_status = "BELOW ZONE"

        # ─── 4. ICHIMOKU BASELINE (Moved to 1H section below) ───────
        # Default fallbacks
        ichimoku_base = current_price
        above_ichimoku = True
        ichimoku_score = 5

        # ─── 5. RS vs SPY (21-day, DAILY data only) ─────────────────
        spy_closes = _get_spy_closes()
        rs_vs_spy = 1.0
        rs_label = "NEUTRAL"
        if len(daily_closes) >= 21 and len(spy_closes) >= 21:
            stock_return = daily_closes[-1] / max(daily_closes[-21], 0.01)
            spy_return = spy_closes[-1] / max(spy_closes[-21], 0.01)
            rs_vs_spy = stock_return / spy_return if spy_return > 0 else 1.0

        if rs_vs_spy >= 1.15:
            rs_score, rs_label = 5, "LEADER"
        elif rs_vs_spy >= 1.05:
            rs_score, rs_label = 4, "STRONG"
        elif rs_vs_spy >= 0.95:
            rs_score, rs_label = 3, "NEUTRAL"
        elif rs_vs_spy >= 0.85:
            rs_score, rs_label = 2, "LAGGING"
        else:
            rs_score, rs_label = 1, "WEAK"

        # ─── 6. INTRADAY DATA (5m) — RSI & RVOL ────────────────────
        intraday_rsi = None
        intraday_rvol = None
        data_source = "daily"

        try:
            intra_ticker = yf.Ticker(symbol)
            intra = intra_ticker.history(period="5d", interval="5m", prepost=False)
            if not intra.empty and len(intra) >= 5:
                intra_closes = intra["Close"].dropna().values.tolist()
                intra_volumes = intra["Volume"].dropna().values.tolist()
                if len(intra_closes) >= 15:
                    intra_deltas = [intra_closes[i] - intra_closes[i-1] for i in range(1, len(intra_closes))]
                    intra_gains = [d if d > 0 else 0 for d in intra_deltas[-14:]]
                    intra_losses = [-d if d < 0 else 0 for d in intra_deltas[-14:]]
                    _ig = sum(intra_gains) / max(len(intra_gains), 1)
                    _il = sum(intra_losses) / max(len(intra_losses), 1)
                    intraday_rsi = 100 - (100 / (1 + _ig / _il)) if _il > 0 else 100.0
                if len(intra_volumes) >= 2:
                    _recent_vol = intra_volumes[-1]
                    _avg_vol = sum(intra_volumes[:-1]) / max(len(intra_volumes) - 1, 1)
                    if _avg_vol > 0:
                        intraday_rvol = _recent_vol / _avg_vol
                data_source = "intraday_5m"
        except Exception as e:
            print(f"[Intraday] {symbol}: fetch failed: {e}")

        # RSI
        if intraday_rsi is not None:
            rsi = intraday_rsi
        else:
            deltas = np.diff(daily_closes[-15:])
            gains = np.where(deltas > 0, deltas, 0)
            losses = np.where(deltas < 0, -deltas, 0)
            avg_gain = np.mean(gains) if len(gains) else 0
            avg_loss = np.mean(losses) if len(losses) else 0.001
            rs_val = avg_gain / avg_loss if avg_loss > 0 else 100
            rsi = 100 - (100 / (1 + rs_val))

        if 40 <= rsi <= 65:
            rsi_score = 5
        elif 30 <= rsi < 40 or 65 < rsi <= 75:
            rsi_score = 3
        elif rsi > 75 or rsi < 30:
            rsi_score = 1
        else:
            rsi_score = 2

        rsi_label = "OVERBOUGHT" if rsi > 70 else "OVERSOLD" if rsi < 30 else "BULLISH" if rsi >= 50 else "NEUTRAL"

        # RVOL
        avg_vol_20 = np.mean(daily_volumes[-21:-1]) if len(daily_volumes) > 21 else (np.mean(daily_volumes[:-1]) if len(daily_volumes) > 1 else 1)
        current_vol = daily_volumes[-1]
        rvol = intraday_rvol if intraday_rvol is not None else (current_vol / avg_vol_20 if avg_vol_20 > 0 else 1.0)

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

        # ─── 7. CATALYST DETECTION (Pine: gap >4% AND rvol >2.5) ───
        gap_pct = ((today_open - prev_close) / prev_close) * 100 if prev_close > 0 else 0
        is_catalyst = abs(gap_pct) > 4.0 and rvol > 2.5

        # ─── 8. 1H EMA RIBBON (8/21/34) — Pine: request.security("60") ──
        ribbon_source = "daily"
        try:
            hourly = yf.Ticker(symbol).history(period="5d", interval="1h")
            if not hourly.empty and len(hourly) >= 35:
                h_closes = hourly["Close"].dropna().values.tolist()
                h_highs = hourly["High"].dropna().values.tolist()
                h_lows = hourly["Low"].dropna().values.tolist()
                
                ema_8 = calculate_ema(h_closes, 8)
                ema_21 = calculate_ema(h_closes, 21)
                ema_34 = calculate_ema(h_closes, 34)
                e8, e21, e34 = ema_8[-1], ema_21[-1], ema_34[-1]
                ribbon_source = "1h"

                # Calculate Ichimoku on 1H data (26 hourly periods = ~3.7 days)
                if len(h_highs) >= 26 and len(h_lows) >= 26:
                    ichi_high = max(h_highs[-26:])
                    ichi_low = min(h_lows[-26:])
                    ichimoku_base = (ichi_high + ichi_low) / 2.0
                    above_ichimoku = current_price > ichimoku_base
                    ichimoku_score = 5 if above_ichimoku else 1

            else:
                raise ValueError("Insufficient 1H data")
        except Exception as e:
            print(f"[1H Ribbon] {symbol}: using daily fallback: {e}")
            ema_8 = calculate_ema(daily_closes, 8)
            ema_21 = calculate_ema(daily_closes, 21)
            ema_34 = calculate_ema(daily_closes, 34)
            e8, e21, e34 = ema_8[-1], ema_21[-1], ema_34[-1]
            
            # Daily fallback for Ichimoku
            if len(daily_highs) >= 26 and len(daily_lows) >= 26:
                ichi_high = max(daily_highs[-26:])
                ichi_low = min(daily_lows[-26:])
                ichimoku_base = (ichi_high + ichi_low) / 2.0
                above_ichimoku = current_price > ichimoku_base
                ichimoku_score = 5 if above_ichimoku else 1

        # Ribbon scoring (includes trend — slope check merged in)
        ema_slope_positive = len(ema_8) >= 3 and ema_8[-1] > ema_8[-3]

        if e8 > e21 > e34 and ema_slope_positive:
            ribbon_status, ribbon_score = "BULL", 5
        elif e8 > e21 > e34:
            ribbon_status, ribbon_score = "BULL", 4
        elif e8 > e21 and ema_slope_positive:
            ribbon_status, ribbon_score = "NEUTRAL", 3
        elif e8 > e21:
            ribbon_status, ribbon_score = "NEUTRAL", 3
        elif e8 < e21 < e34:
            ribbon_status, ribbon_score = "BEAR", 0
        elif e8 < e21:
            ribbon_status, ribbon_score = "BEAR", 1
        else:
            ribbon_status, ribbon_score = "NEUTRAL", 2

        # ═══ COMPOSITE GRADE ═══════════════════════════════════════
        # Step 1: Additive base score (weighted average, 0-100)
        weights = {
            "rs_vs_spy": 0.25,
            "ribbon": 0.25,
            "rvol": 0.15,
            "rsi": 0.10,
            "atr": 0.15,
            "ichimoku": 0.10,
        }

        raw_score = (
            rs_score * weights["rs_vs_spy"]
            + ribbon_score * weights["ribbon"]
            + rvol_score * weights["rvol"]
            + rsi_score * weights["rsi"]
            + atr_score * weights["atr"]
            + ichimoku_score * weights["ichimoku"]
        )

        # Scale to 0-100
        base_score = round((raw_score / 5) * 100)

        # Step 2: Multiplicative penalties (Pine alignment)
        # These make "one bad critical factor tanks the grade"
        cloud_mult = 1.0 if in_cloud else 0.75
        ichi_mult = 1.0 if above_ichimoku else 0.85
        rsi_mult = 0.50 if rsi > 75 else (1.05 if rsi <= 25 else 1.0)
        cat_mult = 1.25 if is_catalyst else 1.0

        final_score = round(base_score * cloud_mult * ichi_mult * rsi_mult * cat_mult)
        final_score = max(0, min(100, final_score))

        # Step 3: Grade assignment
        rsi_penalty_applied = rsi > 75

        if final_score >= 80:
            grade, verdict = "A", "Strong Setup — High Conviction"
        elif final_score >= 65:
            grade, verdict = "B", "Solid Setup — Moderate Conviction"
        elif final_score >= 50:
            grade, verdict = "C", "Neutral — Wait for Confirmation"
        elif final_score >= 35:
            grade = "D"
            verdict = "RSI Overextended — Wait for Pullback" if rsi_penalty_applied else "Weak — Proceed with Caution"
        else:
            grade, verdict = "F", "Avoid — Unfavorable Conditions"

        # ═══ METADATA ══════════════════════════════════════════════
        day_change = current_price - prev_close
        day_change_pct = (day_change / prev_close) * 100 if prev_close > 0 else 0

        try:
            info = ticker.info
            company_name = info.get("shortName", info.get("longName", symbol.upper()))
            sector = info.get("sector", "—")
            market_cap = info.get("marketCap", 0)
        except:
            company_name = symbol.upper()
            sector = "—"
            market_cap = 0

        # ─── SECTOR HEADWIND CHECK ─────────────────────────────────
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
                    "source": ribbon_source,
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
                    "value": round(daily_atr, 2),
                    "pct": round(atr_pct, 2),
                    "expansion_ratio": round(atr_ratio, 2),
                    "score": atr_score,
                },
                "rs_vs_spy": {
                    "value": round(rs_vs_spy, 3),
                    "score": rs_score,
                    "label": rs_label,
                },
                "ichimoku": {
                    "baseline": round(ichimoku_base, 2),
                    "above": above_ichimoku,
                    "score": ichimoku_score,
                },
                "cloud": {
                    "status": cloud_status,
                    "in_cloud": in_cloud,
                    "upper_lo": round(upper_cloud_lo, 2),
                    "upper_hi": round(upper_cloud_hi, 2),
                    "lower_lo": round(lower_cloud_lo, 2),
                    "lower_hi": round(lower_cloud_hi, 2),
                    "daily_open": round(today_open, 2),
                },
                "catalyst": {
                    "detected": is_catalyst,
                    "gap_pct": round(gap_pct, 2),
                    "rvol_threshold": rvol >= 2.5,
                },
            },
            "multipliers": {
                "cloud": round(cloud_mult, 2),
                "ichimoku": round(ichi_mult, 2),
                "rsi": round(rsi_mult, 2),
                "catalyst": round(cat_mult, 2),
                "combined": round(cloud_mult * ichi_mult * rsi_mult * cat_mult, 3),
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
                        print(f"[WARN] Redis write failed for Pro cache ({e})")

                print(f"[OK] Pro verified: {email} -> {client_ip}")
                return JSONResponse(content={"is_pro": True, "email": email})

        return JSONResponse(content={
            "is_pro": False,
            "message": "No active Ticker Grader Pro subscription found for this email."
        })

    except Exception as e:
        print(f"[ERR] Stripe verification error: {e}")
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
