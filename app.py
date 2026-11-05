import datetime as dt
import hashlib
import json
import secrets

import pandas as pd
import streamlit as st
import yfinance as yf

st.set_page_config(page_title="投信 模擬トレード", layout="centered")

# 名前: (ティッカー, 円換算が必要か)
ASSETS = {
    "オルカン(ACWI代用)": ("ACWI", True),
    "S&P500": ("^GSPC", True),
    "TOPIX(1306連動ETF)": ("1306.T", False),
    "日経平均": ("^N225", False),
}

st.session_state.setdefault("accounts", {})  # {名前: {salt, pin_hash, trades:[{date, asset, amount}]}}
st.session_state.setdefault("user", None)


def hash_pin(pin: str, salt: str) -> str:
    return hashlib.sha256((salt + pin).encode()).hexdigest()


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


def save_button():
    data = json.dumps({"accounts": st.session_state.accounts}, ensure_ascii=False, indent=2)
    st.download_button("💾 データを保存(bank.json)", data.encode("utf-8"),
                       file_name="bank.json", mime="application/json")
    st.caption("アプリを閉じると記録は消えます。使い終わったら必ず保存してください。")


# ================= ログイン画面 =================
def login_screen():
    st.title("投信 模擬トレード")
    accounts = st.session_state.accounts

    with st.expander("保存したデータを読み込む", expanded=not accounts):
        up = st.file_uploader("bank.json を選ぶ", type="json")
        if up is not None:
            key = f"{up.name}-{up.size}"
            if st.session_state.get("loaded") != key:
                try:
                    st.session_state.accounts = json.load(up)["accounts"]
                    st.session_state.loaded = key
                    st.rerun()
                except Exception:
                    st.error("読み込めませんでした。bank.json か確認してください。")

    tab_login, tab_new = st.tabs(["口座にログイン", "口座をつくる"])

    with tab_login:
        if not accounts:
            st.info("まだ口座がありません。「口座をつくる」から作ってください。")
        else:
            with st.form("login"):
                name = st.selectbox("名前", list(accounts))
                pin = st.text_input("暗証番号(4けた)", type="password", max_chars=4)
                ok = st.form_submit_button("口座を開く", type="primary")
            if ok:
                acc = accounts[name]
                if hash_pin(pin, acc["salt"]) == acc["pin_hash"]:
                    st.session_state.user = name
                    st.rerun()
                else:
                    st.error("暗証番号がちがいます")

    with tab_new:
        with st.form("new"):
            name = st.text_input("名前")
            pin = st.text_input("暗証番号(数字4けた)", type="password", max_chars=4)
            ok = st.form_submit_button("口座をつくる", type="primary")
        if ok:
            name = name.strip()
            if not name:
                st.error("名前を入れてください")
            elif name in accounts:
                st.error("その名前はもう使われています")
            elif not (pin.isdigit() and len(pin) == 4):
                st.error("暗証番号は数字4けたです")
            else:
                salt = secrets.token_hex(8)
                accounts[name] = {"salt": salt, "pin_hash": hash_pin(pin, salt), "trades": []}
                st.session_state.user = name
                st.rerun()

    if accounts:
        st.divider()
        save_button()


