# QuantRead Ticker Grader

Free institutional-grade conviction scoring for any stock. Powered by the QuantRead Indicator Suite.

## Features
- 6-factor conviction scoring (EMA Ribbon, RVOL, RSI, ATR, Momentum, Trend)
- Real-time market data via yfinance
- Premium dark-mode UI
- A/B/C/D/F grading system

## Run Locally
```bash
pip install -r requirements.txt
python server.py
# Open http://localhost:8000
```

## Deploy to Render
1. Push this repo to GitHub
2. Connect to Render.com
3. Create a new Web Service → select this repo
4. Render auto-detects Python, uses `requirements.txt`
5. Set Start Command: `python server.py`

## API
```
GET /api/grade/{TICKER}
```
Returns JSON with grade, score, verdict, and all indicator values.

## Built by
[Dante Mudd](https://quantread.app) — QuantRead
