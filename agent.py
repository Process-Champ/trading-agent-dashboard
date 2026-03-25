import os
import time
import csv
import yfinance as yf
import pandas as pd
from datetime import datetime
from ta.momentum import RSIIndicator
from ta.trend import MACD, SMAIndicator

# ================= CONFIG =================
CONFIG = {
    "total_capital": 25000,
    "max_position_pct": 0.20,
    "cash_buffer_pct": 0.20,
}

WATCHLIST = ["INFY.NS", "RELIANCE.NS", "HDFCBANK.NS"]

# ================= LOGGING =================
def log_trade(symbol, action, price, qty, reason):
    file_exists = os.path.isfile("trades.csv")

    with open("trades.csv", "a", newline="") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow([
                "date", "time", "symbol", "action",
                "price", "qty", "status", "pnl", "reason"
            ])

        now = datetime.now()

        writer.writerow([
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

# ================= POSITION SIZE =================
def quantity(price):
    max_pos = CONFIG["total_capital"] * CONFIG["max_position_pct"]
    return int(max_pos // price)

# ================= MAIN =================
def run():
    capital = CONFIG["total_capital"]

    print("\n🚀 AGENT RUNNING...\n")

    for symbol in WATCHLIST:
        df = fetch_data(symbol)
        if df is None:
            continue

        price, rsi, score = calculate(df)
        sig = signal(score)

        print(f"{symbol} | Price: {price} | RSI: {rsi} | Score: {score} | {sig}")

        if sig == "HOLD":
            continue

        qty = quantity(price)

        if qty == 0:
            continue

        value = qty * price

        if capital - value < CONFIG["total_capital"] * CONFIG["cash_buffer_pct"]:
            print("⚠️ Cash buffer hit")
            continue

        capital -= value

        print(f"✅ EXECUTE {sig} {qty} @ {price}")

        log_trade(symbol, sig, price, qty, f"score={score}")

        time.sleep(1)

if __name__ == "__main__":
    run()