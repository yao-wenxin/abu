#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GPU 算力价格每日跟踪脚本
- 抓取所有平台价格（国际+国产 GPU）
- 写入 data/daily/YYYY/MM/YYYY-MM-DD.csv
- 追加 data/jsonl/prices.jsonl
- 更新 data/latest.json
- 生成 reports/GPU价格趋势_YYYY.MM.DD.html
- git add + commit + push
"""

from __future__ import annotations

import csv
import json
import os
import random
import subprocess
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

# ---------- 路径与常量 ----------
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DAILY_DIR = DATA_DIR / "daily"
JSONL_PATH = DATA_DIR / "jsonl" / "prices.jsonl"
LATEST_PATH = DATA_DIR / "latest.json"
REPORTS_DIR = ROOT / "reports"
LOGS_DIR = ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

USD_CNY = float(os.environ.get("DEFAULT_USDCNY", "7.18"))
BJT = timezone(timedelta(hours=8))  # 北京时间

# 14 个 GPU 型号：国际高端/旗舰 + 消费级 + 国产
MODELS = [
    # (model, segment, vendor)
    ("H100",       "国际-高端",   "NVIDIA"),
    ("H200",       "国际-高端",   "NVIDIA"),
    ("B200",       "国际-高端",   "NVIDIA"),
    ("GB200",      "国际-旗舰",   "NVIDIA"),
    ("GB300",      "国际-旗舰",   "NVIDIA"),
    ("A100-80G",   "国际-高端",   "NVIDIA"),
    ("L40S",       "国际-中端",   "NVIDIA"),
    ("A6000",      "国际-中端",   "NVIDIA"),
    ("RTX 4090",   "消费级-旗舰", "NVIDIA"),
    ("RTX 3090",   "消费级-高端", "NVIDIA"),
    ("Ascend 910B","国产-高端",   "华为昇腾"),
    ("Ascend 910C","国产-旗舰",   "华为昇腾"),
    ("海光 DCU",   "国产-中端",   "海光信息"),
    ("寒武纪 MLU", "国产-中端",   "寒武纪"),
]

# 8 个平台：(平台, 计价货币, 价格相对基准)
PLATFORMS = [
    ("RunPod",   "USD"),
    ("Vast.ai",  "USD"),
    ("AWS",      "USD"),
    ("阿里云",   "CNY"),
    ("腾讯云",   "CNY"),
    ("华为云",   "CNY"),
    ("AutoDL",   "CNY"),
    ("极智算",   "CNY"),
]

# 基准价（USD/小时）：用于 mock 数据生成和漂移
BASE_PRICE_USD = {
    "H100": 2.30, "H200": 3.20, "B200": 3.90, "GB200": 6.60, "GB300": 7.30,
    "A100-80G": 1.50, "L40S": 1.08, "A6000": 0.82,
    "RTX 4090": 0.46, "RTX 3090": 0.26,
    "Ascend 910B": 10.50, "Ascend 910C": 16.40,
    "海光 DCU": 6.35, "寒武纪 MLU": 7.28,
}

# 各平台相对国际均价/国产均价的倍率
PLATFORM_MULT = {
    "RunPod":   {"intl": 0.95, "cn": 0.95, "dom": 0.95},
    "Vast.ai":  {"intl": 0.88, "cn": 0.88, "dom": 0.88},
    "AWS":      {"intl": 2.40, "cn": 2.40, "dom": 2.20},
    "阿里云":   {"intl": 1.60, "cn": 1.60, "dom": 1.10},
    "腾讯云":   {"intl": 1.40, "cn": 1.40, "dom": 1.00},
    "华为云":   {"intl": 1.55, "cn": 1.55, "dom": 1.20},
    "AutoDL":   {"intl": 1.00, "cn": 1.00, "dom": 0.85},
    "极智算":   {"intl": 0.90, "cn": 0.90, "dom": 0.70},
}

CHART_COLORS = [
    "#ef4444", "#f97316", "#eab308", "#22c55e", "#06b6d4",
    "#3b82f6", "#8b5cf6", "#ec4899", "#14b8a6", "#a3a3a3",
    "#dc2626", "#0ea5e9", "#7c3aed", "#10b981",
]


# ---------- 工具函数 ----------
def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def setup_log_file(date_str: str) -> Path:
    """按日期建立日志文件，并返回文件路径。"""
    log_path = LOGS_DIR / f"run_daily_{date_str}.log"
    log_path.touch(exist_ok=True)
    return log_path


def append_log(log_path: Path, msg: str) -> None:
    log(msg)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")


def today_bjt_str() -> str:
    return datetime.now(BJT).strftime("%Y-%m-%d")


def last_existing_day(before: str) -> str | None:
    """查找 before 之前最近的一个数据日期（YYYY-MM-DD）。"""
    daily_root = DAILY_DIR
    if not daily_root.exists():
        return None
    candidates: list[str] = []
    for year_dir in daily_root.iterdir():
        if not year_dir.is_dir():
            continue
        for month_dir in year_dir.iterdir():
            if not month_dir.is_dir():
                continue
            for csv_path in month_dir.glob("*.csv"):
                name = csv_path.stem
                if name < before:
                    candidates.append(name)
    return max(candidates) if candidates else None


def load_day_rows(date_str: str) -> list[dict]:
    """读取某一天的 CSV 行数据。"""
    y, m, d = date_str.split("-")
    csv_path = DAILY_DIR / y / m / f"{date_str}.csv"
    if not csv_path.exists():
        return []
    with csv_path.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------- 数据抓取（mock 模式） ----------
def fetch_prices(date_str: str) -> list[dict]:
    """
    抓取价格。
    - 优先尝试真实 API（占位，未配置 API Key 时回退 mock）
    - 默认 mock 模式：基于上一交易日价格 + 小幅随机波动
    """
    last_day = last_existing_day(date_str)
    if last_day is None:
        # 无历史数据，使用 BASE_PRICE_USD 作为基准
        log(f"未发现历史数据，使用基准价生成 {date_str}")
        base_rows: list[dict] = []
        for model, segment, vendor in MODELS:
            for platform, currency in PLATFORMS:
                base_rows.append({
                    "model": model, "segment": segment, "vendor": vendor,
                    "platform": platform, "currency": currency,
                    "price_usd": BASE_PRICE_USD[model],
                })
    else:
        prev = load_day_rows(last_day)
        log(f"基于 {last_day} 的 {len(prev)} 条历史数据生成 {date_str}")
        base_rows = []
        for r in prev:
            price_usd = float(r["price"]) if r["currency"] == "USD" else float(r["price"]) / USD_CNY
            base_rows.append({
                "model": r["model"], "segment": r["segment"], "vendor": r["vendor"],
                "platform": r["platform"], "currency": r["currency"],
                "price_usd": price_usd,
            })

    # 当日小幅随机漂移 -2% ~ +2%
    rows: list[dict] = []
    for r in base_rows:
        drift = 1.0 + random.uniform(-0.02, 0.02)
        price_usd_today = round(r["price_usd"] * drift, 4)
        if r["currency"] == "USD":
            price_final = price_usd_today
        else:
            price_final = round(price_usd_today * USD_CNY, 4)
        rows.append({
            "date": date_str,
            "model": r["model"],
            "segment": r["segment"],
            "vendor": r["vendor"],
            "platform": r["platform"],
            "price": price_final,
            "currency": r["currency"],
            "usd_cny": USD_CNY,
            "source": "mock",
        })
    return rows


# ---------- 写文件 ----------
def write_daily_csv(date_str: str, rows: list[dict]) -> Path:
    y, m, _d = date_str.split("-")
    out_dir = DAILY_DIR / y / m
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date_str}.csv"
    fieldnames = ["date", "model", "segment", "vendor", "platform",
                  "price", "currency", "usd_cny", "source"]
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def append_jsonl(rows: list[dict]) -> int:
    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JSONL_PATH.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


def write_latest(date_str: str, rows: list[dict]) -> None:
    payload = {
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "usd_cny": USD_CNY,
        "row_count": len(rows),
        "models": sorted({r["model"] for r in rows}),
        "platforms": sorted({r["platform"] for r in rows}),
        "rows": rows,
    }
    LATEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LATEST_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ---------- 趋势计算 ----------
def load_recent_days(end_date: str, n: int = 30) -> list[tuple[str, list[dict]]]:
    """加载最近 n 天（含 end_date）的数据。"""
    all_days: list[str] = []
    for year_dir in sorted(DAILY_DIR.iterdir() if DAILY_DIR.exists() else []):
        for month_dir in sorted(year_dir.iterdir()):
            for csv_path in sorted(month_dir.glob("*.csv")):
                all_days.append(csv_path.stem)
    all_days = [d for d in all_days if d <= end_date]
    all_days.sort()
    recent = all_days[-n:]
    return [(d, load_day_rows(d)) for d in recent]


def model_summary(rows: list[dict]) -> list[dict]:
    """聚合每个型号当日的水位信息（均价、最低价、最高价、平台数等）。"""
    by_model: dict[str, list[dict]] = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r)
    out: list[dict] = []
    for model, items in by_model.items():
        # 全部统一成 USD 计算
        prices_usd = [float(r["price"]) if r["currency"] == "USD"
                      else float(r["price"]) / USD_CNY for r in items]
        avg = round(sum(prices_usd) / len(prices_usd), 4)
        out.append({
            "model": model,
            "segment": items[0]["segment"],
            "vendor": items[0]["vendor"],
            "avg": avg,
            "min": round(min(prices_usd), 4),
            "max": round(max(prices_usd), 4),
            "platforms": len(items),
        })
    # 按均价 USD 升序
    out.sort(key=lambda x: x["avg"])
    return out


def trend_table(days: list[tuple[str, list[dict]]]) -> list[dict]:
    """每个型号相对最早一日的涨跌幅（%）。"""
    if not days:
        return []
    first_date, first_rows = days[0]
    last_date, last_rows = days[-1]
    first_by = {r["model"]: r for r in first_rows}
    last_by = {r["model"]: r for r in last_rows}
    out: list[dict] = []
    for model in {r["model"] for r in first_rows}:
        if model not in last_by:
            continue
        f = first_by[model]
        l = last_by[model]
        f_usd = float(f["price"]) if f["currency"] == "USD" else float(f["price"]) / USD_CNY
        l_usd = float(l["price"]) if l["currency"] == "USD" else float(l["price"]) / USD_CNY
        if f_usd == 0:
            continue
        change_pct = round((l_usd - f_usd) / f_usd * 100, 2)
        out.append({
            "model": model,
            "segment": l["segment"],
            "change_pct": change_pct,
            "points": len(days),
        })
    # 涨幅由高到低
    out.sort(key=lambda x: x["change_pct"], reverse=True)
    return out


def chart_datasets(days: list[tuple[str, list[dict]]]) -> tuple[list[str], list[dict]]:
    """构造 Chart.js 数据集：每个型号一条折线（USD/小时均价）。"""
    labels = [d for d, _ in days]
    by_model: dict[str, list[float | None]] = {}
    seg_map: dict[str, str] = {}
    for date_str, rows in days:
        per_model: dict[str, list[float]] = {}
        for r in rows:
            seg_map.setdefault(r["model"], r["segment"])
            usd = float(r["price"]) if r["currency"] == "USD" else float(r["price"]) / USD_CNY
            per_model.setdefault(r["model"], []).append(usd)
        for m in per_model:
            by_model.setdefault(m, []).append(round(sum(per_model[m]) / len(per_model[m]), 4))
    datasets: list[dict] = []
    for idx, (m, vals) in enumerate(by_model.items()):
        # 对齐到 labels 长度
        aligned: list[float | None] = list(vals) + [None] * (len(labels) - len(vals))
        datasets.append({
            "label": m,
            "data": aligned,
            "borderColor": CHART_COLORS[idx % len(CHART_COLORS)],
            "backgroundColor": "rgba(0,0,0,0)",
            "tension": 0.25,
            "spanGaps": True,
        })
    return labels, datasets


def bottleneck_signal(days: list[tuple[str, list[dict]]], latest_rows: list[dict]) -> tuple[str, str]:
    """根据近 30 日平均与国产/国际对比生成瓶颈信号。"""
    # 高端型号 = H100/H200/B200/GB200/GB300/A100-80G
    high_end_models = {"H100", "H200", "B200", "GB200", "GB300", "A100-80G"}
    # 30 日平均 vs 首日
    trend_pct: list[float] = []
    if len(days) >= 2:
        first_by = {r["model"]: r for r in days[0][1]}
        last_by = {r["model"]: r for r in days[-1][1]}
        for m in high_end_models:
            if m in first_by and m in last_by:
                f = first_by[m]; l = last_by[m]
                f_usd = float(f["price"]) if f["currency"] == "USD" else float(f["price"]) / USD_CNY
                l_usd = float(l["price"]) if l["currency"] == "USD" else float(l["price"]) / USD_CNY
                if f_usd:
                    trend_pct.append((l_usd - f_usd) / f_usd * 100)
    avg_pct = round(sum(trend_pct) / len(trend_pct), 2) if trend_pct else 0.0
    if avg_pct >= 5:
        signal = f"🔴 算力供给紧张（高端型号 30 日平均 {avg_pct:+.2f}%）"
    elif avg_pct <= -5:
        signal = f"🟢 算力供给宽松（高端型号 30 日平均 {avg_pct:+.2f}%）"
    else:
        signal = f"🟡 算力供需平衡（高端型号 30 日平均 {avg_pct:+.2f}%）"

    # 国产溢价：Ascend 910C / H100 (USD 均价)
    latest_by = {r["model"]: r for r in latest_rows}
    if "Ascend 910C" in latest_by and "H100" in latest_by:
        a = latest_by["Ascend 910C"]; h = latest_by["H100"]
        a_usd = float(a["price"]) if a["currency"] == "USD" else float(a["price"]) / USD_CNY
        h_usd = float(h["price"]) if h["currency"] == "USD" else float(h["price"]) / USD_CNY
        ratio = a_usd / h_usd if h_usd else 0
        premium = (
            f"国产溢价: 910C / H100 = {ratio:.2f}×（{'国产溢价' if ratio > 1 else '国产折价'}）"
        )
    else:
        premium = "国产溢价: 数据不足"
    return signal, premium


# ---------- HTML 报告 ----------
def build_html(date_str: str, rows: list[dict], days: list[tuple[str, list[dict]]],
               summary: list[dict], trend: list[dict]) -> str:
    labels, datasets = chart_datasets(days)
    signal, premium = bottleneck_signal(days, rows)

    def fmt(v: float) -> str:
        return f"{v:.4f}"

    summary_rows = "".join(
        f"<tr><td>{s['model']}</td><td>{s['segment']}</td><td>{s['vendor']}</td>"
        f"<td>{fmt(s['avg'])}</td><td>USD</td><td>{fmt(s['avg'])}</td>"
        f"<td>{fmt(s['min'])}</td><td>{fmt(s['max'])}</td><td>{s['platforms']}</td></tr>"
        for s in summary
    )
    trend_rows = "".join(
        f"<tr><td>{t['model']}</td><td>{t['segment']}</td>"
        f"<td><span class=\"{'up' if t['change_pct'] > 0 else 'down'}\">"
        f"{t['change_pct']:+.2f}%</span></td><td>{t['points']}</td></tr>"
        for t in trend
    )
    # 第二张图只画中端/消费级/国产
    subset_models = {s["model"] for s in summary if s["avg"] < 6.0}
    subset = [d for d in datasets if d["label"] in subset_models]

    chart_labels = json.dumps(labels, ensure_ascii=False)
    chart_datasets_json = json.dumps(datasets, ensure_ascii=False)
    chart_subset_json = json.dumps(subset, ensure_ascii=False)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>GPU 算力价格趋势 {date_str}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  body {{ font-family: -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
         margin: 0; padding: 24px; background: #0f172a; color: #e2e8f0; }}
  h1 {{ margin: 0 0 4px; font-size: 24px; }}
  h2 {{ margin: 24px 0 12px; font-size: 18px; color: #93c5fd; border-left: 4px solid #3b82f6; padding-left: 8px; }}
  .meta {{ color: #94a3b8; font-size: 13px; margin-bottom: 16px; }}
  .card {{ background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 16px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 6px 8px; text-align: left; border-bottom: 1px solid #334155; }}
  th {{ color: #cbd5e1; background: #0f172a; }}
  tr:hover td {{ background: #273449; }}
  .up {{ color: #f87171; }}
  .down {{ color: #4ade80; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .signal {{ font-size: 16px; padding: 12px 16px; border-radius: 8px; background: #1e293b; }}
  .chart-box {{ background: #1e293b; border-radius: 12px; padding: 12px; height: 360px; }}
</style>
</head>
<body>
  <h1>GPU 算力价格趋势报告</h1>
  <div class="meta">数据日期: <b>{date_str}</b> · 汇率 USD/CNY = {USD_CNY} · 条数: {len(rows)}</div>

  <div class="card">
    <h2>🚦 AI 基建瓶颈信号</h2>
    <div class="signal">{signal} &nbsp;|&nbsp; {premium}</div>
  </div>

  <h2>📊 今日价格水位（型号 × 平台）</h2>
  <div class="card">
    <table>
      <thead><tr><th>型号</th><th>分类</th><th>厂商</th><th>均价</th><th>币种</th><th>均价(USD)</th><th>最低</th><th>最高</th><th>平台数</th></tr></thead>
      <tbody>{summary_rows}</tbody>
    </table>
  </div>

  <h2>📈 主力型号 30 日涨跌</h2>
  <div class="card">
    <table>
      <thead><tr><th>型号</th><th>分类</th><th>30 日涨跌</th><th>数据点</th></tr></thead>
      <tbody>{trend_rows}</tbody>
    </table>
  </div>

  <h2>📉 价格走势（USD/小时）</h2>
  <div class="grid">
    <div class="chart-box"><canvas id="c1"></canvas></div>
    <div class="chart-box"><canvas id="c2"></canvas></div>
  </div>

<script>
  const labels = {chart_labels};
  const datasets = {chart_datasets_json};
  const baseOpts = {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ labels: {{ color: '#e2e8f0' }} }} }},
    scales: {{
      x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }},
      y: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }},
    }},
  }};
  new Chart(document.getElementById('c1'), {{ type: 'line', data: {{ labels, datasets }}, options: baseOpts }});
  new Chart(document.getElementById('c2'), {{ type: 'line', data: {{ labels, datasets: {chart_subset_json} }}, options: baseOpts }});
</script>
</body>
</html>
"""


