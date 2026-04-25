"""
Trading Signal Agent - Nifty 50 Top 10 + 8 New Stocks
Runs via GitHub Actions every hour, 24/7, 365 days
Writes buy/sell/hold signals to Google Sheets

v3 CHANGES:
- Removed market hours restriction — runs 24/7
- Market status logged in every row (MARKET_OPEN / AFTER_HOURS / WEEKEND)
- Signals during after-hours flagged as LOW confidence
- All other v2 features retained
"""

import os
import json
import time
import datetime
import pytz
import pandas as pd
import numpy as np
import gspread
from google.oauth2.service_account import Credentials

IST = pytz.timezone("Asia/Kolkata")

STOCKS = [
    # Original 10
    "RELIANCE.NS",
    "TCS.NS",
    "HDFCBANK.NS",
    "INFY.NS",
    "ICICIBANK.NS",
    "HINDUNILVR.NS",
    "ITC.NS",
    "SBIN.NS",
    "BAJFINANCE.NS",
    "KOTAKBANK.NS",
    # New 8
    "WIPRO.NS",
    "AXISBANK.NS",
    "MARUTI.NS",
    "SUNPHARMA.NS",
    "ADANIENT.NS",
    "BHARTIARTL.NS",
    "TATAMOTORS.NS",
    "LTIM.NS",
]

UPSTOX_SYMBOLS = {
    "RELIANCE.NS":   "NSE_EQ|INE002A01018",
    "TCS.NS":        "NSE_EQ|INE467B01029",
    "HDFCBANK.NS":   "NSE_EQ|INE040A01034",
    "INFY.NS":       "NSE_EQ|INE009A01021",
    "ICICIBANK.NS":  "NSE_EQ|INE090A01021",
    "HINDUNILVR.NS": "NSE_EQ|INE030A01027",
    "ITC.NS":        "NSE_EQ|INE154A01025",
    "SBIN.NS":       "NSE_EQ|INE062A01020",
    "BAJFINANCE.NS": "NSE_EQ|INE296A01024",
    "KOTAKBANK.NS":  "NSE_EQ|INE237A01028",
    "WIPRO.NS":      "NSE_EQ|INE075A01022",
    "AXISBANK.NS":   "NSE_EQ|INE238A01034",
    "MARUTI.NS":     "NSE_EQ|INE585B01010",
    "SUNPHARMA.NS":  "NSE_EQ|INE044A01036",
    "ADANIENT.NS":   "NSE_EQ|INE423A01024",
    "BHARTIARTL.NS": "NSE_EQ|INE397D01024",
    "TATAMOTORS.NS": "NSE_EQ|INE155A01022",
    "LTIM.NS":       "NSE_EQ|INE214T01019",
}

SECTOR_MAP = {
    "TCS":        "IT",
    "INFY":       "IT",
    "WIPRO":      "IT",
    "LTIM":       "IT",
    "HDFCBANK":   "BANK",
    "ICICIBANK":  "BANK",
    "KOTAKBANK":  "BANK",
    "AXISBANK":   "BANK",
    "SBIN":       "BANK",
    "RELIANCE":   "OIL_GAS",
    "HINDUNILVR": "FMCG",
    "ITC":        "FMCG",
    "BAJFINANCE": "FINANCE",
    "MARUTI":     "AUTO",
    "TATAMOTORS": "AUTO",
    "SUNPHARMA":  "PHARMA",
    "ADANIENT":   "CONGLOMERATE",
    "BHARTIARTL": "TELECOM",
}

GOOGLE_SHEET_NAME = "Trading data"

# ── Thresholds ───────────────────────────────────────────────────────────────
RSI_OVERSOLD   = 45
RSI_OVERBOUGHT = 55
VOLUME_SPIKE   = 1.5
ADX_STRONG     = 25
ADX_WEAK       = 20
ATR_SL_MULT    = 1.5
ATR_TGT_MULT   = 2.5
COOLDOWN_HOURS = 4


# ════════════════════════════════════════════════════════════════════════════
# MARKET STATUS
# ════════════════════════════════════════════════════════════════════════════

def get_market_status() -> str:
    """
    Returns:
      MARKET_OPEN   — 9:15 AM to 3:30 PM IST, Mon–Fri
      AFTER_HOURS   — Weekday but outside trading hours
      WEEKEND       — Saturday or Sunday
    """
    now = datetime.datetime.now(IST)
    if now.weekday() >= 5:
        return "WEEKEND"
    market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if market_open <= now <= market_close:
        return "MARKET_OPEN"
    return "AFTER_HOURS"


