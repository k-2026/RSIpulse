"""
毎営業日14:40頃にGitHub Actionsから実行されるスクリプト。
1. config.json を読む（監視銘柄・閾値）
2. yfinance で各銘柄の株価を取得
3. RSIと5MAを計算し、買い/売りシグナルを判定
4. status.json に結果を書き出す
5. 条件を満たした銘柄があれば、このアプリ自身のWeb Push通知でiPhoneに知らせる

必要なライブラリ: yfinance, pandas, pywebpush (requirements.txt参照)
"""

from __future__ import annotations

import json
import os
import datetime
import zoneinfo
import pandas as pd
import yfinance as yf
from pywebpush import webpush, WebPushException

JST = zoneinfo.ZoneInfo("Asia/Tokyo")

CONFIG_PATH = "config.json"
STATUS_PATH = "status.json"
SUBSCRIPTION_PATH = "subscription.json"


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def compute_rsi(closes: pd.Series, period: int) -> pd.Series:
    """Wilder方式のRSIを計算する"""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def fetch_stock_data(code: str):
    """過去の日足終値と現在値(直近値)を取得する"""
    ticker = yf.Ticker(f"{code}.T")

    # 過去の確定した日足終値（1年分のバックテスト+ウォームアップ期間を確保するため15ヶ月分取得）
    hist = ticker.history(period="15mo", interval="1d")
    if hist.empty:
        raise RuntimeError(f"{code}: 株価データを取得できませんでした")

    closes = hist["Close"].dropna()

    # 現在値（14:40時点の直近取引値。取れない場合は最後の終値で代用）
    try:
        current_price = float(ticker.fast_info["last_price"])
    except Exception:
        current_price = float(closes.iloc[-1])

    return closes, current_price, ticker


def fetch_earnings_date(ticker) -> str | None:
    """次回決算発表予定日を取得する。取得できなければNoneを返す（日本株はyfinance上でデータが無いことも多い）"""
    try:
        cal = ticker.calendar
        dates = None
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date")
        elif cal is not None and "Earnings Date" in getattr(cal, "index", []):
            dates = cal.loc["Earnings Date"].tolist()
        if dates:
            d = dates[0]
            if hasattr(d, "isoformat"):
                return d.isoformat()[:10]
            return str(d)[:10]
    except Exception:
        pass
    return None


def evaluate_stock(closes: pd.Series, current_price: float, rsi_period: int, ma_period: int, history_len: int = 30):
    """
    直近の確定終値 closes と 本日時点の現在値 current_price から、
    「本日の終値がこの値になったと仮定した場合」のRSI・5MA・クロス判定を行う。
    あわせて、チャート表示用に直近history_len日分の価格/5MA/RSIの系列も作る。
    """
    # 直近の確定終値だけで見た「前日時点」のRSIと5MA
    prev_rsi_series = compute_rsi(closes, rsi_period)
    prev_rsi = float(prev_rsi_series.iloc[-1])
    prev_ma5 = float(closes.tail(ma_period).mean())
    prev_close = float(closes.iloc[-1])

    # 本日の現在値を「本日の終値見込み」として追加した系列
    today_index = closes.index[-1] + pd.Timedelta(days=1)
    closes_with_today = pd.concat([closes, pd.Series([current_price], index=[today_index])])

    today_rsi_series = compute_rsi(closes_with_today, rsi_period)
    today_rsi = float(today_rsi_series.iloc[-1])
    ma5_series = closes_with_today.rolling(ma_period, min_periods=1).mean()
    today_ma5 = float(ma5_series.iloc[-1])

    # 5MAを「終値(見込み)」で上抜け／下抜けしそうかどうか
    #   上抜け: 前日終値が前日5MA以下 → 本日見込み終値が本日5MA超え
    #   下抜け: 前日終値が前日5MA以上 → 本日見込み終値が本日5MA未満
    cross_up = (prev_close <= prev_ma5) and (current_price > today_ma5)
    cross_down = (prev_close >= prev_ma5) and (current_price < today_ma5)

    # チャート用の直近系列（画面に小さく表示する用なので丸めて軽量化）
    price_history = [round(v, 2) for v in closes_with_today.tail(history_len).tolist()]
    ma5_history = [round(v, 2) for v in ma5_series.tail(history_len).tolist()]
    rsi_history = [
        (round(v, 2) if pd.notna(v) else None)
        for v in today_rsi_series.tail(history_len).tolist()
    ]

    return {
        "current_price": round(current_price, 2),
        "rsi": round(today_rsi, 2),
        "ma5": round(today_ma5, 2),
        "prev_rsi": round(prev_rsi, 2),
        "cross_up": cross_up,
        "cross_down": cross_down,
        "price_history": price_history,
        "ma5_history": ma5_history,
        "rsi_history": rsi_history,
    }