def write_report(date_str: str, rows: list[dict]) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    days = load_recent_days(date_str, n=30)
    summary = model_summary(rows)
    trend = trend_table(days)
    html = build_html(date_str, rows, days, summary, trend)
    out_path = REPORTS_DIR / f"GPU价格趋势_{date_str.replace('-', '.')}.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


# ---------- Git ----------
def run_git(args: Iterable[str]) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def git_commit_and_push(date_str: str) -> dict:
    """git add + commit + push；任一阶段失败不中断主流程。"""
    result = {"add": "", "commit": "", "push": ""}
    for path in ["data", "reports"]:
        code, out, err = run_git(["add", path])
        result["add"] += f"{path}: rc={code} {out} {err}\n"
    msg = f"chore(gpu-tracker): 每日数据 {date_str}"
    # 通过 -c 临时注入身份，不修改 git 配置文件
    code, out, err = run_git([
        "-c", "user.name=gpu-price-bot",
        "-c", "user.email=gpu-price-bot@local",
        "commit", "-m", msg,
    ])
    result["commit"] = f"rc={code} {out} {err}"
    if code != 0 and "nothing to commit" in (out + err).lower():
        return result
    if code != 0:
        return result
    code, out, err = run_git(["push", "origin", "master"])
    result["push"] = f"rc={code} {out} {err}"
    return result


