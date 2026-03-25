import streamlit as st
import pandas as pd

st.set_page_config(layout="wide")

st.title("📊 Trading Dashboard")

try:
    df = pd.read_csv("trades.csv")
except:
    st.warning("No trades yet")
    st.stop()

# ================= PnL =================
pnl = 0
positions = {}

for _, row in df.iterrows():
    symbol = row["symbol"]

    if row["action"] == "BUY":
        positions[symbol] = row["price"]

    elif row["action"] == "SELL" and symbol in positions:
        entry = positions[symbol]
        pnl += (row["price"] - entry) * row["qty"]
        del positions[symbol]

# ================= METRICS =================
col1, col2, col3 = st.columns(3)

col1.metric("💰 Total PnL", f"₹{round(pnl,2)}")
col2.metric("📊 Total Trades", len(df))
col3.metric("📈 Open Positions", len(positions))

# ================= TABLE =================
st.subheader("📋 Trade Log")
st.dataframe(df, use_container_width=True)

# ================= POSITIONS =================
st.subheader("📌 Open Positions")
st.write(positions)