def backtest_signals(
    closes: pd.Series,
    rsi_period: int,
    ma_period: int,
    buy_cond: dict,
    sell_cond: dict,
    sell_watch: bool,
    lookback_days: int = 365,
    cooldown_days: int = 5,
):
    """
    過去の確定終値だけを使って、実際に買い/売り条件を満たした日を洗い出す（バックテスト）。

    回数の数え方:
    - 条件を満たしている状態が連続している間はまとめて1回として数える
      （例: RSIが数日連続で閾値以下にとどまっていても1回）
    - 条件のON/OFFを短期間に繰り返すケース（閾値付近の行ったり来たり）で水増しされない
      よう、直前のカウントからcooldown_days営業日以内の再発生はカウントしない
    - 買いの回数は「RSI<=upper かつ 5MA上抜け」のみを対象とする（RSI<=oversold単独は含めない）
    """
    rsi_series = compute_rsi(closes, rsi_period)
    ma5_series = closes.rolling(ma_period, min_periods=1).mean()
    prev_close = closes.shift(1)
    prev_ma5 = ma5_series.shift(1)

    cross_up = (prev_close <= prev_ma5) & (closes > ma5_series)
    cross_down = (prev_close >= prev_ma5) & (closes < ma5_series)

    buy_mask = (rsi_series <= buy_cond["rsi_upper_threshold"]) & cross_up
    oversold_mask = rsi_series <= buy_cond["rsi_oversold_threshold"]
    if sell_watch:
        sell_mask = (rsi_series >= sell_cond["rsi_lower_threshold"]) & cross_down
    else:
        sell_mask = pd.Series(False, index=closes.index)

    def episode_starts(mask: pd.Series):
        """条件がOFF→ONに変わった位置(インデックス番号)だけを、クールダウンを適用しつつ返す"""
        positions = []
        active = False
        last_pos = -10**9
        for i, v in enumerate(mask.tolist()):
            v = bool(v)
            if v and not active:
                if i - last_pos >= cooldown_days:
                    positions.append(i)
                    last_pos = i
                active = True
            elif not v:
                active = False
        return positions

    cutoff = closes.index[-1] - pd.Timedelta(days=lookback_days)

    events = []
    for i in episode_starts(buy_mask):
        dt = closes.index[i]
        if dt >= cutoff:
            events.append({"date": dt.strftime("%Y-%m-%d"), "type": "buy"})
    for i in episode_starts(sell_mask):
        dt = closes.index[i]
        if dt >= cutoff:
            events.append({"date": dt.strftime("%Y-%m-%d"), "type": "sell"})
    for i in episode_starts(oversold_mask):
        dt = closes.index[i]
        if dt >= cutoff:
            events.append({"date": dt.strftime("%Y-%m-%d"), "type": "buy_oversold"})
    events.sort(key=lambda e: e["date"])

    buy_count = sum(1 for e in events if e["type"] == "buy")
    sell_count = sum(1 for e in events if e["type"] == "sell")
    oversold_count = sum(1 for e in events if e["type"] == "buy_oversold")

    recent = closes.index >= cutoff
    chart_1y = {
        "dates": [dt.strftime("%Y-%m-%d") for dt in closes.index[recent]],
        "prices": [round(v, 2) for v in closes[recent].tolist()],
    }

    return events, buy_count, sell_count, oversold_count, chart_1y


def send_webpush(subscription: dict, vapid_private_key: str, vapid_subject: str, title: str, body: str):
    if not subscription:
        print("警告: subscription.json が未登録のため通知はスキップされました（アプリの⚙から通知を有効にしてください）")
        return
    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps({"title": title, "body": body}, ensure_ascii=False),
            vapid_private_key=vapid_private_key,
            vapid_claims={"sub": vapid_subject},
        )
    except WebPushException as e:
        print(f"Web Push通知の送信に失敗しました: {e}")


def main():
    config = load_config()
    rsi_period = config["rsi_period"]
    ma_period = config["ma_period"]
    buy_cond = config["buy_conditions"]
    sell_cond = config["sell_conditions"]

    # 秘密鍵・購読情報を読み込む
    vapid_private_key = os.environ.get("VAPID_PRIVATE_KEY", "")
    vapid_subject = os.environ.get("VAPID_SUBJECT", "mailto:example@example.com")
    subscription = None
    if os.path.exists(SUBSCRIPTION_PATH):
        with open(SUBSCRIPTION_PATH, encoding="utf-8") as f:
            subscription = json.load(f)

    now = datetime.datetime.now(JST)
    results = []
    buy_alerts = []
    sell_alerts = []

    for stock in config["watchlist"]:
        code = stock["code"]
        name = stock["name"]
        try:
            closes, current_price, ticker = fetch_stock_data(code)
            ev = evaluate_stock(closes, current_price, rsi_period, ma_period)
            earnings_date = fetch_earnings_date(ticker)
            events, buy_count, sell_count, oversold_count, chart_1y = backtest_signals(
                closes, rsi_period, ma_period, buy_cond, sell_cond, stock.get("sell_watch", False)
            )
        except Exception as e:
            results.append({"code": code, "name": name, "error": str(e)})
            continue

        buy_signal = (
            ev["rsi"] <= buy_cond["rsi_upper_threshold"] and ev["cross_up"]
        ) or (ev["rsi"] <= buy_cond["rsi_oversold_threshold"])

        sell_signal = False
        if stock.get("sell_watch"):
            sell_signal = (
                ev["rsi"] >= sell_cond["rsi_lower_threshold"] and ev["cross_down"]
            )

        entry = {
            "code": code,
            "name": name,
            **ev,
            "buy_signal": buy_signal,
            "sell_signal": sell_signal,
            "earnings_date": earnings_date,
            "buy_count_1y": buy_count,
            "sell_count_1y": sell_count,
            "buy_oversold_count_1y": oversold_count,
            "signal_history": events,
            "chart_1y": chart_1y,
        }
        results.append(entry)

        if buy_signal:
            buy_alerts.append(f"{name}({code}) RSI={ev['rsi']} 現在値={ev['current_price']}")
        if sell_signal:
            sell_alerts.append(f"{name}({code}) RSI={ev['rsi']} 現在値={ev['current_price']}")

    status = {
        "last_checked": now.isoformat(),
        "results": results,
    }
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

    if buy_alerts:
        send_webpush(
            subscription, vapid_private_key, vapid_subject,
            "📈 買いシグナル",
            "\n".join(buy_alerts),
        )
    if sell_alerts:
        send_webpush(
            subscription, vapid_private_key, vapid_subject,
            "📉 売りシグナル",
            "\n".join(sell_alerts),
        )

    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
