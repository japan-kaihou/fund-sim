import datetime as dt
import hashlib
import html
import json
import secrets

import pandas as pd
import streamlit as st
import yfinance as yf

st.set_page_config(page_title="バンク・オブ・ジャパン銀行", page_icon="🏛️", layout="centered")

# 名前: (ティッカー, 円換算が必要か)
ASSETS = {
    "オルカン(ACWI代用)": ("ACWI", True),
    "S&P500": ("^GSPC", True),
    "TOPIX(1306連動ETF)": ("1306.T", False),
    "日経平均": ("^N225", False),
}
BUY, SELL = "買う", "売る"
EPS = 1e-9

st.session_state.setdefault("accounts", {})  # {名前: {salt, pin_hash, trades:[...]}}
st.session_state.setdefault("user", None)
st.session_state.setdefault("dirty", False)  # 未保存の変更があるか

STYLE = """<style>
.bank-header{display:flex;align-items:center;gap:14px;padding:16px 18px;border-radius:14px;
background:linear-gradient(135deg,#0B1F3A 0%,#16365F 100%);border-bottom:3px solid #C9A24B;margin-bottom:14px;}
.bank-crest{font-size:34px;line-height:1;}
.bank-name{color:#F5E6B8;font-size:22px;font-weight:800;letter-spacing:.05em;}
.bank-sub{color:#9FB3CF;font-size:11px;letter-spacing:.18em;margin-top:2px;}
.acct-card{padding:20px 22px;border-radius:18px;color:#fff;margin:6px 0 14px 0;
background:linear-gradient(135deg,#0B1F3A 0%,#1D4A7A 100%);border:1px solid #C9A24B;
box-shadow:0 6px 18px rgba(11,31,58,.35);}
.acct-row{display:flex;justify-content:space-between;color:#F5E6B8;font-size:14px;letter-spacing:.08em;}
.acct-label{margin-top:16px;color:#9FB3CF;font-size:12px;}
.acct-balance{font-size:38px;font-weight:800;letter-spacing:.02em;margin:2px 0 4px 0;}
.acct-pl{font-size:15px;font-weight:700;}
.acct-pl.up{color:#5EE0A0;}
