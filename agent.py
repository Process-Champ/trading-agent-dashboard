import yfinance as yf
import pandas as pd
import time
from datetime import datetime
from ta.momentum import RSIIndicator
from ta.trend import MACD, SMAIndicator

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ================= CONFIG =================
CONFIG = {
    "total_capital": 25000,
    "max_position_pct": 0.20,
    "cash_buffer_pct": 0.20,
}

WATCHLIST = ["INFY.NS", "RELIANCE.NS", "HDFCBANK.NS"]

# ================= GOOGLE SHEETS =================
def connect_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_name(
        "credentials.json", scope)

    client = gspread.authorize(creds)
    sheet = client.open("TradingData").sheet1
    return sheet

sheet = connect_sheet()

# ================= LOG TRADE =================
def log_trade(symbol, action, price, qty, reason):
    now = datetime.now()

    sheet.append_row([
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M:%S"),
        symbol,
        action,
        price,
        qty,
        "OPEN",
        0,
        reason
    ])

# ================= DATA =================
def fetch_data(symbol):
    try:
        df = yf.Ticker(symbol).history(period="3mo")
        if df.empty:
            return None
        return df
    except:
        return None

# ================= INDICATORS =================
def calculate(df):
    close = df["Close"]

    rsi = RSIIndicator(close).rsi().iloc[-1]
    macd = MACD(close)

    macd_line = macd.macd().iloc[-1]
    signal = macd.macd_signal().iloc[-1]

    sma50 = SMAIndicator(close, 50).sma_indicator().iloc[-1]
    price = close.iloc[-1]

    score = 0

    if rsi < 30:
        score += 2
    elif rsi > 70:
        score -= 2

    if macd_line > signal:
        score += 1
    else:
        score -= 1

    if price > sma50:
        score += 1
    else:
        score -= 1

    return round(price, 2), round(rsi, 2), score

# ================= SIGNAL =================
def signal(score):
    if score >= 2:
        return "BUY"
    elif score <= -2:
        return "SELL"
    return "HOLD"

# ================= QTY =================
def quantity(price):
    max_pos = CONFIG["total_capital"] * CONFIG["max_position_pct"]
    return int(max_pos // price)

# ================= MARKET CHECK =================
def is_market_open():
    now = datetime.now()
    start = now.replace(hour=9, minute=15, second=0)
    end = now.replace(hour=15, minute=30, second=0)
    return start <= now <= end

# ================= MAIN =================
def run():
    capital = CONFIG["total_capital"]

    for symbol in WATCHLIST:
        df = fetch_data(symbol)
        if df is None:
            continue

        price, rsi, score = calculate(df)
        sig = signal(score)

        print(f"{symbol} | {sig}")

        if sig == "HOLD":
            continue

        qty = quantity(price)
        value = qty * price

        if capital - value < CONFIG["total_capital"] * CONFIG["cash_buffer_pct"]:
            continue

        capital -= value

        print(f"EXECUTE {sig} {symbol}")

        log_trade(symbol, sig, price, qty, f"score={score}")

        time.sleep(1)

# ================= LOOP =================
if __name__ == "__main__":
    print("🚀 Live Agent Started")

    while True:
        if is_market_open():
            print("📈 Market Open → Running...")
            run()
        else:
            print("⏳ Market Closed")

        time.sleep(300)