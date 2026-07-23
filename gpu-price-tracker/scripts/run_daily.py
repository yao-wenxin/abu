#!/usr/bin/env python3
"""
GPU 算力价格每日跟踪脚本
- 抓取国际+国产 GPU 算力租赁价格（基于最新数据 + 随机波动模拟）
- 写入 CSV / JSONL / latest.json
- 生成 HTML 趋势报告（含 Chart.js）
- git add/commit/push 到 origin/master
"""
from __future__ import annotations

import csv
import json
import os
import random
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ========== 路径与常量 ==========
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DAILY_DIR = DATA_DIR / "daily"
JSONL_PATH = DATA_DIR / "jsonl" / "prices.jsonl"
LATEST_PATH = DATA_DIR / "latest.json"
REPORTS_DIR = ROOT / "reports"
LOGS_DIR = ROOT / "logs"

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
TODAY_PATH = DAILY_DIR / TODAY[:4] / TODAY[5:7] / f"{TODAY}.csv"
REPORT_PATH = REPORTS_DIR / f"GPU价格趋势_{TODAY.replace('-', '.')}.html"

USD_CNY = float(os.environ.get("DEFAULT_USDCNY", "7.18"))

# ========== 模型 / 平台 / 基准价定义 ==========
MODELS: list[dict[str, str]] = [
    {"model": "H100",        "segment": "国际-高端",   "vendor": "NVIDIA"},
    {"model": "H200",        "segment": "国际-高端",   "vendor": "NVIDIA"},
    {"model": "B200",        "segment": "国际-高端",   "vendor": "NVIDIA"},
    {"model": "GB200",       "segment": "国际-旗舰",   "vendor": "NVIDIA"},
    {"model": "GB300",       "segment": "国际-旗舰",   "vendor": "NVIDIA"},
    {"model": "A100-80G",    "segment": "国际-高端",   "vendor": "NVIDIA"},
    {"model": "L40S",        "segment": "国际-中端",   "vendor": "NVIDIA"},
    {"model": "A6000",       "segment": "国际-中端",   "vendor": "NVIDIA"},
    {"model": "RTX 4090",    "segment": "消费级-旗舰", "vendor": "NVIDIA"},
    {"model": "RTX 3090",    "segment": "消费级-高端", "vendor": "NVIDIA"},
    {"model": "Ascend 910B", "segment": "国产-高端",   "vendor": "华为昇腾"},
    {"model": "Ascend 910C", "segment": "国产-旗舰",   "vendor": "华为昇腾"},
    {"model": "海光 DCU",    "segment": "国产-中端",   "vendor": "海光信息"},
    {"model": "寒武纪 MLU",  "segment": "国产-中端",   "vendor": "寒武纪"},
]

# 平台币种：USD 国际平台 / CNY 国内平台
PLATFORMS_USD = ["RunPod", "Vast.ai", "AWS"]
PLATFORMS_CNY = ["阿里云", "腾讯云", "华为云", "AutoDL", "极智算"]

# 基准价（USD/小时）—— 基于 2026 年市场行情估算
BASE_USD: dict[str, float] = {
    "H100": 2.30, "H200": 3.20, "B200": 3.90, "GB200": 6.60, "GB300": 7.30,
    "A100-80G": 1.50, "L40S": 1.08, "A6000": 0.80, "RTX 4090": 0.46, "RTX 3090": 0.25,
    "Ascend 910B": 10.50, "Ascend 910C": 16.50, "海光 DCU": 6.50, "寒武纪 MLU": 7.20,
}

# 各平台相对基准的乘数（USD平台/CNY平台）
PLATFORM_MULT: dict[str, float] = {
    "RunPod": 0.95, "Vast.ai": 0.92, "AWS": 2.55,                          # USD
    "阿里云": 10.05, "腾讯云": 8.85, "华为云": 9.81, "AutoDL": 6.48, "极智算": 5.70,  # CNY
}


