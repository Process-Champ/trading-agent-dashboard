"""
Filename: trading_agent.py
Description: Production-Grade Multitasking Momentum Swing Agent
             with corrected Wilder's Smoothing indicators, self-healing 
             JSON credential parsers, and automated Google Sheets logging for Sheet2.
"""

import datetime
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import gspread
import numpy as np
import pandas as pd
import pytz
import requests
import yfinance as yf
from google.oauth2.service_account import Credentials

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

IST = pytz.timezone("Asia/Kolkata")

# ==============================================================================
# CONFIG & CONFIGURABLE SETTINGS
# ==============================================================================
GOOGLE_SHEET_NAME = 'Trading data'
WORKSHEET_NAME = "Sheet2"

ADX_MIN = 22
BREAKOUT_LOOKBACK = 20
VOLUME_SPIKE = 1.5
ATR_SL_MULT = 2.5  # Strategic upgrade to stay clear of market noise

# ==============================================================================
# 1. CORRECTED MATHEMATICAL INDICATORS (Wilder's Smoothing & True DMI Logic)
# ==============================================================================


def wilders_smoothing(series, period):
    """Applies true Wilder's smoothing technique using an EWM variant."""
    return series.ewm(alpha=1 / period, adjust=False).mean()


def calculate_indicators(df, period=14):
    """Calculates mathematically accurate ATR, ADX, RSI, and EMAs."""
    df = df.copy()

    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    # --- True Range & ATR ---
    tr = pd.concat(
        [high - low, abs(high - close.shift()), abs(low - close.shift())],
        axis=1,
    ).max(axis=1)
    df["ATR"] = wilders_smoothing(tr, period)

    # --- Directional Movement (DMI / ADX) ---
    up_move = high.diff()
    down_move = low.diff() * -1

    # Strict DMI logic: Greater move wins, negative moves wiped out
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # Smooth the directional movements
    smoothed_plus_dm = wilders_smoothing(pd.Series(plus_dm, index=df.index), period)
    smoothed_minus_dm = wilders_smoothing(
        pd.Series(minus_dm, index=df.index), period
    )

    # Calculate True DI lines
    plus_di = 100 * (smoothed_plus_dm / df["ATR"])
    minus_di = 100 * (smoothed_minus_dm / df["ATR"])

    # Calculate ADX
    dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di + 1e-8))
    df["ADX"] = wilders_smoothing(dx, period)

    # --- RSI ---
    delta = close.diff()
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    avg_gain = wilders_smoothing(pd.Series(gain, index=df.index), period)
    avg_loss = wilders_smoothing(pd.Series(loss, index=df.index), period)

    rs = avg_gain / (avg_loss + 1e-8)
    df["RSI"] = 100 - (100 / (1 + rs))

    # --- Trend Filters ---
    df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["Close"].ewm(span=21, adjust=False).mean()

    return df


# ==============================================================================
# 2. DYNAMIC UNIVERSE INGESTION (Stable Repository Mirror Source)
# ==============================================================================


def fetch_nifty500_tickers():
    """Fetches the Nifty 500 list securely from a stable open mirror repository."""
    url = "https://raw.githubusercontent.com/kprohith/nse-stock-analysis/master/ind_nifty500list.csv"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        lines = response.text.split("\n")
        tickers = []

        for line in lines:
            line = line.strip()
            if not line or "Symbol" in line or "Company Name" in line:
                continue

            columns = line.split(",")
            if len(columns) > 2:
                symbol = columns[2].strip()

                if symbol and not symbol.startswith('"') and len(symbol) < 12:
                    if symbol == "TATAMOTORS":
                        symbol = "TATAMOTR"
                    tickers.append(f"{symbol}.NS")

        if len(tickers) > 400:
            logging.info(
                f"Successfully pulled {len(tickers)} live Nifty 500 tickers from repository database mirror."
            )
            return tickers

    except Exception as e:
        logging.error(f"Primary mirror request failed: {e}")

    logging.info("Deploying high-momentum baseline fallback universe.")
    return [
        "RELIANCE.NS",
        "TCS.NS",
        "INFY.NS",
        "HDFCBANK.NS",
        "TATAMOTR.NS",
        "ICICIBANK.NS",
        "SBIN.NS",
        "BHARTIARTL.NS",
        "ITC.NS",
        "ADANIENT.NS",
        "SUNPHARMA.NS",
        "AXISBANK.NS",
        "TITAN.NS",
        "MARUTI.NS",
        "HAL.NS",
        "BEL.NS",
        "NTPC.NS",
        "POWERGRID.NS",
        "TATASTEEL.NS",
        "COALINDIA.NS",
    ]


# ==============================================================================
# 3. CONCURRENT / MULTITASKING PIPELINE
# ==============================================================================


def process_single_stock(ticker):
    """Worker thread target: Fetches data, runs filters, returns clean data if qualified."""
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period="1y", interval="1d")

        if len(df) < 130:
            return None

        current_price = df["Close"].iloc[-1]
        avg_volume_20d = df["Volume"].rolling(20).mean().iloc[-1]

        # Liquidity Filter: Eliminate penny or highly illiquid assets
        if current_price < 50 or avg_volume_20d < 300000:
            return None

        # Calculate 6-Month Velocity Momentum Score
        momentum_score = (
            (df["Close"].iloc[-1] - df["Close"].iloc[-126])
            / df["Close"].iloc[-126]
        ) * 100

        return {
            "ticker": ticker,
            "df": df,
            "current_price": current_price,
            "momentum_score": momentum_score,
            "avg_volume": avg_volume_20d,
        }

    except Exception:
        return None


