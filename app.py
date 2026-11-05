import datetime as dt

import pandas as pd
import streamlit as st
import yfinance as yf

st.set_page_config(page_title="投信 模擬トレード", layout="wide")
st.title("投資信託 模擬トレード")

# 名前: (ティッカー, 円換算が必要か)
# オルカンは連動対象に近いACWI(全世界株ETF)で代用
ASSETS = {
    "オルカン(ACWI代用)": ("ACWI", True),
    "S&P500": ("^GSPC", True),
    "TOPIX": ("^TOPX", False),
    "日経平均": ("^N225", False),
}


@st.cache_data(ttl=3600)
def get_close(ticker: str, start: dt.date) -> pd.Series:
    df = yf.download(ticker, start=start, progress=False, auto_adjust=True)
    if df.empty:
        return pd.Series(dtype=float)
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close.dropna()


# ---- 入力 ----
with st.sidebar:
    st.header("入力")
    amount = st.number_input("購入額(円)", min_value=1000, value=1_000_000, step=10_000)
    start = st.date_input("購入日", value=dt.date.today() - dt.timedelta(days=365),
                          max_value=dt.date.today() - dt.timedelta(days=1))
    use_fx = st.checkbox("為替を考慮(USD→円換算)", value=True)
    run = st.button("シミュレーション実行", type="primary")

if not run:
    st.info("左のサイドバーで購入額と購入日を入力し、実行してください。")
    st.stop()

# ---- データ取得 ----
usdjpy = get_close("USDJPY=X", start)

series = {}
for name, (ticker, needs_fx) in ASSETS.items():
    s = get_close(ticker, start)
    if s.empty:
        st.warning(f"{name}: データを取得できませんでした")
        continue
    if needs_fx and use_fx and not usdjpy.empty:
        fx = usdjpy.reindex(s.index).ffill().bfill()
        s = s * fx
    series[name] = s

if not series:
    st.error("データを取得できませんでした。ネット接続を確認してください。")
    st.stop()

df = pd.DataFrame(series).sort_index().ffill().dropna()

# ---- 資産推移(購入日の価格で購入額分を買ったとする) ----
value = df / df.iloc[0] * amount

# ---- 結果 ----
st.subheader(f"{df.index[0]:%Y-%m-%d} に {amount:,.0f} 円を投資した場合")
cols = st.columns(len(value.columns))
for col, name in zip(cols, value.columns):
    now = value[name].iloc[-1]
    profit = now - amount
    col.metric(name, f"{now:,.0f} 円", f"{profit:+,.0f} 円 ({profit / amount:+.2%})")

st.subheader("資産推移")
st.line_chart(value)

result = pd.DataFrame({
    "現在評価額(円)": value.iloc[-1].round(0),
    "損益(円)": (value.iloc[-1] - amount).round(0),
    "騰落率": (value.iloc[-1] / amount - 1),
    "増えている?": ["増えている" if v > amount else "減っている" for v in value.iloc[-1]],
})
st.dataframe(result.style.format({"現在評価額(円)": "{:,.0f}", "損益(円)": "{:+,.0f}", "騰落率": "{:+.2%}"}))

st.caption("※ 手数料・信託報酬・税金は考慮していません。オルカンはACWIで代用した参考値です。")
