import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

st.set_page_config(layout="wide")

st.title("📊 LIVE Trading Dashboard")

# ================= LOAD DATA =================
def load_data():
    scope = [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets"
    ]

    # Use Streamlit Secrets (instead of credentials.json)
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=scope
    )

    client = gspread.authorize(creds)
    sheet = client.open("TradingData").sheet1

    data = sheet.get_all_records()
    return pd.DataFrame(data)

# Load data safely
try:
    df = load_data()
except Exception as e:
    st.error(f"Error loading data: {e}")
    st.stop()

if df.empty:
    st.warning("No trades yet")
    st.stop()

# ================= PnL =================
pnl = 0
positions = {}

for _, row in df.iterrows():
    symbol = row["symbol"]

    if row["action"] == "BUY":
        positions[symbol] = {
            "price": row["price"],
            "qty": row["qty"]
        }

    elif row["action"] == "SELL" and symbol in positions:
        entry = positions[symbol]["price"]
        qty = positions[symbol]["qty"]
        pnl += (row["price"] - entry) * qty
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