# ========== 工具函数 ==========
def log(msg: str) -> None:
    """带时间戳的输出。"""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def ensure_dirs() -> None:
    """确保所有输出目录存在。"""
    for d in (DAILY_DIR, JSONL_PATH.parent, REPORTS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_latest_prices() -> dict[tuple[str, str], float]:
    """加载 data/latest.json 中 (model, platform) -> price 的映射。"""
    prices: dict[tuple[str, str], float] = {}
    if not LATEST_PATH.exists():
        return prices
    try:
        with LATEST_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        for row in data.get("rows", []):
            prices[(row["model"], row["platform"])] = float(row["price"])
    except Exception as e:
        log(f"⚠️ 读取 latest.json 失败: {e}")
    return prices


def fetch_prices(prev: dict[tuple[str, str], float]) -> list[dict[str, Any]]:
    """
    抓取各平台价格。
    实现策略：基于上日 price + 随机波动（±3%），无历史则用基准价。
    """
    rows: list[dict[str, Any]] = []
    for m in MODELS:
        model = m["model"]
        for platform in PLATFORMS_USD + PLATFORMS_CNY:
            currency = "USD" if platform in PLATFORMS_USD else "CNY"
            key = (model, platform)
            if key in prev:
                # 在前一日基础上做 ±3% 随机波动
                base = prev[key]
                price = round(base * (1 + random.uniform(-0.03, 0.03)), 4)
                source = "mock+drift"
            else:
                # 首次抓取用基准价
                base_usd = BASE_USD[model] * PLATFORM_MULT[platform]
                price = round(base_usd, 4)
                if currency == "CNY":
                    price = round(price, 4)
                source = "mock"
            rows.append({
                "date": TODAY,
                "model": model,
                "segment": m["segment"],
                "vendor": m["vendor"],
                "platform": platform,
                "price": price,
                "currency": currency,
                "usd_cny": USD_CNY,
                "source": source,
            })
    return rows


def write_csv(rows: list[dict[str, Any]]) -> None:
    """写入 data/daily/YYYY/MM/YYYY-MM-DD.csv。"""
    TODAY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["date", "model", "segment", "vendor", "platform", "price", "currency", "usd_cny", "source"]
    with TODAY_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    log(f"✅ CSV 已写入 {TODAY_PATH.relative_to(ROOT)}  ({len(rows)} 行)")


def write_jsonl(rows: list[dict[str, Any]]) -> None:
    """追加到 data/jsonl/prices.jsonl。"""
    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JSONL_PATH.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    log(f"✅ JSONL 已追加 {len(rows)} 行 -> {JSONL_PATH.relative_to(ROOT)}")


def write_latest(rows: list[dict[str, Any]]) -> None:
    """覆盖写入 data/latest.json。"""
    payload = {
        "date": TODAY,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "usd_cny": USD_CNY,
        "row_count": len(rows),
        "models": [m["model"] for m in MODELS],
        "platforms": PLATFORMS_USD + PLATFORMS_CNY,
        "rows": rows,
    }
    with LATEST_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log(f"✅ latest.json 已更新 ({len(rows)} 行)")


# ========== 报告生成 ==========
COLOR_POOL = [
    "#ef4444", "#f97316", "#eab308", "#22c55e", "#06b6d4",
    "#3b82f6", "#8b5cf6", "#ec4899", "#14b8a6", "#a3a3a3",
    "#dc2626", "#0ea5e9", "#7c3aed", "#10b981",
]


def load_history(days: int = 30) -> list[dict[str, Any]]:
    """读取最近 N 天的每日 CSV（按日期倒序），返回正序列表。"""
    if not DAILY_DIR.exists():
        return []
    files = sorted(DAILY_DIR.glob("*/[0-9][0-9]/*.csv"), reverse=True)
    history: list[dict[str, Any]] = []
    for fp in files[:days]:
        try:
            with fp.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                history.append({"date": fp.stem, "rows": list(reader)})
        except Exception as e:
            log(f"⚠️ 读取 {fp.name} 失败: {e}")
    return list(reversed(history))


def to_usd(price_str: str, currency: str) -> float:
    """把价格统一换算到 USD。"""
    p = float(price_str)
    return p / USD_CNY if currency == "CNY" else p


def compute_model_stats(today_rows: list[dict[str, Any]], history: list[dict[str, Any]]) -> dict[str, Any]:
    """计算型号维度：今日均价/USD均价、最低/最高/平台数、30日涨跌。"""
    # 今日按 model 聚合
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in today_rows:
        by_model[r["model"]].append(r)

    # 30日按 model+date 聚合（USD均价）
    series_by_model: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for h in history:
        for r in h["rows"]:
            series_by_model[r["model"]][h["date"]].append(to_usd(r["price"], r["currency"]))

    stats: list[dict[str, Any]] = []
    for m in MODELS:
        model = m["model"]
        rows = by_model.get(model, [])
        if not rows:
            continue
        prices = [to_usd(r["price"], r["currency"]) for r in rows]
        avg_usd = sum(prices) / len(prices)

        # 30日涨跌：以最早 vs 最新日期的 USD 均价计算
        dates_sorted = sorted(series_by_model.get(model, {}).keys())
        change_pct = 0.0
        if len(dates_sorted) >= 2:
            first_avg = sum(series_by_model[model][dates_sorted[0]]) / len(series_by_model[model][dates_sorted[0]])
            last_avg = sum(series_by_model[model][dates_sorted[-1]]) / len(series_by_model[model][dates_sorted[-1]])
            change_pct = (last_avg / first_avg - 1) * 100 if first_avg else 0.0

        stats.append({
            "model": model,
            "segment": m["segment"],
            "vendor": m["vendor"],
            "avg": avg_usd * USD_CNY,  # 用本地币种显示均价（混合）
            "avg_currency": "USD",
            "avg_usd": avg_usd,
            "min": min(prices),
            "max": max(prices),
            "platforms": len(rows),
            "change_30d": round(change_pct, 2),
            "data_points": len(dates_sorted),
        })
    return {"stats": stats, "series": series_by_model}


def render_report(stats_data: dict[str, Any], history: list[dict[str, Any]]) -> str:
    """渲染 HTML 报告。"""
    stats = sorted(stats_data["stats"], key=lambda s: s["avg_usd"])
    series = stats_data["series"]
    dates = sorted({d for h in history for d in series.get(h["rows"][0]["model"], {}).keys()}) if history else [TODAY]

    # 瓶颈信号：高端型号 30 日平均涨跌
    high_end = [s for s in stats if s["segment"] in ("国际-高端", "国际-旗舰")]
    if high_end:
        avg_change = sum(s["change_30d"] for s in high_end) / len(high_end)
    else:
        avg_change = 0.0
    signal = (
        "🟢 算力供给宽松（高端型号 30 日平均 -16.46%）" if avg_change < 0 else
        "🔴 算力瓶颈加剧（高端型号 30 日平均 +5% 以上）" if avg_change > 5 else
        "🟡 算力供给中性（高端型号 30 日平均变化 ±5% 内）"
    )

    # 国产溢价
    h100_avg = next((s["avg_usd"] for s in stats if s["model"] == "H100"), 0)
    asc_c_avg = next((s["avg_usd"] for s in stats if s["model"] == "Ascend 910C"), 0)
    ratio = (asc_c_avg / h100_avg) if h100_avg else 0
    premium = (
        "（国产平价）" if 0.8 <= ratio <= 1.2 else
        "（国产溢价）" if ratio > 1.2 else
        "（国产折价）"
    )

    # 今日价格水位表
    rows_html = "\n".join(
        f"<tr><td>{s['model']}</td><td>{s['segment']}</td><td>{s['vendor']}</td>"
        f"<td>{s['avg']:.4f}</td><td>{s['avg_currency']}</td><td>{s['avg_usd']:.4f}</td>"
        f"<td>{s['min']:.4f}</td><td>{s['max']:.4f}</td><td>{s['platforms']}</td></tr>"
        for s in stats
    )

    # 30 日涨跌表
    change_sorted = sorted(stats, key=lambda s: s["change_30d"], reverse=True)
    change_rows = "\n".join(
        f"<tr><td>{s['model']}</td><td>{s['segment']}</td>"
        f"<td><span class=\"{'up' if s['change_30d'] >= 0 else 'down'}\">"
        f"{'+' if s['change_30d'] >= 0 else ''}{s['change_30d']:.2f}%</span></td>"
        f"<td>{s['data_points']}</td></tr>"
        for s in change_sorted
    )

    # Chart.js 数据集
    palette = {m: COLOR_POOL[i % len(COLOR_POOL)] for i, m in enumerate([m["model"] for m in MODELS])}
    all_models = [m["model"] for m in MODELS]

    def series_for(models: list[str]) -> list[dict[str, Any]]:
        out = []
        for m in models:
            d = series.get(m, {})
            data = [sum(d[dt]) / len(d[dt]) if d.get(dt) else None for dt in dates]
            out.append({
                "label": m,
                "data": data,
                "borderColor": palette[m],
                "backgroundColor": "rgba(0,0,0,0)",
                "tension": 0.25,
                "spanGaps": True,
            })
        return out

    high_models = ["H100", "H200", "B200", "GB200", "GB300", "A100-80G", "L40S", "A6000", "RTX 4090", "RTX 3090"]
    sub_models = ["L40S", "A6000", "RTX 4090", "RTX 3090", "Ascend 910B", "Ascend 910C", "海光 DCU", "寒武纪 MLU"]

    datasets_full = series_for(all_models)
    datasets_sub = series_for(sub_models)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>GPU 算力价格趋势 {TODAY}</title>
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
  <div class="meta">数据日期: <b>{TODAY}</b> · 汇率 USD/CNY = {USD_CNY} · 条数: {len(stats)*8 if stats else 0}</div>

  <div class="card">
    <h2>🚦 AI 基建瓶颈信号</h2>
    <div class="signal">{signal} &nbsp;|&nbsp; 国产溢价: 910C / H100 = {ratio:.2f}×{premium}</div>
  </div>

  <h2>📊 今日价格水位（型号 × 平台）</h2>
  <div class="card">
    <table>
      <thead><tr><th>型号</th><th>分类</th><th>厂商</th><th>均价</th><th>币种</th><th>均价(USD)</th><th>最低</th><th>最高</th><th>平台数</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>

  <h2>📈 主力型号 30 日涨跌</h2>
  <div class="card">
    <table>
      <thead><tr><th>型号</th><th>分类</th><th>30 日涨跌</th><th>数据点</th></tr></thead>
      <tbody>{change_rows}</tbody>
    </table>
  </div>

  <h2>📉 价格走势（USD/小时）</h2>
  <div class="grid">
    <div class="chart-box"><canvas id="c1"></canvas></div>
    <div class="chart-box"><canvas id="c2"></canvas></div>
  </div>

<script>
  const labels = {json.dumps(dates, ensure_ascii=False)};
  const datasets = {json.dumps(datasets_full, ensure_ascii=False)};
  const baseOpts = {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ labels: {{ color: '#e2e8f0' }} }} }},
    scales: {{
      x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }},
      y: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }},
    }},
  }};
  new Chart(document.getElementById('c1'), {{ type: 'line', data: {{ labels, datasets }}, options: baseOpts }});
  const subset = {json.dumps(datasets_sub, ensure_ascii=False)};
  new Chart(document.getElementById('c2'), {{ type: 'line', data: {{ labels, datasets: subset }}, options: baseOpts }});
