"""
Filename: trading_agent.py
Description: Production-Grade Multitasking Momentum Swing Agent.
             Reads your working secret directly as a string to eliminate format issues.
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
GOOGLE_SHEET_NAME = "Trading data"
WORKSHEET_NAME = "Sheet2"  # Targets your second sheet precisely

ADX_MIN = 22
BREAKOUT_LOOKBACK = 20
VOLUME_SPIKE = 1.5
ATR_SL_MULT = 2.5 

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

    tr = pd.concat(
        [high - low, abs(high - close.shift()), abs(low - close.shift())],
        axis=1,
    ).max(axis=1)
    df["ATR"] = wilders_smoothing(tr, period)

    up_move = high.diff()
    down_move = low.diff() * -1

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    smoothed_plus_dm = wilders_smoothing(pd.Series(plus_dm, index=df.index), period)
    smoothed_minus_dm = wilders_smoothing(pd.Series(minus_dm, index=df.index), period)

    plus_di = 100 * (smoothed_plus_dm / df["ATR"])
    minus_di = 100 * (smoothed_minus_dm / df["ATR"])

    dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di + 1e-8))
    df["ADX"] = wilders_smoothing(dx, period)

    delta = close.diff()
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    avg_gain = wilders_smoothing(pd.Series(gain, index=df.index), period)
    avg_loss = wilders_smoothing(pd.Series(loss, index=df.index), period)

    rs = avg_gain / (avg_loss + 1e-8)
    df["RSI"] = 100 - (100 / (1 + rs))

    df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["Close"].ewm(span=21, adjust=False).mean()

    return df

# ==============================================================================
# 2. DYNAMIC UNIVERSE INGESTION
# ==============================================================================

def fetch_nifty500_tickers():
    """Fetches the Nifty 500 list securely from a stable open mirror repository."""
    url = "https://raw.githubusercontent.com/kprohith/nse-stock-analysis/master/ind_nifty500list.csv"
    headers = {"User-Agent": "Mozilla/5.0"}

    # Comprehensive up-to-date mapping of legacy mirror tickers to active Yahoo Finance tickers
    ticker_migration_map = {
        "AMARAJABAT": "ARE&M",         # Amara Raja Energy & Mobility
        "ADANITRANS": "ADANIENSOL",    # Adani Transmission became Adani Energy Solutions
        "CADILAHC": "ZYDUSLIFE",       # Cadila Healthcare became Zydus Lifesciences
        "MOTHERSUMI": "MOTHERSON",     # Motherson Sumi became Samvardhana Motherson
        "PVR": "PVRINOX",              # PVR merged with Inox Leisure
        "INOXLEISUR": "PVRINOX",        # Inox side of the merger
        "GMRINFRA": "GMRIFTP",         # GMR Infrastructure restructuring
        "INFRATEL": "INDUSTOWER",      # Bharti Infratel became Indus Towers
        "L&TFH": "LTF",                # L&T Finance Holdings shortened to LTF
        "LTI": "LTIM",                 # LTI and Mindtree merged into LTIMindtree
        "MINDTREE": "LTIM",
        "MINDAIND": "UNOMINDA",        # Minda Industries became Uno Minda
        "SRTRANSFIN": "SHRIRAMFIN",    # Shriram Transport became Shriram Finance
        "SHRIRAMCIT": "SHRIRAMFIN",
        "TATAGLOBAL": "TATACONSUM",    # Tata Global Beverages became Tata Consumer Products
        "TATACOFFEE": "TATACONSUM",    # Tata Coffee merged into Tata Consumer
        "WABCOINDIA": "ZFCOMM",        # Wabco became ZF Commercial Vehicle
        "WELSPUNIND": "WELSPUNLIV",    # Welspun India became Welspun Living
        "PHILIPCARB": "PCBL",          # Phillips Carbon Black became PCBL
        "MAHINDCIE": "CIEINDIA",       # Mahindra CIE became CIE Automotive India
        "STRTECH": "STLTECH",          # Sterlite Technologies updated name
        "ANDHRABANK": "UNIONBANK",     # Merged into Union Bank of India
        "CORPBANK": "UNIONBANK",
        "ALBK": "INDIANB",             # Allahabad Bank merged into Indian Bank
        "ORIENTBANK": "PNB",           # Oriental Bank of Commerce merged into PNB
        "SYNDIBANK": "CANBK",          # Syndicate Bank merged into Canara Bank
        "KALPATPOWR": "KPIL",          # Kalpataru Power became Kalpataru Projects
        "MAGMA": "POONAWALLA",         # Magma Fincorp became Poonawalla Fincorp
        "UJJIVAN": "UJJIVANSFB",       # Map to active Small Finance Bank listing if merged
        "MCDOWELL-N": "MCDOWELL-N",    # Keep clean or fallback
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
                symbol = columns[2].strip().replace('"', '') # Clean up potential quotes
                
                if symbol:
                    # Check if the symbol has changed over time. If yes, map to the active one.
                    if symbol in ticker_migration_map:
                        symbol = ticker_migration_map[symbol]
                    
                    ticker_with_ext = f"{symbol}.NS"
                    if ticker_with_ext not in tickers and len(symbol) < 12:
                        tickers.append(ticker_with_ext)

        if len(tickers) > 400:
            logging.info(f"Successfully pulled {len(tickers)} live Nifty 500 tickers (with auto-corrections applied).")
            return tickers
            
    except Exception as e:
        logging.error(f"Primary mirror request failed: {e}")

    logging.info("Deploying high-momentum baseline fallback universe.")
    return ["RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "SBIN.NS"]

# ==============================================================================
# 3. CONCURRENT / MULTITASKING PIPELINE
# ==============================================================================

def process_single_stock(ticker):
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period="1y", interval="1d")
        if len(df) < 130:
            return None

        current_price = df["Close"].iloc[-1]
        avg_volume_20d = df["Volume"].rolling(20).mean().iloc[-1]

        if current_price < 50 or avg_volume_20d < 300000:
            return None

        momentum_score = ((df["Close"].iloc[-1] - df["Close"].iloc[-126]) / df["Close"].iloc[-126]) * 100
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
    tickers = fetch_nifty500_tickers()
    screened_pool = []
    logging.info(f"Initiating multi-threaded analysis across {len(tickers)} assets...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_single_stock, t): t for t in tickers}
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                screened_pool.append(result)

    pool_df = pd.DataFrame(screened_pool)
    if pool_df.empty:
        return pd.DataFrame()

    pool_df = pool_df.sort_values(by="momentum_score", ascending=False)
    return pool_df.head(50)

# ==============================================================================
# 4. SIGNAL GENERATION ENGINE
# ==============================================================================

def evaluate_execution_signals(top_momentum_pool):
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

        is_bullish_trend = df["EMA9"].iloc[-1] > df["EMA21"].iloc[-1]
        recent_high_20d = high.rolling(BREAKOUT_LOOKBACK).max().iloc[-2]
        is_breakout = cp > recent_high_20d
        avg_vol_20d = vol.rolling(20).mean().iloc[-1]
        is_volume_confirmed = vol.iloc[-1] >= (avg_vol_20d * VOLUME_SPIKE)
        is_trend_strong = adx_val >= ADX_MIN

        if is_bullish_trend and is_breakout and is_volume_confirmed and is_trend_strong:
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
# 5. GOOGLE SHEETS AUTOMATION EXPORT PIPELINE (Direct From Saved File)
# ==============================================================================

def export_signals_to_sheets(signals_df):
    if signals_df.empty:
        logging.info("No actionable breakout signals to log to sheets today.")
        return

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    try:
        # Reads the file created by the GitHub action workflow step
        creds = Credentials.from_service_account_file("service_account.json", scopes=scopes)
        gc = gspread.authorize(creds)
        spreadsheet = gc.open(GOOGLE_SHEET_NAME)

        try:
            worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=WORKSHEET_NAME, rows="1000", cols="10")
            logging.info(f"Created fresh worksheet: {WORKSHEET_NAME}")

        current_date = datetime.datetime.now(IST).strftime("%Y-%m-%d")
        payload = []

        if len(worksheet.get_all_values()) == 0:
            headers = ["Date Logged"] + list(signals_df.columns)
            payload.append(headers)

        for _, row in signals_df.iterrows():
            row_data = [current_date] + list(row.values)
            payload.append([str(item) for item in row_data])

        worksheet.append_rows(payload, value_input_option="USER_ENTERED")
        logging.info(f"Successfully logged {len(signals_df)} entries into Google Sheet ({WORKSHEET_NAME}).")

    except Exception as e:
        logging.error(f"Google Sheets Pipeline Error: {e}")

# ==============================================================================
# MAIN RUNTIME LOOP
# ==============================================================================

if __name__ == "__main__":
    start_time = time.time()
    momentum_leaderboard = run_multitasking_scanner(max_workers=40)

    if not momentum_leaderboard.empty:
        final_picks = evaluate_execution_signals(momentum_leaderboard)
        export_signals_to_sheets(final_picks)

        print("\n" + "=" * 80)
        print(f" SYSTEM MONITORING REPORT - {datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M')} IST")
        print("=" * 80)
        if not final_picks.empty:
            print(final_picks.to_string(index=False))
        else:
            print("Execution complete. No structural breakouts confirmed today.")
        print("=" * 80)
    else:
        logging.error("Halting: Initial screening returned zero viable rows.")

    print(f"\nPipeline finished execution in {round(time.time() - start_time, 2)} seconds.")
