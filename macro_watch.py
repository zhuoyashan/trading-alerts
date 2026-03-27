#!/usr/bin/env python3
"""
宏观交易实时预警系统（大师兄框架）

功能：
1. 拉取各资产实时价格和涨跌幅（Stooq API）
2. 检查CRCL / 黄金是否触发黑盒子关键点位
3. 评估宏观信号矩阵（表外进表内 vs 真实危机）
4. Mac桌面通知推送
5. --watch 模式循环监控

用法：
  python macro_watch.py              # 单次检查
  python macro_watch.py --watch 30   # 每30分钟检查一次
  python macro_watch.py --manual '{"Circle":4.5,"Gold":-1.1,"BTC":0.3,...}'
"""

from __future__ import annotations

import argparse
import json
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime


# ── Stooq API ──────────────────────────────────────────────────────────────────

STOOQ_API = "https://stooq.com/q/l/"
NTFY_TOPIC = "wuerhouxing-trading-2026"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

SYMBOLS: dict[str, str] = {
    # 核心持仓
    "CRCL":   "crcl.us",
    "OXY":    "oxy.us",
    "USO":    "uso.us",
    # 宏观锚定
    "Gold":   "gc.f",
    "BTC":    "btcusd",
    "WTI":    "cl.f",
    "Brent":  "bz.f",
    # 黑盒子观察标的
    "TSLA":   "tsla.us",
    "BABA":   "baba.us",
    "QQQ":    "qqq.us",     # 纳斯达克ETF（四支柱之一）
    "USDJPY": "jpyusd",     # 日元（倒数，Stooq格式）
    # A股
    "Moutai": "600519.cn",
    "SSE":    "^sse",
}


# ── 黑盒子关键点位（大师兄原文/原图）─────────────────────────────────────────

CRCL_LEVELS = {
    "止损警告":  (85.0,   "low",  "🔴 跌破止损线 $85，考虑清仓"),
    "强力买入":  (108.0,  "low",  "🟢 触及0.236线 $108，强力买入区"),
    "准备加仓":  (112.0,  "low",  "🟡 进入准备加仓区 ≤$112"),
    "突破确认":  (145.0,  "high", "🔵 突破0.382线 $145，放量则确认"),
    "核心目标":  (203.84, "high", "🎯 触及0.618核心目标 $204"),
}

GOLD_LEVELS = {
    "强入场区":  (3741.0, "low",  "🟢 黄金触及0.236线 $3741，大师兄标注第一波回调目标"),
    "健康回踩":  (3923.0, "low",  "🟢 黄金触及0.382线 $3923，多头健康回踩区"),
    "中期平衡":  (4167.0, "low",  "🟡 黄金触及0.500线 $4167，中期平衡点，大师兄标注入场区"),
    "关键分水岭": (4410.0, "low", "⚠️  黄金跌破0.618线 $4410，主升浪关键分水岭"),
    "强阻力":    (4702.0, "high", "⚠️  黄金触及0.786线 $4702，末升浪强阻力"),
}

# 茅台点位 [原文]
# 买入核心条件：M1/M2剪刀差收窄（需手动确认，程序只监测价格）
MOUTAI_LEVELS = {
    "极低吸筹":  (1200.0, "low",  "🟢 茅台跌至¥1200，历史极低区，但需等M1/M2信号"),
    "低位观察":  (1400.0, "low",  "🟡 茅台触及¥1400，关注M1/M2剪刀差是否收窄"),
    "黑盒子关键": (1650.0, "high", "⚠️  茅台触及¥1650-1660，黑盒子第二期标注重要压力位"),
    "前期压力":  (1900.0, "high", "⚠️  茅台触及¥1900，大师兄标注前期关键压力位"),
    "突破确认":  (2000.0, "high", "🔵 茅台突破¥2000，配合M1/M2收窄则趋势确认"),
}