def is_noisy_time() -> bool:
    """First 15 min (9:15–9:30) and last 15 min (3:15–3:30) of session."""
    now = datetime.datetime.now(IST)
    open_noise_end    = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_noise_start = now.replace(hour=15, minute=15, second=0, microsecond=0)
    open_time         = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_time        = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return (open_time <= now <= open_noise_end) or (close_noise_start <= now <= close_time)


# ════════════════════════════════════════════════════════════════════════════
# GOOGLE SHEETS
# ════════════════════════════════════════════════════════════════════════════

def get_sheet():
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise EnvironmentError("GOOGLE_CREDENTIALS_JSON env var not set")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open(GOOGLE_SHEET_NAME)
    try:
        sheet = spreadsheet.worksheet("Signals")
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="Signals", rows=10000, cols=27)
        headers = [
            "Date", "Time", "Symbol", "Sector", "LTP", "Signal",
            "RSI", "MACD", "MACD_Signal", "EMA9", "EMA21",
            "Volume", "Avg_Volume", "Vol_Ratio", "Confidence", "Notes",
            "ADX", "BB_Upper", "BB_Mid", "BB_Lower",
            "ATR", "Stop_Loss", "Target", "Risk_Reward",
            "Nifty_Trend", "Candle_Pattern", "Market_Status",
        ]
        sheet.append_row(headers)
    return sheet


def get_recent_signals(sheet, symbol, hours=COOLDOWN_HOURS):
    """Return last signal & timestamp for cooldown check."""
    try:
        records = sheet.get_all_records()
        df = pd.DataFrame(records)
        if df.empty:
            return None, None
        sym_df = df[df["Symbol"] == symbol].copy()
        if sym_df.empty:
            return None, None
        sym_df = sym_df.tail(1)
        last_signal  = sym_df["Signal"].values[0]
        last_date    = sym_df["Date"].values[0]
        last_time    = sym_df["Time"].values[0]
        return last_signal, f"{last_date} {last_time}"
    except Exception as e:
        print(f"  Could not read recent signals: {e}")
        return None, None


def is_cooldown_active(last_signal, last_datetime_str, current_signal):
    """True if same signal was given within COOLDOWN_HOURS."""
    if last_signal != current_signal or last_datetime_str is None:
        return False
    try:
        last_dt  = datetime.datetime.strptime(last_datetime_str, "%Y-%m-%d %H:%M")
        last_dt  = IST.localize(last_dt)
        now      = datetime.datetime.now(IST)
        diff_hrs = (now - last_dt).total_seconds() / 3600
        return diff_hrs < COOLDOWN_HOURS
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════════════
# DATA FETCHING
# ════════════════════════════════════════════════════════════════════════════

def fetch_historical(symbol: str) -> pd.DataFrame:
    import requests
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://finance.yahoo.com",
        "Referer": "https://finance.yahoo.com/",
    }

    # 15m data
    url = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=15m&range=30d&includePrePost=false"
    )
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data   = r.json()
        result = data["chart"]["result"][0]
        ohlcv  = result["indicators"]["quote"][0]
        df = pd.DataFrame({
            "Open":   ohlcv["open"],
            "High":   ohlcv["high"],
            "Low":    ohlcv["low"],
            "Close":  ohlcv["close"],
            "Volume": ohlcv["volume"],
        }, index=pd.to_datetime(result["timestamp"], unit="s", utc=True))
        df.dropna(inplace=True)
        print(f"  Fetched {len(df)} rows (15m)")
        return df
    except Exception as e:
        print(f"  Yahoo 15m failed: {e}")

    # Daily fallback
    url_daily = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range=3mo&includePrePost=false"
    )
    try:
        r = requests.get(url_daily, headers=headers, timeout=15)
        r.raise_for_status()
        data   = r.json()
        result = data["chart"]["result"][0]
        ohlcv  = result["indicators"]["quote"][0]
        df = pd.DataFrame({
            "Open":   ohlcv["open"],
            "High":   ohlcv["high"],
            "Low":    ohlcv["low"],
            "Close":  ohlcv["close"],
            "Volume": ohlcv["volume"],
        }, index=pd.to_datetime(result["timestamp"], unit="s", utc=True))
        df.dropna(inplace=True)
        print(f"  Fetched {len(df)} rows (daily fallback)")
        return df
    except Exception as e2:
        print(f"  Yahoo daily fallback failed: {e2}")
        return pd.DataFrame()