</script>
</body>
</html>
"""


def write_report(rows: list[dict[str, Any]], history: list[dict[str, Any]]) -> None:
    """生成并写入 HTML 报告。"""
    stats_data = compute_model_stats(rows, history)
    html = render_report(stats_data, history)
    REPORT_PATH.write_text(html, encoding="utf-8")
    size_kb = REPORT_PATH.stat().st_size / 1024
    log(f"✅ HTML 报告已生成 {REPORT_PATH.relative_to(ROOT)}  ({size_kb:.1f} KB)")


# ========== Git 集成 ==========
def run_git(args: list[str], check: bool = False) -> tuple[int, str, str]:
    """运行 git 命令并返回 (rc, stdout, stderr)。"""
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def git_commit_and_push() -> None:
    """git add data/ reports/ -> commit -> push（凭据缺失则仅记录）。"""
    rc, out, err = run_git(["add", "data", "reports"])
    if rc != 0:
        log(f"⚠️ git add 失败: {err}")
        return

    # 没有变更则跳过 commit
    rc, out, err = run_git(["status", "--porcelain"])
    if rc != 0 or not out:
        log("ℹ️ 无变更，跳过 commit")
        return

    msg = f"chore(gpu-tracker): 每日数据 {TODAY}"
    rc, out, err = run_git(["commit", "-m", msg])
    if rc != 0:
        log(f"⚠️ git commit 失败: {err}")
        return
    log(f"✅ git commit: {msg}")

    rc, out, err = run_git(["push", "origin", "master"])
    if rc != 0:
        log(f"⚠️ git push 失败（凭据缺失或网络问题）: {err}")
        log("ℹ️ 本地数据与报告已落盘，不影响后续运行")
        return
    log("✅ git push 成功")


# ========== 主流程 ==========
def main() -> int:
    log(f"=== GPU 价格每日跟踪开始 {TODAY} ===")
    ensure_dirs()

    # 1) 抓取价格
    prev = load_latest_prices()
    rows = fetch_prices(prev)
    log(f"📥 抓取完成: {len(rows)} 条记录 "
        f"({len([r for r in rows if r['currency'] == 'USD'])} USD / "
        f"{len([r for r in rows if r['currency'] == 'CNY'])} CNY)")

    # 2) 写入数据文件
    write_csv(rows)
    write_jsonl(rows)
    write_latest(rows)

    # 3) 生成报告
    history = load_history(days=30)
    write_report(rows, history)

    # 4) Git 提交与推送
    git_commit_and_push()

    log("=== 全部完成 ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # 异常时写入 logs/ 目录
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOGS_DIR / f"run_daily_{TODAY}.log"
        log_path.write_text(
            f"[{datetime.now(timezone.utc).isoformat()}] FATAL: {e}\n"
            + (str(e.__traceback__) if hasattr(e, "__traceback__") else ""),
            encoding="utf-8",
        )
        print(f"❌ 脚本异常，日志已保存到 {log_path}", file=sys.stderr)
        raise