def run_multitasking_scanner(max_workers=40):
    """Coordinates parallel threads to scan and screen the market fast."""
    tickers = fetch_nifty500_tickers()
    screened_pool = []

    logging.info(
        f"Initiating multi-threaded analysis across {len(tickers)} assets..."
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_single_stock, t): t for t in tickers
        }

        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                screened_pool.append(result)

    pool_df = pd.DataFrame(screened_pool)
    if pool_df.empty:
        logging.warning("No liquid stocks survived initial screening.")
        return pd.DataFrame()

    pool_df = pool_df.sort_values(by="momentum_score", ascending=False)
    logging.info(
        f"Completed screening. Found {len(pool_df)} liquid candidates. Isolating top 50 momentum leaders."
    )

    return pool_df.head(50)


# ==============================================================================
# 4. SIGNAL GENERATION ENGINE
# ==============================================================================


def evaluate_execution_signals(top_momentum_pool):
    """Processes technical execution layers strictly on momentum leaders."""
    actionable_signals = []

    for _, stock_data in top_momentum_pool.iterrows():
        ticker = stock_data["ticker"]
        df = calculate_indicators(stock_data["df"])

        close = df["Close"]
        high = df["High"]
        vol = df["Volume"]

        cp = round(close.iloc[-1], 2)
        atr_val = df["ATR"].iloc[-1]
        adx_val = df["ADX"].iloc[-1]
        rsi_val = df["RSI"].iloc[-1]

        # Strategic Filters
        is_bullish_trend = df["EMA9"].iloc[-1] > df["EMA21"].iloc[-1]

        recent_high_20d = high.rolling(BREAKOUT_LOOKBACK).max().iloc[-2]
        is_breakout = cp > recent_high_20d

        avg_vol_20d = vol.rolling(20).mean().iloc[-1]
        is_volume_confirmed = vol.iloc[-1] >= (avg_vol_20d * VOLUME_SPIKE)

        is_trend_strong = adx_val >= ADX_MIN

        # Strict Intersection Rule
        if (
            is_bullish_trend
            and is_breakout
            and is_volume_confirmed
            and is_trend_strong
        ):
            stop_loss = round(cp - (ATR_SL_MULT * atr_val), 2)
            take_profit = round(cp + (2.0 * (cp - stop_loss)), 2)

            actionable_signals.append({
                "Ticker": ticker,
                "Entry Price": cp,
                "6M Momentum %": round(stock_data["momentum_score"], 2),
                "ADX Strength": round(adx_val, 2),
                "RSI": round(rsi_val, 2),
                "Stop Loss (2.5x ATR)": stop_loss,
                "Target (1:2 R:R)": take_profit,
            })

    return pd.DataFrame(actionable_signals)


# ==============================================================================
# 5. GOOGLE SHEETS AUTOMATION EXPORT PIPELINE (With Self-Healing JSON Parser)
# ==============================================================================


def export_signals_to_sheets(signals_df):
    """Connects to Google Sheets and appends fresh execution logs to Sheet2."""
    if signals_df.empty:
        logging.info("No actionable breakout signals to log to sheets today.")
        return

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    try:
        # Load raw credentials text from disk
        with open("service_account.json", "r") as f:
            raw_credentials_content = f.read()

        try:
            # Try parsing directly first
            info = json.loads(raw_credentials_content)
        except json.JSONDecodeError:
            # Self-Healing Layer: Fixes invalid single quotes common in manual configuration copies
            logging.warning(
                "Detecting structural syntax errors inside credentials JSON file. Launching auto-repair..."
            )
            sanitized_content = raw_credentials_content.replace("'", '"')
            info = json.loads(sanitized_content)

        # Authenticate using standard dictionary definitions instead of raw text maps
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        gc = gspread.authorize(creds)

        spreadsheet = gc.open(GOOGLE_SHEET_NAME)

        try:
            worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=WORKSHEET_NAME, rows="1000", cols="10"
            )
            logging.info(f"Created clean, fresh worksheet: {WORKSHEET_NAME}")

        current_date = datetime.datetime.now(IST).strftime("%Y-%m-%d")
        payload = []

        if len(worksheet.get_all_values()) == 0:
            headers = ["Date Logged"] + list(signals_df.columns)
            payload.append(headers)

        for _, row in signals_df.iterrows():
            row_data = [current_date] + list(row.values)
            payload.append([str(item) for item in row_data])

        worksheet.append_rows(payload, value_input_option="USER_ENTERED")
        logging.info(
            f"Successfully logged {len(signals_df)} entries into Google Sheet ({WORKSHEET_NAME})."
        )

    except Exception as e:
        logging.error(f"Google Sheets Pipeline Error: {e}")


# ==============================================================================
# MAIN RUNTIME LOOP
# ==============================================================================

if __name__ == "__main__":
    start_time = time.time()

    # Step 1: Fire off the multi-threaded network workers
    momentum_leaderboard = run_multitasking_scanner(max_workers=40)

    if not momentum_leaderboard.empty:
        # Step 2: Extract technical breakout intersections
        final_picks = evaluate_execution_signals(momentum_leaderboard)

        # Step 3: Automatically dump entries into Google Sheet2
        export_signals_to_sheets(final_picks)

        print("\n" + "=" * 80)
        print(
            f" SYSTEM MONITORING REPORT - {datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M')} IST"
        )
        print("=" * 80)
        if not final_picks.empty:
            print(final_picks.to_string(index=False))
        else:
            print(
                "Execution complete. Top relative strength assets are currently in consolidations.\nNo structural breakouts confirmed today."
            )
        print("=" * 80)
    else:
        logging.error("Halting: Initial screening returned zero viable rows.")

    print(f"\nPipeline finished execution in {round(time.time() - start_time, 2)} seconds.")
