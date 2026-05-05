from pathlib import Path

import pandas as pd

import server


class _FakeTicker:
    fast_info = {"last_price": 199.69}
    info = {
        "shortName": "NVIDIA Corporation",
        "sector": "Technology",
        "marketCap": 4_900_000_000_000,
    }

    def __init__(self, symbol):
        self.symbol = symbol

    def history(self, period="3mo", interval="1d", prepost=False):
        if interval == "5m":
            return _intraday_5m()
        if interval == "1h":
            return _hourly_1h()
        return _daily()


def _daily():
    closes = [185 + (i * 0.23) for i in range(59)] + [199.30]
    rows = []
    for i, close in enumerate(closes):
        volume = 148_200_000
        if i == len(closes) - 1:
            volume = 12_800_000
        rows.append(
            {
                "Open": close - 0.4,
                "High": close + 3.0,
                "Low": close - 3.0,
                "Close": close,
                "Volume": volume,
            }
        )
    return pd.DataFrame(rows)


def _intraday_5m():
    closes = [193 + (i * 0.5) for i in range(20)]
    volumes = [1_000_000 for _ in range(19)] + [100_000]
    return pd.DataFrame(
        {
            "Open": [close - 0.1 for close in closes],
            "High": [close + 0.4 for close in closes],
            "Low": [close - 0.4 for close in closes],
            "Close": closes,
            "Volume": volumes,
        }
    )


def _hourly_1h():
    closes = [230 - (i * 0.9) for i in range(40)]
    return pd.DataFrame(
        {
            "Open": [close + 0.2 for close in closes],
            "High": [close + 6.0 for close in closes],
            "Low": [close + 3.0 for close in closes],
            "Close": closes,
            "Volume": [20_000_000 for _ in closes],
        }
    )


def test_nvda_like_structural_red_flags_return_f(monkeypatch):
    monkeypatch.setattr(server.yf, "Ticker", _FakeTicker)
    monkeypatch.setattr(server, "_get_spy_closes", lambda: [500 + i for i in range(60)])

    result = server.grade_ticker("NVDA")

    assert result["grade"] == "F"
    assert result["score"] < 35
    assert result["component_scores"]["ribbon"] == 0
    assert result["indicators"]["ema_ribbon"]["status"] == "BEAR"
    assert result["indicators"]["ichimoku"]["above"] is False
    assert result["indicators"]["rsi"]["value"] > 75
    assert result["indicators"]["rvol"]["value"] < 0.7


def test_rvol_display_fields_match_scoring_source(monkeypatch):
    monkeypatch.setattr(server.yf, "Ticker", _FakeTicker)
    monkeypatch.setattr(server, "_get_spy_closes", lambda: [500 + i for i in range(60)])

    result = server.grade_ticker("NVDA")

    assert result["rvol_source"] == "intraday_5m"
    assert result["indicators"]["rvol"]["source"] == "intraday_5m"
    assert result["indicators"]["rvol"]["current_volume"] == 100_000
    assert result["indicators"]["rvol"]["avg_volume"] == 1_000_000
    assert result["indicators"]["rvol"]["value"] == 0.1


def test_public_copy_does_not_claim_same_execution_engine():
    public_copy = Path("static/index.html").read_text(encoding="utf-8").lower()

    forbidden = (
        "same conviction model",
        "same intraday engine",
        "automated trading bot",
        "automated trading system",
    )
    for phrase in forbidden:
        assert phrase not in public_copy
