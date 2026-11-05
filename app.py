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
.acct-pl.down{color:#FF8A8A;}
.acct-no{margin-top:16px;color:#C9A24B;font-size:12px;letter-spacing:.12em;}
button[kind="primary"],button[kind="primaryFormSubmit"],button[data-testid="stBaseButton-primary"],
button[data-testid="stBaseButton-primaryFormSubmit"]{
background:linear-gradient(135deg,#C9A24B,#EBCB7B)!important;color:#0B1F3A!important;
border:none!important;font-weight:800!important;font-size:17px!important;padding:10px 18px!important;}
.stDownloadButton button{width:100%;}
</style>"""
st.markdown(STYLE, unsafe_allow_html=True)


def bank_header():
    st.markdown(
        '<div class="bank-header"><div class="bank-crest">🏛️</div><div>'
        '<div class="bank-name">バンク・オブ・ジャパン銀行</div>'
        '<div class="bank-sub">BANK OF JAPAN ・ 子ども投資口座</div>'
        "</div></div>",
        unsafe_allow_html=True,
    )


def hash_pin(pin: str, salt: str) -> str:
    return hashlib.sha256((salt + pin).encode()).hexdigest()


# ================= 価格データ =================
def _close(ticker: str, start: dt.date) -> pd.Series:
    df = yf.download(ticker, start=start, progress=False, auto_adjust=True)
    if df.empty:
        return pd.Series(dtype=float)
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close.dropna()


@st.cache_data(ttl=3600, show_spinner="最新の価格を取得中…")
def get_prices() -> dict:
    start = dt.date.today() - dt.timedelta(days=365 * 5)
    usdjpy = _close("USDJPY=X", start)
    out = {}
    for name, (ticker, needs_fx) in ASSETS.items():
        s = _close(ticker, start)
        if s.empty:
            continue
        if needs_fx and not usdjpy.empty:
            s = s * usdjpy.reindex(s.index).ffill().bfill()
        out[name] = s
    return out


def price_on(s: pd.Series, date):
    """購入日以降で最初の営業日の価格。未来日は最新価格。"""
    pos = min(s.index.searchsorted(pd.Timestamp(date)), len(s) - 1)
    return s.index[pos], float(s.iloc[pos])


# ================= 売買の計算 =================
def replay(trades, prices, until=None):
    """取引を日付順に再生し、(約定リスト, 保有口数, 取得額, 確定損益) を返す。平均取得単価方式。"""
    units, cost = {}, {}
    realized = 0.0
    events = []
    order = sorted(range(len(trades)), key=lambda i: (trades[i]["date"], i))
    for i in order:
        t = trades[i]
        a = t["asset"]
        if a not in prices:
            continue
        if until is not None and t["date"] > until:
            continue
        day, p = price_on(prices[a], t["date"])
        side = t.get("side", BUY)
        u0, c0 = units.get(a, 0.0), cost.get(a, 0.0)
        if side == BUY:
            amt = float(t["amount"])
            du, dc, real = amt / p, amt, 0.0
        else:
            su = u0 if t.get("all") else min(float(t["amount"]) / p, u0)
            if su <= EPS:
                continue
            avg = c0 / u0
            amt = su * p
            du, dc, real = -su, -avg * su, amt - avg * su
        units[a], cost[a] = u0 + du, c0 + dc
        if units[a] <= EPS:
            units[a], cost[a] = 0.0, 0.0
        realized += real
        events.append({"date": day, "asset": a, "side": side, "amount": amt,
                       "price": p, "du": du, "dc": dc})
    return events, units, cost, realized


def timeline(events, prices) -> pd.DataFrame:
    first = min(e["date"] for e in events)
    assets = sorted({e["asset"] for e in events})
    all_days = sorted(set().union(*[set(prices[a].index) for a in assets]))
    idx = pd.DatetimeIndex([d for d in all_days if d >= first])
    value = pd.Series(0.0, index=idx)
    cost = pd.Series(0.0, index=idx)
    for a in assets:
        du = pd.Series(0.0, index=idx)
        dc = pd.Series(0.0, index=idx)
        for e in events:
            if e["asset"] == a:
                du.loc[e["date"]] += e["du"]
                dc.loc[e["date"]] += e["dc"]
        px = prices[a].reindex(idx).ffill()
        value = value.add((du.cumsum() * px).fillna(0.0), fill_value=0.0)
        cost = cost.add(dc.cumsum(), fill_value=0.0)
    return pd.DataFrame({"評価額": value, "元本(保有中)": cost})


# ================= 保存 =================
def mark_saved():
    st.session_state.dirty = False


def save_panel():
    if st.session_state.dirty:
        st.warning("⚠️ まだ保存していません。やめる前に、下の【保存】ボタンを押してね。")
    else:
        st.success("✅ 保存ずみ")
    data = json.dumps({"accounts": st.session_state.accounts}, ensure_ascii=False, indent=2)
    st.download_button("💾 データを保存する", data.encode("utf-8"), file_name="bank.json",
                       mime="application/json", type="primary", on_click=mark_saved)
    st.caption("アプリを閉じると記録は消えます。次に使うときは、この保存ファイルを読み込みます。")


# ================= ログイン画面 =================
def login_screen():
    bank_header()
    accounts = st.session_state.accounts

    with st.expander("📂 保存したデータを読み込む", expanded=not accounts):
        up = st.file_uploader("bank.json を選ぶ", type="json")
        if up is not None:
            key = f"{up.name}-{up.size}"
            if st.session_state.get("loaded") != key:
                try:
                    st.session_state.accounts = json.load(up)["accounts"]
                    st.session_state.loaded = key
                    st.session_state.dirty = False
                    st.rerun()
                except Exception:
                    st.error("読み込めませんでした。bank.json か確認してください。")

    tab_login, tab_new = st.tabs(["🔑 ログイン", "✨ 口座をつくる"])

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
                st.session_state.dirty = True
                st.rerun()

    if accounts:
        st.divider()
        save_panel()
    st.caption("※ これは家族で遊ぶための「ごっこ」の銀行です。本物の銀行・お金ではありません。")


# ================= 口座画面 =================
def account_screen(user: str):
    acc = st.session_state.accounts[user]
    trades = acc["trades"]

    bank_header()
    msg = st.session_state.pop("flash", None)
    if msg:
        st.toast(msg, icon="✅")

    prices = get_prices()
    if not prices:
        get_prices.clear()
        st.error("価格データを取得できませんでした。少し待って、ページを更新してください。")
        if st.button("ログアウト"):
            st.session_state.user = None
            st.rerun()
        return

    events, units, cost, realized = replay(trades, prices)
    held = {a: u for a, u in units.items() if u > EPS}
    value_now = {a: held[a] * float(prices[a].iloc[-1]) for a in held}
    total_value = sum(value_now.values())
    total_cost = sum(cost[a] for a in held)
    unrealized = total_value - total_cost
    total_pl = unrealized + realized
    total_bought = sum(e["amount"] for e in events if e["side"] == BUY)

    # ---- あいさつ・ログアウト ----
    g1, g2 = st.columns([3, 1])
    g1.markdown(f"### {html.escape(user)} 様")
    if g2.button("ログアウト"):
        st.session_state.user = None
        st.rerun()

    # ---- 口座カード ----
    no = int(acc["salt"], 16) % 10_000_000
    cls = "up" if total_pl >= 0 else "down"
    arrow = "▲" if total_pl >= 0 else "▼"
    pct = f"({total_pl / total_bought:+.2%})" if total_bought > 0 else ""
    st.markdown(
        '<div class="acct-card">'
        f'<div class="acct-row"><span>投資口座</span><span>{html.escape(user)} 様</span></div>'
        '<div class="acct-label">保有資産の評価額</div>'
        f'<div class="acct-balance">¥{total_value:,.0f}</div>'
        f'<div class="acct-pl {cls}">{arrow} 合計損益 {total_pl:+,.0f}円 {pct}</div>'
        f'<div class="acct-no">本店営業部 ・ 普通 {no:07d}</div>'
        "</div>",
        unsafe_allow_html=True,
    )

    # ---- 保存ボタン(いつも見える場所) ----
    save_panel()

    tab_home, tab_buy, tab_sell, tab_hist = st.tabs(["🏠 ホーム", "🛒 買う", "💰 売る", "📜 履歴"])
    today = dt.date.today()
    min_day = today - dt.timedelta(days=365 * 4)

    # ---------- ホーム ----------
    with tab_home:
        m1, m2, m3 = st.columns(3)
        m1.metric("元本(保有中)", f"{total_cost:,.0f}円")
        m2.metric("含み損益", f"{unrealized:+,.0f}円")
        m3.metric("確定損益", f"{realized:+,.0f}円")
        st.caption("含み損益 = まだ売っていない分の増減 / 確定損益 = 売ってきまった増減")

        if held:
            st.markdown("**持っている投資信託**")
            rows = [{
                "銘柄": a,
                "評価額": value_now[a],
                "元本": cost[a],
                "損益": value_now[a] - cost[a],
                "損益率": value_now[a] / cost[a] - 1 if cost[a] > 0 else 0.0,
            } for a in held]
            st.dataframe(
                pd.DataFrame(rows).style.format({"評価額": "{:,.0f}円", "元本": "{:,.0f}円",
                                                 "損益": "{:+,.0f}円", "損益率": "{:+.2%}"}),
                hide_index=True)
        elif events:
            st.info("いまは何も持っていません。")
        else:
            st.info("「買う」タブから、はじめての投資をしてみよう。")

        if events:
            st.markdown("**資産のうごき**")
            st.line_chart(timeline(events, prices))
            if held:
                latest = max(prices[a].index[-1] for a in held)
                st.caption(f"価格の基準日: {latest:%Y-%m-%d}")

    # ---------- 買う ----------
    with tab_buy:
        st.markdown("#### 投資信託を買う")
        with st.form("buy"):
            d = st.date_input("買う日", value=today, min_value=min_day, max_value=today)
            a = st.selectbox("銘柄", list(prices))
            amt = st.number_input("金額(円)", min_value=100, value=1000, step=100)
            ok = st.form_submit_button("買う", type="primary")
        st.caption("その日の価格で買います。休日は次の営業日の価格になります。")
        if ok:
            trades.append({"date": d.strftime("%Y-%m-%d"), "asset": a, "side": BUY, "amount": int(amt)})
            st.session_state.dirty = True
            st.session_state.flash = f"{a} を {int(amt):,}円 買いました"
            st.rerun()

    # ---------- 売る ----------
    with tab_sell:
        st.markdown("#### 投資信託を売る")
        if not held:
            st.info("売れるものがありません。先に「買う」タブで買ってください。")
        else:
            asset = st.selectbox("売る銘柄", list(held),
                                 format_func=lambda x: f"{x}(いまの評価額 {value_now[x]:,.0f}円)")
            sd = st.date_input("売る日", value=today, min_value=min_day, max_value=today, key="sell_date")
            sell_all = st.checkbox("ぜんぶ売る")
            samt = st.number_input("売る金額(円)", min_value=100, value=1000, step=100,
                                   disabled=sell_all, key="sell_amt")
            if st.button("売る", type="primary", key="sell_btn"):
                ds = sd.strftime("%Y-%m-%d")
                _, u_at, _, _ = replay(trades, prices, until=ds)
                held_at = u_at.get(asset, 0.0)
                _, p = price_on(prices[asset], ds)
                if held_at <= EPS:
                    st.error("その日は、まだ持っていません。売る日を変えてください。")
                elif not sell_all and samt > held_at * p * 1.005:
                    st.error(f"その日に売れるのは最大 {held_at * p:,.0f}円 です。")
                else:
                    trades.append({"date": ds, "asset": asset, "side": SELL,
                                   "amount": 0 if sell_all else int(samt), "all": bool(sell_all)})
                    st.session_state.dirty = True
                    label = "ぜんぶ" if sell_all else f"{int(samt):,}円分"
                    st.session_state.flash = f"{asset} を {label} 売りました"
                    st.rerun()
            st.caption("その日の価格で売ります。休日は次の営業日の価格になります。")

    # ---------- 履歴 ----------
    with tab_hist:
        if not events:
            st.info("まだ取引がありません。")
        else:
            st.markdown("#### 取引のきろく")
            df = pd.DataFrame([{
                "日付": e["date"].strftime("%Y-%m-%d"),
                "売買": e["side"],
                "銘柄": e["asset"],
                "金額": e["amount"],
                "価格": e["price"],
            } for e in reversed(events)])
            st.dataframe(df.style.format({"金額": "{:,.0f}円", "価格": "{:,.2f}"}), hide_index=True)

            with st.expander("記録を消す(まちがえたとき)"):
                labels = []
                for i, t in enumerate(trades):
                    money = "ぜんぶ" if t.get("all") else f"{int(t['amount']):,}円"
                    labels.append(f"{i + 1}. {t['date']} {t.get('side', BUY)} {t['asset']} {money}")
                sel = st.selectbox("消す記録", labels, index=None, placeholder="えらんでね")
                if st.button("消す") and sel is not None:
                    trades.pop(labels.index(sel))
                    st.session_state.dirty = True
                    st.rerun()

    st.caption("※ 家族で遊ぶ「ごっこ」の銀行です。手数料・信託報酬・税金は含みません。"
               "オルカンはACWI、TOPIXは1306(連動ETF)で代用した参考値です。")


# ================= 起動 =================
user = st.session_state.user
if user and user in st.session_state.accounts:
    account_screen(user)
else:
    st.session_state.user = None
    login_screen()