def fetch_upstox_ltp(symbol: str):
    try:
        from upstox_client import ApiClient, Configuration, MarketQuoteApi
        token = os.environ.get("UPSTOX_ACCESS_TOKEN")
        if not token:
            return None
        config = Configuration()
        config.access_token = token
        config.host = "https://api-v2.upstox.com"
        api_client = ApiClient(config)
        api = MarketQuoteApi(api_client)
        instrument_key = UPSTOX_SYMBOLS.get(symbol)
        if not instrument_key:
            return None
        resp = api.get_full_market_quote([instrument_key], api_version="2.0")
        data = resp.data
        if data:
            first = list(data.values())[0]
            return float(first.last_price)
    except Exception as e:
        print(f"  Upstox LTP fetch failed: {e}")
    return None


def fetch_nifty_trend() -> str:
    df = fetch_historical("^NSEI")
    if df.empty:
        print("  Could not fetch Nifty trend — defaulting to NEUTRAL")
        return "NEUTRAL"
    ema9  = calc_ema(df["Close"], 9).iloc[-1]
    ema21 = calc_ema(df["Close"], 21).iloc[-1]
    if ema9 > ema21 * 1.002:
        trend = "UP"
    elif ema9 < ema21 * 0.998:
        trend = "DOWN"
    else:
        trend = "NEUTRAL"
    print(f"  Nifty 50 Trend: {trend} (EMA9={ema9:.1f}, EMA21={ema21:.1f})")
    return trend


# ════════════════════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ════════════════════════════════════════════════════════════════════════════

