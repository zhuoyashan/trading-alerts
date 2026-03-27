#!/usr/bin/env python3
"""
远程agent入口：拉取价格 → 检查预警 → 推送ntfy
用于 Claude Code 定时触发器（无Mac环境，仅推送手机通知）
"""
import sys
import os

# 确保当前目录在路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 导入核心模块（跳过Mac弹窗，仅ntfy推送）
from macro_watch import (
    fetch_quotes, check_thresholds, evaluate_macro,
    macro_conclusion, SYMBOLS, notify, print_report
)
import urllib.request
import ssl
import json
from datetime import datetime

NTFY_TOPIC = "wuerhouxing-trading-2026"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"


def push_ntfy(title: str, message: str, priority: str = "high") -> bool:
    """直接推送ntfy，不依赖Mac osascript"""
    try:
        safe_title = title.encode("ascii", errors="ignore").decode("ascii").strip() or "Trading Alert"
        req = urllib.request.Request(
            NTFY_URL,
            data=f"{title}\n{message}".encode("utf-8"),
            headers={
                "Title": safe_title,
                "Priority": priority,
                "Tags": "chart_with_upwards_trend",
                "Content-Type": "text/plain; charset=utf-8",
            },
        )
        ctx = ssl._create_unverified_context()
        urllib.request.urlopen(req, timeout=10, context=ctx)
        return True
    except Exception as e:
        print(f"ntfy推送失败: {e}", file=sys.stderr)
        return False


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 拉取实时数据...")
    quotes = fetch_quotes(SYMBOLS)

    if not quotes:
        push_ntfy("Trading Alert - ERROR", "无法获取市场数据，请检查网络", priority="default")
        sys.exit(1)

    alerts = check_thresholds(quotes)
    signals = evaluate_macro(quotes)
    conclusion = macro_conclusion(signals, quotes)

    # 打印报告（远程agent日志）
    print_report(quotes, alerts, signals)

    # 构建摘要推送（每次都发一条摘要）
    crcl = quotes.get("CRCL")
    gold = quotes.get("Gold")
    btc = quotes.get("BTC")

    summary_parts = []
    if crcl:
        summary_parts.append(f"CRCL ${crcl.price:.2f} ({crcl.pct_change:+.1f}%)")
    if gold:
        summary_parts.append(f"Gold ${gold.price:.0f} ({gold.pct_change:+.1f}%)")
    if btc:
        summary_parts.append(f"BTC ${btc.price:,.0f} ({btc.pct_change:+.1f}%)")

    summary = " | ".join(summary_parts)
    macro_short = conclusion.split("—")[0].strip() if "—" in conclusion else conclusion[:30]

    # 每次发摘要（默认优先级，不打扰）
    push_ntfy(
        f"Trading Check {datetime.now().strftime('%H:%M')}",
        f"{summary}\n{macro_short}",
        priority="default"
    )

    # 有触发预警则额外推高优先级通知
    critical_alerts = [a for a in alerts if a.is_critical]
    if critical_alerts:
        for a in critical_alerts:
            push_ntfy(f"ALERT: {a.asset} {a.level_name}", a.message, priority="urgent")

    print(f"推送完成，共 {len(critical_alerts)} 条预警")


if __name__ == "__main__":
    main()