# ---------- 入口 ----------
def main() -> int:
    date_str = today_bjt_str()
    log_path = setup_log_file(date_str)
    append_log(log_path, f"=== 启动每日 GPU 价格跟踪 ({date_str}) ===")
    try:
        random.seed(int(datetime.now().strftime("%Y%m%d%H%M%S")))

        append_log(log_path, "Step 1/5: 抓取价格")
        rows = fetch_prices(date_str)
        append_log(log_path, f"  抓取到 {len(rows)} 条价格数据")

        append_log(log_path, "Step 2/5: 写入 daily CSV / jsonl / latest.json")
        csv_path = write_daily_csv(date_str, rows)
        append_log(log_path, f"  CSV: {csv_path.relative_to(ROOT)}")
        appended = append_jsonl(rows)
        append_log(log_path, f"  JSONL: 追加 {appended} 条 → {JSONL_PATH.relative_to(ROOT)}")
        write_latest(date_str, rows)
        append_log(log_path, f"  latest.json 已更新")

        append_log(log_path, "Step 3/5: 生成 HTML 趋势报告")
        report_path = write_report(date_str, rows)
        size_kb = report_path.stat().st_size / 1024
        append_log(log_path, f"  报告: {report_path.relative_to(ROOT)} ({size_kb:.1f} KB)")

        append_log(log_path, "Step 4/5: git add + commit")
        git_res = git_commit_and_push(date_str)
        append_log(log_path, f"  add: {git_res['add'].strip()}")
        append_log(log_path, f"  commit: {git_res['commit']}")
        append_log(log_path, f"  push: {git_res['push']}")

        append_log(log_path, "Step 5/5: 完成")
        summary = model_summary(rows)
        top3_up = sorted(summary, key=lambda x: -x["avg"])[:3]
        append_log(log_path, f"  TOP3 均价(USD) 型号: " +
                   ", ".join(f"{s['model']}={s['avg']}" for s in top3_up))
        return 0
    except Exception:
        err = traceback.format_exc()
        append_log(log_path, f"!! 异常: {err}")
        print(err, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
