import sqlite3
from pathlib import Path
import streamlit as st
import pandas as pd
import plotly.express as px
st.set_page_config(page_title="Long-Term Trader",layout="wide")
st.title("Long-Term Trader · Paper Only")
st.caption("Research dashboard. Backtests and paper results are not investment advice or a guarantee of returns.")
if not Path("data/trader.db").exists(): st.info("Run a backtest or paper-run first to create the local database."); st.stop()
c=sqlite3.connect("data/trader.db")
snap=pd.read_sql("SELECT * FROM snapshots ORDER BY date",c); fills=pd.read_sql("SELECT * FROM fills ORDER BY id DESC",c); decisions=pd.read_sql("SELECT * FROM decisions ORDER BY id DESC",c)
if not snap.empty:
    last=snap.iloc[-1]; a,b,c1,d=st.columns(4); a.metric("Portfolio value",f"₹{last.value_inr:,.0f}"); b.metric("Cash",f"₹{last.cash_inr:,.0f}"); c1.metric("Realised P/L",f"₹{last.realised_pnl_inr:,.0f}"); d.metric("Dividend income",f"₹{last.dividend_income_inr:,.0f}")
    st.plotly_chart(px.line(snap,x="date",y="value_inr",title="Portfolio value (INR)"),use_container_width=True)
st.subheader("Trade history"); st.dataframe(fills,use_container_width=True)
st.subheader("Recent decisions and explanations"); st.dataframe(decisions,use_container_width=True)
