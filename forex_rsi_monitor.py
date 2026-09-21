# -*- coding: utf-8 -*-
"""
Forex RSI Monitor (GitHub Actions + yfinance 版本)
====================================================
每次被 GitHub Actions 触发时运行一次（不是常驻进程）：
  1. 从 state.json 读取上次的提醒状态（防止重复提醒）
  2. 用 yfinance 批量拉取 28 个货币对的 M15 数据
  3. 计算 RSI(14)，触及 74/26 时推送 Discord
  4. 把最新状态写回 state.json，交给 workflow 去 git commit

注意：
  - 数据来自 Yahoo Finance，不是 Tickmill 原盘，可能有细微价差
  - 周末/节假日外汇休市时会自动跳过（通过判断数据新鲜度）
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests
import yfinance as yf

# ========================= 配置区 =========================

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# Yahoo Finance 的外汇代码格式是 "XXXYYY=X"
RAW_SYMBOLS = [
    "GBPUSD", "EURUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY",
    "GBPJPY", "EURJPY", "AUDCHF", "CHFJPY", "GBPAUD", "EURAUD", "AUDJPY",
    "NZDJPY", "GBPCAD", "EURCAD", "AUDNZD", "NZDCHF", "GBPCHF", "EURCHF",
    "CADCHF", "GBPNZD", "EURNZD", "NZDCAD", "EURGBP", "AUDCAD", "CADJPY",
]
YF_TICKERS = {s: f"{s}=X" for s in RAW_SYMBOLS}

RSI_PERIOD = 14
RSI_OVERBOUGHT = 74.0
RSI_OVERSOLD = 26.0
INTERVAL = "15m"
PERIOD = "5d"  # 拉取近5天的15分钟K线，足够覆盖RSI(14)需要的数据量

# 数据新鲜度阈值（分钟）：超过这个时间没有新数据，认为市场休市/数据源异常，跳过该品种
STALE_MINUTES = 40

STATE_FILE = "state.json"
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("forex_rsi_monitor")


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("state.json 读取失败，使用空状态: %s", e)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def send_discord_alert(symbol, rsi_value, level, price):
    if not DISCORD_WEBHOOK_URL:
        log.error("未配置 DISCORD_WEBHOOK_URL，无法推送。请在 GitHub Secrets 里设置。")
        return

    if level == "high":
        title = f"🔴 {symbol} RSI 超买"
        color = 15158332
        desc = f"RSI({RSI_PERIOD}) = **{rsi_value:.2f}**，已触及/突破 {RSI_OVERBOUGHT}"
    else:
        title = f"🟢 {symbol} RSI 超卖"
        color = 3066993
        desc = f"RSI({RSI_PERIOD}) = **{rsi_value:.2f}**，已触及/跌破 {RSI_OVERSOLD}"

    payload = {
        "embeds": [
            {
                "title": title,
                "description": desc,
                "color": color,
                "fields": [
                    {"name": "周期", "value": "M15", "inline": True},
                    {"name": "当前价", "value": f"{price:.5f}", "inline": True},
                    {"name": "数据源", "value": "Yahoo Finance", "inline": True},
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code not in (200, 204):
            log.warning("Discord 推送失败 [%s]: %s", resp.status_code, resp.text)
    except Exception as e:
        log.warning("Discord 推送异常: %s", e)


def fetch_batch_with_retry(tickers):
    """批量拉取所有品种的行情，带重试"""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            data = yf.download(
                tickers=" ".join(tickers),
                period=PERIOD,
                interval=INTERVAL,
                group_by="ticker",
                threads=True,
                progress=False,
                auto_adjust=False,
            )
            if data is not None and not data.empty:
                return data
            log.warning("第 %d 次拉取返回空数据，重试...", attempt)
        except Exception as e:
            last_err = e
            log.warning("第 %d 次拉取失败: %s，重试...", attempt, e)
        time.sleep(RETRY_DELAY_SECONDS)
    log.error("多次重试后仍然拉取失败: %s", last_err)
    return None


def main():
    if not DISCORD_WEBHOOK_URL:
        log.error("环境变量 DISCORD_WEBHOOK_URL 未设置，请检查 GitHub Secrets 配置")
        sys.exit(1)

    state = load_state()
    tickers = list(YF_TICKERS.values())

    log.info("开始拉取 %d 个品种的行情...", len(tickers))
    data = fetch_batch_with_retry(tickers)
    if data is None:
        log.error("本次拉取彻底失败，跳过本轮检测（不改变已有状态）")
        return

    now_utc = datetime.now(timezone.utc)
    checked, skipped, alerted = 0, 0, 0

    for symbol, yf_ticker in YF_TICKERS.items():
        try:
            # yfinance 多品种下载时，单个 ticker 的列是一个二级列 (ticker, field)
            if yf_ticker not in data.columns.get_level_values(0):
                log.warning("%s: 返回数据中没有这个品种，跳过", symbol)
                skipped += 1
                continue

            sub = data[yf_ticker].dropna(subset=["Close"])
            if sub.empty:
                log.warning("%s: 没有可用的收盘价数据，跳过", symbol)
                skipped += 1
                continue

            last_ts = sub.index[-1]
            if last_ts.tzinfo is None:
                last_ts = last_ts.tz_localize("UTC")
            else:
                last_ts = last_ts.tz_convert("UTC")

            age_minutes = (now_utc - last_ts.to_pydatetime()).total_seconds() / 60.0
            if age_minutes > STALE_MINUTES:
                log.info("%s: 最新数据已经是 %.0f 分钟前，判断为休市/数据延迟，跳过", symbol, age_minutes)
                skipped += 1
                continue

            closes = sub["Close"].tolist()
            rsi_value = compute_rsi(closes, RSI_PERIOD)
            if rsi_value is None:
                log.warning("%s: 数据点不足以计算RSI(%d)，跳过", symbol, RSI_PERIOD)
                skipped += 1
                continue

            checked += 1
            current_price = closes[-1]
            prev_state = state.get(symbol)

            if rsi_value >= RSI_OVERBOUGHT:
                if prev_state != "high":
                    log.info("%s RSI=%.2f -> 超买提醒", symbol, rsi_value)
                    send_discord_alert(symbol, rsi_value, "high", current_price)
                    alerted += 1
                state[symbol] = "high"
            elif rsi_value <= RSI_OVERSOLD:
                if prev_state != "low":
                    log.info("%s RSI=%.2f -> 超卖提醒", symbol, rsi_value)
                    send_discord_alert(symbol, rsi_value, "low", current_price)
                    alerted += 1
                state[symbol] = "low"
            else:
                if prev_state is not None:
                    log.info("%s RSI=%.2f 回归中性区间，解除锁定", symbol, rsi_value)
                state[symbol] = None

        except Exception as e:
            log.exception("处理 %s 时出错: %s", symbol, e)
            skipped += 1

    save_state(state)
    log.info("本轮完成：检测 %d 个，跳过 %d 个，触发提醒 %d 次", checked, skipped, alerted)


if __name__ == "__main__":
    main()
