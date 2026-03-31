"""
Trading Signal Agent - Nifty 50 Top 10
Runs via GitHub Actions every 15 min during market hours (9:15 AM - 3:30 PM IST, Mon-Fri)
Writes buy/sell/hold signals to Google Sheets
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
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "HINDUNILVR.NS", "ITC.NS", "SBIN.NS", "BAJFINANCE.NS", "KOTAKBANK.NS",
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
}

GOOGLE_SHEET_NAME = "Trading data"
RSI_OVERSOLD   = 35
RSI_OVERBOUGHT = 65
VOLUME_SPIKE   = 1.5


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
        sheet = spreadsheet.add_worksheet(title="Signals", rows=5000, cols=15)
        headers = [
            "Date", "Time", "Symbol", "LTP", "Signal",
            "RSI", "MACD", "MACD_Signal", "EMA9", "EMA21",
            "Volume", "Avg_Volume", "Vol_Ratio", "Confidence", "Notes"
        ]
        sheet.append_row(headers)
    return sheet


def fetch_historical(symbol: str) -> pd.DataFrame:
    """Fetch data using Yahoo Finance direct API - works on GitHub Actions."""
    import requests

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://finance.yahoo.com",
        "Referer": "https://finance.yahoo.com/",
    }

    # Try 15m data first (last 30 days)
    url = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=15m&range=30d&includePrePost=false"
    )
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        ohlcv = result["indicators"]["quote"][0]
        df = pd.DataFrame({
            "Open":   ohlcv["open"],
            "High":   ohlcv["high"],
            "Low":    ohlcv["low"],
            "Close":  ohlcv["close"],
            "Volume": ohlcv["volume"],
        }, index=pd.to_datetime(timestamps, unit="s", utc=True))
        df.dropna(inplace=True)
        print(f"  Fetched {len(df)} rows via Yahoo API")
        return df
    except Exception as e:
        print(f"  Yahoo API (15m) failed: {e}")

    # Fallback: daily data
    url_daily = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range=3mo&includePrePost=false"
    )
    try:
        r = requests.get(url_daily, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        ohlcv = result["indicators"]["quote"][0]
        df = pd.DataFrame({
            "Open":   ohlcv["open"],
            "High":   ohlcv["high"],
            "Low":    ohlcv["low"],
            "Close":  ohlcv["close"],
            "Volume": ohlcv["volume"],
        }, index=pd.to_datetime(timestamps, unit="s", utc=True))
        df.dropna(inplace=True)
        print(f"  Fetched {len(df)} rows via Yahoo daily fallback")
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


def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def calc_volume_ratio(volume, window=20):
    avg_vol = volume.rolling(window=window).mean()
    return volume / avg_vol.replace(0, np.nan)


def generate_signal(df):
    if len(df) < 30:
        return {"signal": "HOLD", "confidence": "LOW", "rsi": 0, "macd": 0,
                "macd_signal": 0, "ema9": 0, "ema21": 0, "volume": 0,
                "avg_volume": 0, "vol_ratio": 0, "notes": "Insufficient data"}

    close = df["Close"]
    volume = df["Volume"]

    rsi = calc_rsi(close).iloc[-1]
    macd, macd_sig = calc_macd(close)
    macd_val      = macd.iloc[-1]
    macd_sig_val  = macd_sig.iloc[-1]
    macd_prev     = macd.iloc[-2]
    macd_sig_prev = macd_sig.iloc[-2]
    ema9       = calc_ema(close, 9).iloc[-1]
    ema21      = calc_ema(close, 21).iloc[-1]
    vol_ratio  = calc_volume_ratio(volume).iloc[-1]
    avg_volume = volume.rolling(20).mean().iloc[-1]

    score = 0
    notes_parts = []

    if rsi < RSI_OVERSOLD:
        score += 1
        notes_parts.append(f"RSI oversold({rsi:.1f})")
    elif rsi > RSI_OVERBOUGHT:
        score -= 1
        notes_parts.append(f"RSI overbought({rsi:.1f})")

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

    if ema9 > ema21:
        score += 1
        notes_parts.append("EMA uptrend")
    else:
        score -= 1
        notes_parts.append("EMA downtrend")

    if vol_ratio > VOLUME_SPIKE:
        score = score * 1.5
        notes_parts.append(f"Vol spike x{vol_ratio:.1f}")

    if score >= 2:
        signal = "BUY"
        confidence = "HIGH" if score >= 3 else "MEDIUM"
    elif score <= -2:
        signal = "SELL"
        confidence = "HIGH" if score <= -3 else "MEDIUM"
    else:
        signal = "HOLD"
        confidence = "LOW"

    return {
        "signal":      signal,
        "confidence":  confidence,
        "rsi":         round(rsi, 2),
        "macd":        round(macd_val, 4),
        "macd_signal": round(macd_sig_val, 4),
        "ema9":        round(ema9, 2),
        "ema21":       round(ema21, 2),
        "volume":      int(volume.iloc[-1]),
        "avg_volume":  int(avg_volume),
        "vol_ratio":   round(vol_ratio, 2),
        "notes":       " | ".join(notes_parts) if notes_parts else "No strong signal",
    }


def is_market_open():
    now = datetime.datetime.now(IST)
    if now.weekday() >= 5:
        return False
    market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close


def run_agent():
    print(f"\n{'='*60}")
    print(f"Trading Agent Run - {datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"{'='*60}")

    if not is_market_open():
        print("Market is closed. Exiting.")
        return

    sheet = get_sheet()
    now = datetime.datetime.now(IST)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")

    rows_to_append = []

    for symbol in STOCKS:
        print(f"\nProcessing {symbol}...")
        df = fetch_historical(symbol)
        if df.empty:
            print(f"  Skipping {symbol} - no data")
            continue

        ltp = fetch_upstox_ltp(symbol)
        if ltp is None:
            ltp = round(float(df["Close"].iloc[-1]), 2)
            print(f"  Using last close as LTP: Rs {ltp}")
        else:
            print(f"  Upstox LTP: Rs {ltp}")

        result = generate_signal(df)
        clean_symbol = symbol.replace(".NS", "")

        row = [
            date_str, time_str, clean_symbol, ltp,
            result["signal"], result["rsi"], result["macd"],
            result["macd_signal"], result["ema9"], result["ema21"],
            result["volume"], result["avg_volume"], result["vol_ratio"],
            result["confidence"], result["notes"],
        ]
        rows_to_append.append(row)

        icon = "BUY **" if result["signal"] == "BUY" else ("SELL **" if result["signal"] == "SELL" else "HOLD")
        print(f"  {icon} [{result['confidence']}] - {result['notes']}")
        time.sleep(1)

    if rows_to_append:
        sheet.append_rows(rows_to_append, value_input_option="USER_ENTERED")
        print(f"\nWrote {len(rows_to_append)} rows to Google Sheets")
    else:
        print("\nNo data written - all fetches failed")

    print(f"\nRun complete at {datetime.datetime.now(IST).strftime('%H:%M:%S IST')}\n")


if __name__ == "__main__":
    run_agent()