# 特斯拉黑盒子点位 [原文，黑盒子第二期]
# 原话："茅台一六五零一六六零是个什么点，特斯拉两百二两百五是个什么点"
TSLA_LEVELS = {
    "强力买入":  (225.0, "low",  "🟢 特斯拉触及$225，0.5-0.618线买入区，大师兄标注"),
    "低位支撑":  (269.0, "low",  "🟡 特斯拉触及$269，黑盒子支撑位"),
    "关键压力1": (351.0, "high", "⚠️  特斯拉触及$351，黑盒子阻力位"),
    "关键压力2": (410.0, "high", "⚠️  特斯拉触及$410，黑盒子阻力位"),
    "历史高位":  (488.0, "high", "🎯 特斯拉接近$488历史高位，注意结算"),
}

# 阿里巴巴点位 [原文，黑盒子第二期]
BABA_LEVELS = {
    "强力支撑":  (90.0,  "low",  "🟢 阿里触及$80-90，0.5线支撑区，大师兄标注"),
    "低位区间":  (100.0, "low",  "🟡 阿里低于$100，关注黑盒子支撑"),
}

# 日元点位 [原文，2023.10汇率课]
# 注意：Stooq返回的是JPYUSD（日元/美元），需转换
# 150日元/美元 = 0.00667 JPYUSD
USDJPY_ALERT_LEVELS = {
    "关键压力区": (152.0, "high_jpy", "⚠️  日元触及152，接近157-161爆仓区，注意日元升值信号"),
    "极度危险区": (157.0, "high_jpy", "🔴 日元触及157-161，大师兄标注爆仓区，不可碰"),
}

# BTC关键区间 [原文，黑盒子第二期]
BTC_LEVELS = {
    "极强支撑":  (42000.0, "low",  "🟢 BTC跌至$42000，大师兄标注历史关键支撑区"),
    "区间下沿":  (64000.0, "high", "🔵 BTC触及$64000-66000，区间上沿，注意压力"),
}

# A股上证指数点位 [原文]
# 原话："4250才是真正压力位，4000只是整数关口"
SSE_LEVELS = {
    "极强支撑":  (3500.0, "low",  "🟢 上证触及3500，大师兄标注极强支撑，'太爽了'"),
    "强支撑区":  (3750.0, "low",  "🟢 上证触及3750，大师兄标注强支撑区"),
    "整数关口":  (4000.0, "high", "🟡 上证突破4000，心理关口，非真正压力位"),
    "真正压力":  (4250.0, "high", "⚠️  上证触及4250，大师兄标注真正压力位，注意高位结算"),
    "政策牛目标": (4500.0, "high", "🎯 上证触及4500，政策牛阶段目标，务必结算"),
}


# ── 数据获取 ──────────────────────────────────────────────────────────────────

@dataclass
class Quote:
    price: float
    pct_change: float


def _urlopen(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8")
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLError):
            raise
        ctx = ssl._create_unverified_context()  # noqa: SLF001
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            return r.read().decode("utf-8")