# ================= 口座画面 =================
def account_screen(user: str):
    acc = st.session_state.accounts[user]

    top1, top2 = st.columns([3, 1])
    top1.title(f"{user}の口座")
    if top2.button("ログアウト"):
        st.session_state.user = None
        st.rerun()

    # ---- 購入の追加 ----
    st.subheader("購入を追加")
    with st.form("add"):
        d = st.date_input("購入日", value=dt.date.today(), max_value=dt.date.today())
        name = st.selectbox("銘柄", list(ASSETS))
        amt = st.number_input("購入額(円)", min_value=100, value=1000, step=100)
        ok = st.form_submit_button("追加", type="primary")
    if ok:
        acc["trades"].append({"date": d.strftime("%Y-%m-%d"), "asset": name, "amount": int(amt)})

    if not acc["trades"]:
        st.info("購入を追加すると、現在の評価額が表示されます。")
        st.divider()
        save_button()
        return

    trades = pd.DataFrame(acc["trades"])
    trades["date"] = pd.to_datetime(trades["date"])
    trades = trades.sort_values("date").reset_index(drop=True)

    # ---- 計算 ----
    start = trades["date"].min().date() - dt.timedelta(days=10)
    usdjpy = get_close("USDJPY=X", start)

    prices = {}
    for nm in trades["asset"].unique():
        ticker, needs_fx = ASSETS[nm]
        s = get_close(ticker, start)
        if s.empty:
            st.warning(f"{nm}: データを取得できませんでした")
            continue
        if needs_fx and not usdjpy.empty:
            s = s * usdjpy.reindex(s.index).ffill().bfill()
        prices[nm] = s

    if not prices:
        st.error("データを取得できませんでした。")
        return

    all_dates = pd.DatetimeIndex(sorted(set().union(*[s.index for s in prices.values()])))

    rows, value_cols, invested_cols = [], [], []
    for _, t in trades.iterrows():
        nm = t["asset"]
        if nm not in prices:
            continue
        s = prices[nm]
        pos = s.index.searchsorted(t["date"])
        if pos >= len(s):
            st.warning(f"{t['date']:%Y-%m-%d} {nm}: 購入日以降の価格がありません")
            continue
        buy_date = s.index[pos]
        buy_price = s.iloc[pos]
        amount = float(t["amount"])
        aligned = s.reindex(all_dates).ffill()
        val = (aligned / buy_price * amount).where(all_dates >= buy_date, 0.0)
        value_cols.append(val)
        invested_cols.append(pd.Series(amount, index=all_dates).where(all_dates >= buy_date, 0.0))
        now_value = val.iloc[-1]
        rows.append({
            "購入日": t["date"].strftime("%Y-%m-%d"),
            "銘柄": nm,
            "購入額": amount,
            "購入時価格": buy_price,
            "現在価格": aligned.iloc[-1],
            "評価額": now_value,
            "損益": now_value - amount,
            "騰落率": now_value / amount - 1,
        })

    if not rows:
        return

    res = pd.DataFrame(rows)
    total_in = res["購入額"].sum()
    total_now = res["評価額"].sum()

    # ---- 結果 ----
    st.subheader("合計")
    c1, c2 = st.columns(2)
    c1.metric("投資額合計", f"{total_in:,.0f} 円")
    c2.metric("現在の評価額", f"{total_now:,.0f} 円",
              f"{total_now - total_in:+,.0f} 円 ({total_now / total_in - 1:+.2%})")
    st.caption(f"価格の基準日: {all_dates[-1]:%Y-%m-%d}")

    st.subheader("銘柄別")
    by = res.groupby("銘柄")[["購入額", "評価額", "損益"]].sum()
    by["騰落率"] = by["評価額"] / by["購入額"] - 1
    st.dataframe(by.style.format({"購入額": "{:,.0f}", "評価額": "{:,.0f}",
                                   "損益": "{:+,.0f}", "騰落率": "{:+.2%}"}),
                 use_container_width=True)

    st.subheader("購入ごと")
    st.dataframe(res.style.format({"購入額": "{:,.0f}", "購入時価格": "{:,.2f}", "現在価格": "{:,.2f}",
                                    "評価額": "{:,.0f}", "損益": "{:+,.0f}", "騰落率": "{:+.2%}"}),
                 use_container_width=True, hide_index=True)

    st.subheader("資産推移")
    first_buy = min(v.ne(0).idxmax() for v in value_cols)
    chart = pd.DataFrame({
        "評価額合計": sum(value_cols),
        "投資額累計": sum(invested_cols),
    }).loc[first_buy:]
    st.line_chart(chart)

    # ---- 記録の削除 ----
    with st.expander("記録を消す"):
        labels = [f"{i + 1}. {r['date']} {r['asset']} {r['amount']:,}円"
                  for i, r in enumerate(acc["trades"])]
        sel = st.selectbox("消す記録", labels, index=None, placeholder="えらんでね")
        if st.button("消す") and sel is not None:
            acc["trades"].pop(labels.index(sel))
            st.rerun()

    st.divider()
    save_button()
    st.caption("※ 手数料・信託報酬・税金は含みません。オルカンはACWI、TOPIXは1306(連動ETF)で代用した参考値です。")


# ================= 起動 =================
user = st.session_state.user
if user and user in st.session_state.accounts:
    account_screen(user)
else:
    st.session_state.user = None
    login_screen()
