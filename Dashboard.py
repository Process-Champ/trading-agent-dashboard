import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials

st.set_page_config(layout="wide")

st.title("📊 LIVE Trading Dashboard")

# ================= LOAD DATA =================
def load_data():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_name(
        "credentials.json", scope)

    client = gspread.authorize(creds)
    sheet = client.open("TradingData").sheet1

    data = sheet.get_all_records()
    return pd.DataFrame(data)

df = load_data()

if df.empty:
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