def _parse_quote(csv_text: str) -> Quote | None:
    lines = [ln.strip() for ln in csv_text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    parts = lines[1].split(",")
    if len(parts) < 7:
        return None
    open_s, close_s = parts[3], parts[6]
    if open_s in {"N/D", ""} or close_s in {"N/D", ""}:
        return None
    o, c = float(open_s), float(close_s)
    if o == 0:
        return None
    return Quote(price=c, pct_change=(c - o) / o * 100.0)


def fetch_quotes(symbols: dict[str, str]) -> dict[str, Quote]:
    out: dict[str, Quote] = {}
    for alias, ticker in symbols.items():
        url = f"{STOOQ_API}?s={ticker}&f=sd2t2ohlcv&h&e=csv"
        try:
            txt = _urlopen(url)
            q = _parse_quote(txt)
            if q:
                out[alias] = q
        except Exception:
            pass
    return out


# ── Mac 通知 ──────────────────────────────────────────────────────────────────

def notify(title: str, message: str, priority: str = "high") -> None:
    """同时发送 Mac弹窗 + 手机ntfy推送。"""
    # Mac 弹窗
    script = (
        f'tell app "System Events" to display dialog "{message}" '
        f'buttons {{"OK"}} with title "{title}"'
    )
    try:
        subprocess.Popen(["osascript", "-e", script])  # 非阻塞，不等用户点OK
    except FileNotFoundError:
        pass

    # 手机推送（ntfy）
    try:
        safe_title = title.encode("ascii", errors="ignore").decode("ascii").strip() or "Trading Alert"
        req = urllib.request.Request(
            NTFY_URL,
            data=f"{title}\n{message}".encode("utf-8"),
            headers={
                "Title": safe_title,
                "Priority": priority,
                "Tags": "warning",
                "Content-Type": "text/plain; charset=utf-8",
            },
        )
        ctx = ssl._create_unverified_context()  # noqa: SLF001
        urllib.request.urlopen(req, timeout=5, context=ctx)
    except Exception:
        pass  # 网络失败不影响主程序


# ── 阈值检查 ──────────────────────────────────────────────────────────────────

@dataclass
class Alert:
    asset: str
    level_name: str
    message: str
    is_critical: bool = False


def check_timing() -> list[str]:
    """检查关键时间窗口是否临近（大师兄原文时间节点）。"""
    today = datetime.now().date()
    reminders = []

    events = [
        ("2026-05-01", "CLARITY Act评论期截止（稳定币监管定稿）[原文关联]"),
        ("2026-05-15", "CRCL下次财报预期窗口（约5月），上次EPS $0.56打脸监管恐慌"),
        ("2026-06-30", "大师兄标注2026年6月：18个月窗口关键验证点，'全部行情要走出来'[原文]"),
        ("2026-08-01", "关税通胀压力期开始（8-10月），大师兄：CPI/PPI将被推高[原文]"),
        ("2026-10-31", "关税通胀压力期结束窗口，美联储降息压力最大时点[原文推演]"),
    ]

    for date_str, msg in events:
        from datetime import date
        event_date = date.fromisoformat(date_str)
        days_left = (event_date - today).days
        if 0 <= days_left <= 30:
            reminders.append(f"  📅 {days_left}天后 — {msg}")
        elif days_left < 0 and days_left >= -7:
            reminders.append(f"  📅 已过{-days_left}天 — {msg}")

    return reminders


def check_thresholds(quotes: dict[str, Quote]) -> list[Alert]:
    alerts: list[Alert] = []

    crcl = quotes.get("CRCL")
    if crcl:
        for name, (price, direction, msg) in CRCL_LEVELS.items():
            triggered = crcl.price <= price if direction == "low" else crcl.price >= price
            if triggered:
                critical = name in ("止损警告", "强力买入")
                alerts.append(Alert("CRCL", name, f"CRCL ${crcl.price:.2f} — {msg}", critical))

    gold = quotes.get("Gold")
    if gold:
        for name, (price, direction, msg) in GOLD_LEVELS.items():
            triggered = gold.price <= price if direction == "low" else gold.price >= price
            if triggered:
                critical = name in ("强入场区", "中期平衡", "关键分水岭")
                alerts.append(Alert("Gold", name, f"黄金 ${gold.price:.0f} — {msg}", critical))

    moutai = quotes.get("Moutai")
    if moutai:
        for name, (price, direction, msg) in MOUTAI_LEVELS.items():
            triggered = moutai.price <= price if direction == "low" else moutai.price >= price
            if triggered:
                critical = name in ("突破确认", "低位观察")
                alerts.append(Alert("茅台", name, f"茅台 ¥{moutai.price:.0f} — {msg}", critical))

    sse = quotes.get("SSE")
    if sse:
        for name, (price, direction, msg) in SSE_LEVELS.items():
            triggered = sse.price <= price if direction == "low" else sse.price >= price
            if triggered:
                critical = name in ("真正压力", "极强支撑", "政策牛目标")
                alerts.append(Alert("上证", name, f"上证 {sse.price:.0f}点 — {msg}", critical))

    tsla = quotes.get("TSLA")
    if tsla:
        for name, (price, direction, msg) in TSLA_LEVELS.items():
            triggered = tsla.price <= price if direction == "low" else tsla.price >= price
            if triggered:
                critical = name in ("强力买入",)
                alerts.append(Alert("TSLA", name, f"TSLA ${tsla.price:.2f} — {msg}", critical))

    baba = quotes.get("BABA")
    if baba:
        for name, (price, direction, msg) in BABA_LEVELS.items():
            triggered = baba.price <= price if direction == "low" else baba.price >= price
            if triggered:
                critical = name in ("强力支撑",)
                alerts.append(Alert("BABA", name, f"BABA ${baba.price:.2f} — {msg}", critical))

    btc = quotes.get("BTC")
    if btc:
        for name, (price, direction, msg) in BTC_LEVELS.items():
            triggered = btc.price <= price if direction == "low" else btc.price >= price
            if triggered:
                critical = name in ("极强支撑",)
                alerts.append(Alert("BTC", name, f"BTC ${btc.price:,.0f} — {msg}", critical))

    # 日元：Stooq返回JPYUSD，需换算为USDJPY
    jpy = quotes.get("USDJPY")
    if jpy and jpy.price > 0:
        usdjpy = 1.0 / jpy.price
        for name, (rate, direction, msg) in USDJPY_ALERT_LEVELS.items():
            triggered = usdjpy >= rate  # high_jpy = 日元贬值（数字越大越贬）
            if triggered:
                critical = name in ("极度危险区",)
                alerts.append(Alert("日元", name, f"USDJPY {usdjpy:.1f} — {msg}", critical))

    return alerts


# ── 宏观信号矩阵 ──────────────────────────────────────────────────────────────

@dataclass
class Signal:
    name: str
    passed: bool
    detail: str
    source: str  # [原文] 或 [推演]


def evaluate_macro(quotes: dict[str, Quote]) -> list[Signal]:
    signals: list[Signal] = []

    # Circle 上涨 = 表外进表内确认 [原文]
    c = quotes.get("CRCL")
    if c:
        ok = c.pct_change >= 1.0
        signals.append(Signal(
            "Circle上涨", ok,
            f"CRCL {c.pct_change:+.2f}%  当前 ${c.price:.2f}",
            "[原文] Circle涨=表外进表内确认"
        ))

    # 黄金偏弱 = 假危机/表外进表内窗口 [原文]
    g = quotes.get("Gold")
    if g:
        ok = g.pct_change <= -0.8
        signals.append(Signal(
            "黄金偏弱", ok,
            f"黄金 {g.pct_change:+.2f}%  当前 ${g.price:.0f}",
            "[原文] 黄金跌=假危机/表外进表内"
        ))

    # BTC稳定 [推演]
    b = quotes.get("BTC")
    if b:
        ok = abs(b.pct_change) <= 1.5
        signals.append(Signal(
            "BTC偏稳", ok,
            f"BTC {b.pct_change:+.2f}%  当前 ${b.price:,.0f}",
            "[推演] BTC稳=市场未恐慌"
        ))

    # 油价非线性爆涨 [原文]
    wti = quotes.get("WTI")
    brent = quotes.get("Brent")
    oil_vals = [v for v in (wti, brent) if v]
    if oil_vals:
        ok = all(abs(v.pct_change) <= 3.0 for v in oil_vals)
        detail_parts = []
        if wti:
            detail_parts.append(f"WTI {wti.pct_change:+.2f}% ${wti.price:.1f}")
        if brent:
            detail_parts.append(f"Brent {brent.pct_change:+.2f}% ${brent.price:.1f}")
        signals.append(Signal(
            "油价非线性爆涨", ok,
            "  ".join(detail_parts),
            "[原文] 油价=烟雾弹，爆涨>3%需警惕"
        ))

    return signals


def macro_conclusion(signals: list[Signal], quotes: dict[str, Quote]) -> str:
    passed = {s.name for s in signals if s.passed}
    btc = quotes.get("BTC")

    if "Circle上涨" in passed and "黄金偏弱" in passed:
        return "🟢 【表外进表内】信号共振 — Circle涨+黄金跌，窗口开启"
    if "Circle上涨" not in passed and "黄金偏弱" not in passed:
        g = quotes.get("Gold")
        if g and g.pct_change > 1.5:
            return "🔴 【真实危机】黄金强势上涨，避险情绪主导，非表外进表内时机"
    if btc and btc.price > 84000:
        return "⚠️  BTC突破$84K — 注意：此阈值来源待确认，请结合其他信号判断"
    return "⚪ 【中性/混合】信号未统一，继续观察"


# ── 报告输出 ──────────────────────────────────────────────────────────────────

def print_logic_validation(quotes: dict[str, Quote]) -> None:
    """大师兄逻辑验证：三层传导 + 金油债四支柱 [原文]"""

    # 金油债四支柱框架（2026.03.12新增）[原文]
    print("\n【金油债四支柱验证】[原文：表外进表内的完整支撑体系]")
    print("  四支柱全部稳固 → 黄金承压；任一松动 → 黄金受益")

    qqq = quotes.get("QQQ")
    btc = quotes.get("BTC")
    jpy = quotes.get("USDJPY")
    usdjpy = (1.0 / jpy.price) if (jpy and jpy.price > 0) else None

    print(f"  ① 美元（汇）   ⬜ 需查DXY美元指数（手动）")
    print(f"  ② 美债（债）   ⬜ 需查10年期美债收益率（手动）")
    print(f"  ③ 单极霸权    ⬜ 伊朗战争走向（手动判断）")
    qqq_str = f"QQQ ${qqq.price:.2f} {qqq.pct_change:+.2f}%" if qqq else "N/A"
    qqq_ok = qqq and qqq.pct_change > -1.0
    print(f"  ④ 纳斯达克（股） {'✅' if qqq_ok else '❌'} {qqq_str}")

    # A股三层传导
    print("\n【A股三层传导验证】[原文：'只要有一个失败，这妥妥的熊市']")
    print("  第一层 国家政策→企业融资")
    print("  ⬜ 超长期特别国债执行进度（需手动更新）")
    print("  ⬜ M1/M2剪刀差收窄与否   ← 茅台买入核心条件 [原文]")

    sse = quotes.get("SSE")
    moutai = quotes.get("Moutai")
    sse_str = f"{sse.price:.0f}点 {sse.pct_change:+.2f}%" if (sse and sse.price > 0) else "N/A"
    mou_str = f"¥{moutai.price:.0f} {moutai.pct_change:+.2f}%" if moutai else "N/A"
    print(f"\n  第二层 企业融资→市场表现")
    print(f"  {'✅' if (sse and sse.price > 3500) else '❌'} 上证 {sse_str}（需站稳3500）")
    print(f"  ⬜ 茅台 {mou_str}（需M1/M2信号）")

    print(f"\n  第三层 市场→民生消费（滞后，季度手动核查）")
    print(f"  ⬜ 社零增速 / 消费信心指数")
    print(f"\n  ⚠️  M1/M2已收窄时，运行加 --m1m2 参数触发茅台买入弹窗")


def print_report(quotes: dict[str, Quote], alerts: list[Alert], signals: list[Signal]) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*55}")
    print(f"  大师兄框架预警系统  {now}")
    print(f"{'='*55}")

    # 价格一览
    print("\n【实时价格】")
    groups = [
        ("核心持仓", ["CRCL", "OXY", "USO"]),
        ("宏观锚定", ["Gold", "BTC", "WTI", "Brent"]),
        ("黑盒子观察", ["TSLA", "BABA", "QQQ"]),
        ("汇率/A股", ["USDJPY", "Moutai", "SSE"]),
    ]
    for group_name, names in groups:
        print(f"\n  ── {group_name} ──")
        for name in names:
            q = quotes.get(name)
            if q and q.price > 0:
                arrow = "▲" if q.pct_change > 0 else "▼" if q.pct_change < 0 else "─"
                unit = "¥" if name in ("Moutai", "SSE") else ""
                if name == "USDJPY" and q.price > 0:
                    usdjpy = 1.0 / q.price
                    print(f"  {name:<8} {usdjpy:>8.2f}円/$   {arrow} {q.pct_change:+.2f}%")
                else:
                    print(f"  {name:<8} {unit}{q.price:>10,.2f}   {arrow} {q.pct_change:+.2f}%")
            else:
                print(f"  {name:<8}  N/A")

    # 时间窗口提醒
    timing = check_timing()
    if timing:
        print("\n【关键时间窗口】")
        for t in timing:
            print(t)

    # 阈值预警
    print("\n【黑盒子点位预警】")
    if alerts:
        for a in alerts:
            prefix = "⚡" if a.is_critical else "  "
            print(f"  {prefix} {a.message}")
    else:
        print("  — 当前无触发点位")

    # 宏观信号
    print("\n【宏观信号矩阵】")
    score = sum(1 for s in signals if s.passed)
    for s in signals:
        mark = "✅" if s.passed else "❌"
        print(f"  {mark} {s.name:<12} {s.detail}")
        print(f"       来源：{s.source}")

    print(f"\n  信号通过率：{score}/{len(signals)}")
    print(f"\n  {macro_conclusion(signals, quotes)}")

    # 逻辑验证
    print_logic_validation(quotes)

    print(f"\n{'='*55}\n")