def calc_rsi(series, period=14):
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_macd(series, fast=12, slow=26, signal=9):
    ema_fast    = series.ewm(span=fast, adjust=False).mean()
    ema_slow    = series.ewm(span=slow, adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def calc_volume_ratio(volume, window=20):
    avg_vol = volume.rolling(window=window).mean()
    return volume / avg_vol.replace(0, np.nan)


def calc_atr(df, period=14) -> float:
    high  = df["High"]
    low   = df["Low"]
    close = df["Close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean().iloc[-1]


def calc_adx(df, period=14) -> float:
    high  = df["High"]
    low   = df["Low"]
    close = df["Close"]
    plus_dm  = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    mask  = high.diff().abs() <= (-low.diff()).abs()
    plus_dm[mask] = 0
    mask2 = (-low.diff()).abs() <= high.diff().abs()
    minus_dm[mask2] = 0
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr      = tr.ewm(span=period, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan)
    dx       = (100 * (plus_di - minus_di).abs() /
                (plus_di + minus_di).replace(0, np.nan))
    adx      = dx.ewm(span=period, adjust=False).mean().iloc[-1]
    return round(float(adx), 2)


def calc_bollinger(series, period=20, std_dev=2):
    sma   = series.rolling(window=period).mean()
    std   = series.rolling(window=period).std()
    upper = (sma + std * std_dev).iloc[-1]
    mid   = sma.iloc[-1]
    lower = (sma - std * std_dev).iloc[-1]
    return round(upper, 2), round(mid, 2), round(lower, 2)


def detect_candle_pattern(df) -> str:
    if len(df) < 2:
        return ""
    o1, h1, l1, c1 = (df["Open"].iloc[-2], df["High"].iloc[-2],
                       df["Low"].iloc[-2],  df["Close"].iloc[-2])
    o2, h2, l2, c2 = (df["Open"].iloc[-1], df["High"].iloc[-1],
                       df["Low"].iloc[-1],  df["Close"].iloc[-1])
    body2         = abs(c2 - o2)
    candle_range2 = h2 - l2 if h2 != l2 else 0.0001
    lower_shadow  = min(o2, c2) - l2
    upper_shadow  = h2 - max(o2, c2)

    if lower_shadow >= 2 * body2 and upper_shadow <= body2 * 0.3 and c1 < o1:
        return "HAMMER"
    if upper_shadow >= 2 * body2 and lower_shadow <= body2 * 0.3 and c1 > o1:
        return "SHOOTING_STAR"
    if c1 < o1 and c2 > o2 and c2 > o1 and o2 < c1:
        return "BULLISH_ENGULFING"
    if c1 > o1 and c2 < o2 and c2 < o1 and o2 > c1:
        return "BEARISH_ENGULFING"
    if body2 <= candle_range2 * 0.1:
        return "DOJI"
    return ""


# ════════════════════════════════════════════════════════════════════════════
# SIGNAL GENERATION
# ════════════════════════════════════════════════════════════════════════════

def generate_signal(df, ltp: float, nifty_trend: str = "NEUTRAL",
                    market_status: str = "MARKET_OPEN") -> dict:

    empty = {
        "signal": "HOLD", "confidence": "LOW",
        "rsi": 0, "macd": 0, "macd_signal": 0,
        "ema9": 0, "ema21": 0, "volume": 0, "avg_volume": 0,
        "vol_ratio": 0, "adx": 0, "bb_upper": 0, "bb_mid": 0,
        "bb_lower": 0, "atr": 0, "stop_loss": 0, "target": 0,
        "risk_reward": "N/A", "candle_pattern": "",
        "notes": "Insufficient data",
    }

    if len(df) < 30:
        return empty

    close  = df["Close"]
    volume = df["Volume"]

    rsi           = calc_rsi(close).iloc[-1]
    macd, macd_sg = calc_macd(close)
    macd_val      = macd.iloc[-1]
    macd_sig_val  = macd_sg.iloc[-1]
    macd_prev     = macd.iloc[-2]
    macd_sig_prev = macd_sg.iloc[-2]
    ema9          = calc_ema(close, 9).iloc[-1]
    ema21         = calc_ema(close, 21).iloc[-1]
    vol_ratio     = calc_volume_ratio(volume).iloc[-1]
    avg_volume    = volume.rolling(20).mean().iloc[-1]
    adx                        = calc_adx(df)
    bb_upper, bb_mid, bb_lower = calc_bollinger(close)
    atr                        = calc_atr(df)
    candle_pattern             = detect_candle_pattern(df)

    score       = 0
    notes_parts = []

    # RSI
    if rsi < RSI_OVERSOLD:
        score += 1
        notes_parts.append(f"RSI oversold({rsi:.1f})")
    elif rsi > RSI_OVERBOUGHT:
        score -= 1
        notes_parts.append(f"RSI overbought({rsi:.1f})")

    # MACD
    macd_crossed_up = (macd_prev < macd_sig_prev) and (macd_val >= macd_sig_val)
    macd_crossed_dn = (macd_prev > macd_sig_prev) and (macd_val <= macd_sig_val)
    if macd_crossed_up:
        score += 1
        notes_parts.append("MACD cross up")
    elif macd_crossed_dn:
        score -= 1
        notes_parts.append("MACD cross dn")
    elif macd_val > macd_sig_val:
        score += 0.5
    else:
        score -= 0.5

    # EMA
    if ema9 > ema21:
        score += 1
        notes_parts.append("EMA uptrend")
    else:
        score -= 1
        notes_parts.append("EMA downtrend")

    # Bollinger Bands
    price = close.iloc[-1]
    if price <= bb_lower:
        score += 1
        notes_parts.append("BB lower(oversold)")
    elif price >= bb_upper:
        score -= 1
        notes_parts.append("BB upper(overbought)")

    # Candlestick
    if candle_pattern in ("HAMMER", "BULLISH_ENGULFING"):
        score += 0.5
        notes_parts.append(f"Candle:{candle_pattern}")
    elif candle_pattern in ("SHOOTING_STAR", "BEARISH_ENGULFING"):
        score -= 0.5
        notes_parts.append(f"Candle:{candle_pattern}")
    elif candle_pattern == "DOJI":
        notes_parts.append("Candle:DOJI(indecision)")

    # Volume spike
    if vol_ratio > VOLUME_SPIKE:
        score = score * 1.5
        notes_parts.append(f"Vol spike x{vol_ratio:.1f}")

    # ADX filter
    if adx < ADX_WEAK:
        notes_parts.append(f"ADX weak({adx:.1f}) - signal suppressed")
        signal     = "HOLD"
        confidence = "LOW"
    else:
        notes_parts.append(f"ADX={adx:.1f}")
        if score >= 1.5:
            signal     = "BUY"
            confidence = "HIGH" if score >= 3 else "MEDIUM"
        elif score <= -1.5:
            signal     = "SELL"
            confidence = "HIGH" if score <= -3 else "MEDIUM"
        else:
            signal     = "HOLD"
            confidence = "LOW"

        # Nifty trend filter
        if nifty_trend == "DOWN" and signal == "BUY":
            confidence = "LOW"
            notes_parts.append("⚠️ BUY vs Nifty DOWN")
        elif nifty_trend == "UP" and signal == "SELL":
            confidence = "LOW"
            notes_parts.append("⚠️ SELL vs Nifty UP")

    # After-hours / weekend — downgrade to LOW confidence
    if market_status in ("AFTER_HOURS", "WEEKEND") and signal != "HOLD":
        confidence = "LOW"
        notes_parts.append(f"⚠️ {market_status} signal")

    # ATR stop-loss & target
    stop_loss = round(ltp - ATR_SL_MULT * atr, 2)
    target    = round(ltp + ATR_TGT_MULT * atr, 2)
    risk      = round(ltp - stop_loss, 2)
    reward    = round(target - ltp, 2)
    rr_ratio  = f"1:{round(reward / risk, 2)}" if risk > 0 else "N/A"

    return {
        "signal":         signal,
        "confidence":     confidence,
        "rsi":            round(rsi, 2),
        "macd":           round(macd_val, 4),
        "macd_signal":    round(macd_sig_val, 4),
        "ema9":           round(ema9, 2),
        "ema21":          round(ema21, 2),
        "volume":         int(volume.iloc[-1]),
        "avg_volume":     int(avg_volume),
        "vol_ratio":      round(vol_ratio, 2),
        "adx":            adx,
        "bb_upper":       bb_upper,
        "bb_mid":         bb_mid,
        "bb_lower":       bb_lower,
        "atr":            round(atr, 2),
        "stop_loss":      stop_loss,
        "target":         target,
        "risk_reward":    rr_ratio,
        "candle_pattern": candle_pattern,
        "notes":          " | ".join(notes_parts) if notes_parts else "No strong signal",
    }


# ════════════════════════════════════════════════════════════════════════════
# MAIN AGENT — runs 24/7, no market hours restriction
# ════════════════════════════════════════════════════════════════════════════

def run_agent():
    print(f"\n{'='*65}")
    print(f"Trading Agent v3 — {datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"{'='*65}")

    market_status = get_market_status()
    print(f"Market Status: {market_status}")

    noisy = is_noisy_time() if market_status == "MARKET_OPEN" else False
    if noisy:
        print("⚠️  Noisy window (open/close 15 min) — signals flagged LOW confidence")

    sheet    = get_sheet()
    now      = datetime.datetime.now(IST)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")

    print("\nFetching Nifty 50 market trend...")
    nifty_trend = fetch_nifty_trend()

    rows_to_append = []

    for symbol in STOCKS:
        print(f"\nProcessing {symbol}...")
        clean_symbol = symbol.replace(".NS", "")
        sector       = SECTOR_MAP.get(clean_symbol, "OTHER")

        df = fetch_historical(symbol)
        if df.empty:
            print(f"  Skipping {symbol} — no data")
            continue

        ltp = fetch_upstox_ltp(symbol)
        if ltp is None:
            ltp = round(float(df["Close"].iloc[-1]), 2)
            print(f"  Using last close as LTP: ₹{ltp}")
        else:
            print(f"  Upstox LTP: ₹{ltp}")

        result = generate_signal(df, ltp, nifty_trend, market_status)

        # Noisy window override
        if noisy and result["signal"] != "HOLD":
            result["confidence"] = "LOW"
            result["notes"] += " | Noisy window"

        # Cooldown check
        last_signal, last_dt_str = get_recent_signals(sheet, clean_symbol)
        if is_cooldown_active(last_signal, last_dt_str, result["signal"]):
            print(f"  ⏸ Cooldown active — same {result['signal']} within {COOLDOWN_HOURS}h, skipping")
            continue

        row = [
            date_str, time_str, clean_symbol, sector, ltp,
            result["signal"], result["rsi"], result["macd"],
            result["macd_signal"], result["ema9"], result["ema21"],
            result["volume"], result["avg_volume"], result["vol_ratio"],
            result["confidence"], result["notes"],
            result["adx"], result["bb_upper"], result["bb_mid"], result["bb_lower"],
            result["atr"], result["stop_loss"], result["target"], result["risk_reward"],
            nifty_trend, result["candle_pattern"], market_status,
        ]
        rows_to_append.append(row)

        icon = ("🟢 BUY" if result["signal"] == "BUY"
                else ("🔴 SELL" if result["signal"] == "SELL" else "⚪ HOLD"))
        print(
            f"  {icon} [{result['confidence']}] [{market_status}] "
            f"SL=₹{result['stop_loss']} TGT=₹{result['target']} "
            f"RR={result['risk_reward']} | {result['notes']}"
        )
        time.sleep(1)

    if rows_to_append:
        sheet.append_rows(rows_to_append, value_input_option="USER_ENTERED")
        print(f"\n✅ Wrote {len(rows_to_append)} rows to Google Sheets")
    else:
        print("\n⚠️  No rows written — all skipped or failed")

    print(f"\nRun complete at {datetime.datetime.now(IST).strftime('%H:%M:%S IST')}\n")


if __name__ == "__main__":
    run_agent()
