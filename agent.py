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
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials
from upstox_client import ApiClient, Configuration, MarketQuoteApi
import upstox_client

# ─── CONFIG ───────────────────────────────────────────────────────────────────

STOCKS = [
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
]

# NSE symbols for Upstox (instrument keys)
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
IST = pytz.timezone("Asia/Kolkata")

# Signal thresholds
RSI_OVERSOLD   = 35
RSI_OVERBOUGHT = 65
VOLUME_SPIKE   = 1.5   # 1.5x average volume = significant


# ─── GOOGLE SHEETS SETUP ──────────────────────────────────────────────────────

def get_sheet():
    """Authenticate and return the Google Sheet worksheet."""
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    # Credentials loaded from GitHub Secret (JSON string stored in env var)
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise EnvironmentError("GOOGLE_CREDENTIALS_JSON env var not set")

    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

    spreadsheet = client.open(GOOGLE_SHEET_NAME)

    # Use 'Signals' worksheet, create if missing
    try:
        sheet = spreadsheet.worksheet("Signals")
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="Signals", rows=5000, cols=15)
        headers = [
            "Date", "Time", "Symbol", "LTP", "Signal",
            "RSI", "MACD", "MACD_Signal", "EMA9", "EMA21",
            "Volume", "Avg_Volume", "Vol_Ratio",
            "Confidence", "Notes"
        ]
        sheet.append_row(headers)
    return sheet


# ─── DATA FETCHING ────────────────────────────────────────────────────────────

def fetch_upstox_ltp(symbol: str) -> float | None:
    """Fetch live LTP from Upstox sandbox. Returns None on failure."""
    try:
        token = os.environ.get("UPSTOX_ACCESS_TOKEN")
        if not token:
            return None

        config = Configuration()
        config.access_token = token
        # Point to sandbox
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
        print(f"  Upstox LTP fetch failed for {symbol}: {e}")
    return None


def fetch_historical(symbol: str, period: str = "60d", interval: str = "15m") -> pd.DataFrame:
    """Fetch OHLCV data via yfinance (reliable fallback, always works)."""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval)
        df.dropna(inplace=True)
        return df
    except Exception as e:
        print(f"  yfinance fetch failed for {symbol}: {e}")
        return pd.DataFrame()


# ─── INDICATORS ───────────────────────────────────────────────────────────────

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_macd(series: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_volume_ratio(volume: pd.Series, window: int = 20) -> pd.Series:
    avg_vol = volume.rolling(window=window).mean()
    return volume / avg_vol.replace(0, np.nan)


# ─── SIGNAL LOGIC ─────────────────────────────────────────────────────────────

def generate_signal(df: pd.DataFrame) -> dict:
    """
    Multi-indicator signal scoring system.

    Score: each bullish condition adds +1, bearish adds -1
      RSI oversold       → +1 (buy pressure)
      RSI overbought     → -1 (sell pressure)
      MACD crossover up  → +1
      MACD crossover dn  → -1
      EMA9 > EMA21       → +1 (uptrend)
      EMA9 < EMA21       → -1 (downtrend)
      Volume spike       → amplifies score (×1.5)

    Final:
      score >= +2  → BUY
      score <= -2  → SELL
      else         → HOLD
    """
    if len(df) < 30:
        return {"signal": "HOLD", "confidence": "LOW", "notes": "Insufficient data"}

    close = df["Close"]
    volume = df["Volume"]

    rsi = calc_rsi(close).iloc[-1]
    macd, macd_sig = calc_macd(close)
    macd_val = macd.iloc[-1]
    macd_sig_val = macd_sig.iloc[-1]
    macd_prev = macd.iloc[-2]
    macd_sig_prev = macd_sig.iloc[-2]
    ema9  = calc_ema(close, 9).iloc[-1]
    ema21 = calc_ema(close, 21).iloc[-1]
    vol_ratio = calc_volume_ratio(volume).iloc[-1]
    avg_volume = volume.rolling(20).mean().iloc[-1]

    score = 0
    notes_parts = []

    # RSI
    if rsi < RSI_OVERSOLD:
        score += 1
        notes_parts.append(f"RSI oversold({rsi:.1f})")
    elif rsi > RSI_OVERBOUGHT:
        score -= 1
        notes_parts.append(f"RSI overbought({rsi:.1f})")

    # MACD crossover (current bar crossed)
    macd_crossed_up = (macd_prev < macd_sig_prev) and (macd_val >= macd_sig_val)
    macd_crossed_dn = (macd_prev > macd_sig_prev) and (macd_val <= macd_sig_val)
    if macd_crossed_up:
        score += 1
        notes_parts.append("MACD↑cross")
    elif macd_crossed_dn:
        score -= 1
        notes_parts.append("MACD↓cross")
    elif macd_val > macd_sig_val:
        score += 0.5
    elif macd_val < macd_sig_val:
        score -= 0.5

    # EMA trend
    if ema9 > ema21:
        score += 1
        notes_parts.append("EMA uptrend")
    else:
        score -= 1
        notes_parts.append("EMA downtrend")

    # Volume amplifier
    if vol_ratio > VOLUME_SPIKE:
        score = score * 1.5
        notes_parts.append(f"Vol spike×{vol_ratio:.1f}")

    # Final signal
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


# ─── MAIN RUN ─────────────────────────────────────────────────────────────────

def is_market_open() -> bool:
    """Check if NSE market is currently open (9:15 AM – 3:30 PM IST, Mon–Fri)."""
    now = datetime.datetime.now(IST)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close


def run_agent():
    print(f"\n{'='*60}")
    print(f"Trading Agent Run — {datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"{'='*60}")

 if False:  # market check disabled for testing
    print("Market is closed. Exiting.")
    return

    sheet = get_sheet()
    now = datetime.datetime.now(IST)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")

    rows_to_append = []

    for symbol in STOCKS:
        print(f"\nProcessing {symbol}...")

        # Fetch data
        df = fetch_historical(symbol, period="30d", interval="15m")
        if df.empty:
            print(f"  Skipping {symbol} — no data")
            continue

        # Try Upstox for live LTP, fallback to last close
        ltp = fetch_upstox_ltp(symbol)
        if ltp is None:
            ltp = round(float(df["Close"].iloc[-1]), 2)
            print(f"  Using yfinance close as LTP: ₹{ltp}")
        else:
            print(f"  Upstox LTP: ₹{ltp}")

        result = generate_signal(df)
        clean_symbol = symbol.replace(".NS", "")

        row = [
            date_str,
            time_str,
            clean_symbol,
            ltp,
            result["signal"],
            result["rsi"],
            result["macd"],
            result["macd_signal"],
            result["ema9"],
            result["ema21"],
            result["volume"],
            result["avg_volume"],
            result["vol_ratio"],
            result["confidence"],
            result["notes"],
        ]
        rows_to_append.append(row)

        signal_icon = "🟢" if result["signal"] == "BUY" else ("🔴" if result["signal"] == "SELL" else "⚪")
        print(f"  {signal_icon} {result['signal']} [{result['confidence']}] — {result['notes']}")

        time.sleep(0.5)  # Small delay to avoid rate limits

    # Batch write to Google Sheets
    if rows_to_append:
        sheet.append_rows(rows_to_append, value_input_option="USER_ENTERED")
        print(f"\n✅ Wrote {len(rows_to_append)} rows to Google Sheets")
    else:
        print("\n⚠️  No data written — check API connections")

    print(f"\nRun complete at {datetime.datetime.now(IST).strftime('%H:%M:%S IST')}\n")


if __name__ == "__main__":
    run_agent()