# ── 通知推送 ──────────────────────────────────────────────────────────────────

def push_notifications(
    alerts: list[Alert],
    signals: list[Signal],
    quotes: dict[str, Quote],
    m1m2_narrowing: bool = False,
) -> None:
    for a in alerts:
        if a.is_critical:
            notify(f"⚡ {a.asset} 价格预警", a.message)

    conclusion = macro_conclusion(signals, quotes)
    if "表外进表内" in conclusion and "共振" in conclusion:
        notify("🟢 表外进表内信号", "Circle↑ + 黄金↓ 同时触发，关注入场窗口")
    elif "真实危机" in conclusion:
        notify("🔴 真实危机信号", "黄金强势上涨，非表外进表内时机")

    # 茅台买入条件：M1/M2收窄 + 价格合适
    if m1m2_narrowing:
        moutai = quotes.get("Moutai")
        if moutai and moutai.price < 2000:
            notify(
                "⚡ 茅台买入条件触发",
                f"M1/M2剪刀差收窄 + 茅台¥{moutai.price:.0f} < ¥2000\n大师兄原话：凡牛必窄，条件成立"
            )


# ── 主程序 ────────────────────────────────────────────────────────────────────

def run_once(manual: str | None, m1m2: bool = False) -> None:
    if manual:
        raw = json.loads(manual)
        # 支持两种格式：
        # {"CRCL": 101} → 只有价格，涨跌幅设为0
        # {"CRCL": {"price": 101, "pct": -20.1}} → 价格+涨跌幅
        quotes = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                quotes[k] = Quote(price=float(v["price"]), pct_change=float(v.get("pct", 0)))
            else:
                quotes[k] = Quote(price=float(v), pct_change=0.0)
    else:
        print("正在拉取实时数据...", end="\r")
        quotes = fetch_quotes(SYMBOLS)
        if not quotes:
            print("❌ 无法获取数据，请检查网络", file=sys.stderr)
            return

    alerts = check_thresholds(quotes)
    signals = evaluate_macro(quotes)
    print_report(quotes, alerts, signals)
    push_notifications(alerts, signals, quotes, m1m2_narrowing=m1m2)


def main() -> int:
    parser = argparse.ArgumentParser(description="大师兄框架实时预警")
    parser.add_argument("--watch", type=int, metavar="分钟",
                        help="循环监控模式，每N分钟检查一次（例：--watch 30）")
    parser.add_argument("--manual", help="手动输入JSON价格数据（测试用）")
    parser.add_argument("--m1m2", action="store_true",
                        help="标记M1/M2剪刀差已收窄，触发茅台买入条件检查")
    args = parser.parse_args()

    try:
        if args.watch:
            print(f"🔄 循环监控已启动，每 {args.watch} 分钟检查一次。Ctrl+C 停止。")
            while True:
                run_once(args.manual, m1m2=args.m1m2)
                time.sleep(args.watch * 60)
        else:
            run_once(args.manual, m1m2=args.m1m2)
        return 0
    except KeyboardInterrupt:
        print("\n已停止监控。")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
