#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPU 算力租赁价格每日跟踪脚本。

工作流：
  1) 抓取（mock）所有平台价格（型号见 MODELS，平台见 PLATFORMS）
  2) 写入 data/daily/YYYY/MM/YYYY-MM-DD.csv
  3) 追加到 data/jsonl/prices.jsonl
  4) 更新 data/latest.json
  5) 生成 reports/GPU价格趋势_YYYY.MM.DD.html
  6) git add data/ reports/ 并提交
  7) git push 到 origin/master（凭据缺失则记录失败但不影响本地数据）
"""
from __future__ import annotations

import csv
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

# ----------------------------- 常量配置 ----------------------------- #
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DAILY_DIR = DATA_DIR / "daily"
JSONL_PATH = DATA_DIR / "jsonl" / "prices.jsonl"
LATEST_PATH = DATA_DIR / "latest.json"
REPORTS_DIR = ROOT / "reports"
LOGS_DIR = ROOT / "logs"
TREND_DAYS = int(os.environ.get("TREND_DAYS", "30"))
USD_CNY = float(os.environ.get("DEFAULT_USDCNY", "7.18"))
TODAY = datetime.now(timezone.utc).astimezone()

# 14 个 GPU 型号 × 8 个平台 = 112 条数据
MODELS: List[Tuple[str, str, str]] = [
    ("H100", "国际-高端", "NVIDIA"),
    ("H200", "国际-高端", "NVIDIA"),
    ("B200", "国际-高端", "NVIDIA"),
    ("GB200", "国际-旗舰", "NVIDIA"),
    ("GB300", "国际-旗舰", "NVIDIA"),
    ("A100-80G", "国际-高端", "NVIDIA"),
    ("L40S", "国际-中端", "NVIDIA"),
    ("A6000", "国际-中端", "NVIDIA"),
    ("RTX 4090", "消费级-旗舰", "NVIDIA"),
    ("RTX 3090", "消费级-高端", "NVIDIA"),
    ("Ascend 910B", "国产-高端", "华为昇腾"),
    ("Ascend 910C", "国产-旗舰", "华为昇腾"),
    ("海光 DCU", "国产-中端", "海光信息"),
    ("寒武纪 MLU", "国产-中端", "寒武纪"),
]
PLATFORMS: List[Tuple[str, str]] = [
    ("RunPod", "USD"),
    ("Vast.ai", "USD"),
    ("AWS", "USD"),
    ("阿里云", "CNY"),
    ("腾讯云", "CNY"),
    ("华为云", "CNY"),
    ("AutoDL", "CNY"),
    ("极智算", "CNY"),
]
# 各平台相对基础价的浮动系数（AWS 偏贵，Vast.ai 偏便宜）
PLATFORM_FACTOR = {
    "RunPod": 1.00,
    "Vast.ai": 0.94,
    "AWS": 2.18,
    "阿里云": 9.65,  # CNY
    "腾讯云": 8.42,
    "华为云": 8.41,
    "AutoDL": 6.46,
    "极智算": 5.65,
}
# 各型号基础价（USD/小时）
MODEL_BASE = {
    "H100": 2.40,
    "H200": 3.32,
    "B200": 3.95,
    "GB200": 6.43,
    "GB300": 7.70,
    "A100-80G": 1.65,
    "L40S": 1.06,
    "A6000": 0.73,
    "RTX 4090": 0.44,
    "RTX 3090": 0.24,
    "Ascend 910B": 9.50,
    "Ascend 910C": 14.38,
    "海光 DCU": 5.88,
    "寒武纪 MLU": 6.69,
}

CHART_COLORS = [
    "#ef4444", "#f97316", "#eab308", "#22c55e", "#06b6d4",
    "#3b82f6", "#8b5cf6", "#ec4899", "#14b8a6", "#a3a3a3",
    "#dc2626", "#0ea5e9", "#7c3aed", "#10b981",
]


# ----------------------------- 工具函数 ----------------------------- #
def setup_dirs() -> None:
    """确保所需目录存在。"""
    for p in [DAILY_DIR, JSONL_PATH.parent, REPORTS_DIR, LOGS_DIR]:
        p.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    """统一日志输出。"""
    print(f"[{TODAY.strftime('%H:%M:%S')}] {msg}", flush=True)


def write_log_file(name: str, content: str) -> Path:
    """把日志保存到 logs/ 目录。"""
    path = LOGS_DIR / name
    path.write_text(content, encoding="utf-8")
    return path


def fmt_date(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


def prev_business_day(d: datetime) -> datetime:
    """获取上一个工作日（跳过周末）。"""
    from datetime import timedelta
    p = d - timedelta(days=1)
    while p.weekday() >= 5:  # 5=周六, 6=周日
        p -= timedelta(days=1)
    return p


def load_latest_snapshot() -> Dict[Tuple[str, str], float]:
    """加载 latest.json 中昨日价格快照（用于随机游走）。"""
    if not LATEST_PATH.exists():
        return {}
    try:
        with LATEST_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        out: Dict[Tuple[str, str], float] = {}
        for r in data.get("rows", []):
            out[(r["model"], r["platform"])] = float(r["price"])
        return out
    except Exception as exc:  # noqa: BLE001
        log(f"读取 latest.json 失败，将使用基础价生成: {exc}")
        return {}


# ----------------------------- 核心业务 ----------------------------- #
def generate_prices(prev: Dict[Tuple[str, str], float]) -> List[dict]:
    """生成今日所有平台价格（基于昨日价格做小幅随机游走）。"""
    rows: List[dict] = []
    today_str = fmt_date(TODAY)
    rng = random.Random(today_str)  # 用日期做种子保证同日幂等
    for model, segment, vendor in MODELS:
        base = MODEL_BASE[model]
        for platform, currency in PLATFORMS:
            factor = PLATFORM_FACTOR[platform]
            if currency == "CNY":
                ref = base * factor  # 基础为人民币
            else:
                ref = base * factor / USD_CNY  # 转美元
            key = (model, platform)
            if key in prev:
                # 随机游走：±6% 抖动 + 缓慢趋势
                shock = rng.uniform(-0.06, 0.06)
                drift = rng.uniform(-0.02, 0.02)
                ref = max(0.05, prev[key] * (1.0 + shock + drift))
            else:
                ref = ref * rng.uniform(0.94, 1.06)
            price = round(ref, 4)
            rows.append({
                "date": today_str,
                "model": model,
                "segment": segment,
                "vendor": vendor,
                "platform": platform,
                "price": price,
                "currency": currency,
                "usd_cny": USD_CNY,
                "source": "mock",
            })
    return rows


def write_daily_csv(rows: List[dict]) -> Path:
    """写入 data/daily/YYYY/MM/YYYY-MM-DD.csv。"""
    year = TODAY.strftime("%Y")
    month = TODAY.strftime("%m")
    target_dir = DAILY_DIR / year / month
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{fmt_date(TODAY)}.csv"
    fields = ["date", "model", "segment", "vendor", "platform",
              "price", "currency", "usd_cny", "source"]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def load_existing_keys(today_str: str) -> set:
    """读取 jsonl 中今日已存在的 (model, platform) 键集合。"""
    keys = set()
    if not JSONL_PATH.exists():
        return keys
    for line in JSONL_PATH.open("r", encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("date") == today_str:
            keys.add((r["model"], r["platform"]))
    return keys


def append_jsonl(rows: List[dict]) -> Tuple[int, int]:
    """追加到 data/jsonl/prices.jsonl，返回 (新增, 当前总行数)。

    幂等：今日已存在的 (model, platform) 会被跳过。
    """
    today_str = fmt_date(TODAY)
    existing = load_existing_keys(today_str)
    new_rows = [r for r in rows if (r["model"], r["platform"]) not in existing]
    with JSONL_PATH.open("a", encoding="utf-8") as f:
        for r in new_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    total = sum(1 for _ in JSONL_PATH.open("r", encoding="utf-8"))
    return len(new_rows), total


def write_latest(rows: List[dict]) -> Path:
    """更新 data/latest.json。"""
    payload = {
        "date": fmt_date(TODAY),
        "generated_at": TODAY.isoformat(),
        "usd_cny": USD_CNY,
        "row_count": len(rows),
        "models": [m[0] for m in MODELS],
        "platforms": [p[0] for p in PLATFORMS],
        "rows": rows,
    }
    with LATEST_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return LATEST_PATH


def load_recent_days(days: int) -> Dict[str, List[dict]]:
    """读取最近 N 天的数据，按日期分组。"""
    by_date: Dict[str, List[dict]] = {}
    if not JSONL_PATH.exists():
        return by_date
    for line in JSONL_PATH.open("r", encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        by_date.setdefault(r["date"], []).append(r)
    # 只保留最近 N 天
    sorted_dates = sorted(by_date.keys())[-days:]
    return {d: by_date[d] for d in sorted_dates}


def to_usd(price: float, currency: str) -> float:
    return price / USD_CNY if currency == "CNY" else price


# ----------------------------- 报告生成 ----------------------------- #
def build_html(by_date: Dict[str, List[dict]], today_rows: List[dict]) -> str:
    """生成 HTML 趋势报告。"""
    dates = sorted(by_date.keys())
    # 今日价格水位
    agg: Dict[str, Dict] = {}
    for r in today_rows:
        m = r["model"]
        usd = to_usd(r["price"], r["currency"])
        a = agg.setdefault(m, {
            "segment": r["segment"], "vendor": r["vendor"],
            "prices": [], "currencies": set(), "usd": [],
        })
        a["prices"].append(r["price"])
        a["currencies"].add(r["currency"])
        a["usd"].append(usd)
    # 选代表币种：先 USD 否则 CNY
    today_table_rows = []
    for m, a in agg.items():
        avg_native = round(sum(a["prices"]) / len(a["prices"]), 4)
        avg_usd = round(sum(a["usd"]) / len(a["usd"]), 4)
        cur = "USD" if "USD" in a["currencies"] else "CNY"
        today_table_rows.append({
            "model": m, "segment": a["segment"], "vendor": a["vendor"],
            "avg": avg_native, "currency": cur, "avg_usd": avg_usd,
            "min": min(a["usd"]), "max": max(a["usd"]), "n": len(a["usd"]),
        })
    today_table_rows.sort(key=lambda x: x["avg_usd"])

    # 30 日涨跌
    trend_rows = []
    if len(dates) >= 2:
        for m in [x[0] for x in MODELS]:
            series = []
            for d in dates:
                day_usd = [to_usd(r["price"], r["currency"])
                           for r in by_date[d] if r["model"] == m]
                if day_usd:
                    series.append((d, sum(day_usd) / len(day_usd)))
            if len(series) >= 2:
                first, last = series[0][1], series[-1][1]
                change = (last - first) / first * 100 if first else 0.0
                seg = next((x[1] for x in MODELS if x[0] == m), "")
                trend_rows.append({
                    "model": m, "segment": seg, "change": change, "n": len(series),
                })
    trend_rows.sort(key=lambda x: x["change"], reverse=True)

    # 图表数据
    chart_labels = json.dumps(dates, ensure_ascii=False)
    chart_datasets = []
    subset_datasets = []
    consumer_models = {"RTX 4090", "RTX 3090", "L40S", "A6000"}
    for i, m in enumerate([x[0] for x in MODELS]):
        series = []
        for d in dates:
            vals = [to_usd(r["price"], r["currency"])
                    for r in by_date[d] if r["model"] == m]
            series.append(round(sum(vals) / len(vals), 4) if vals else None)
        ds = {
            "label": m,
            "data": series,
            "borderColor": CHART_COLORS[i % len(CHART_COLORS)],
            "backgroundColor": "rgba(0,0,0,0)",
            "tension": 0.25,
            "spanGaps": True,
        }
        chart_datasets.append(ds)
        if m in consumer_models or m.startswith("Ascend") or m in ("海光 DCU", "寒武纪 MLU"):
            subset_datasets.append(ds)

    # 瓶颈信号
    high_end_change = sum(t["change"] for t in trend_rows
                          if t["segment"].startswith("国际-")) / max(1, len([t for t in trend_rows if t["segment"].startswith("国际-")]))
    h100_usd = next((r["avg_usd"] for r in today_table_rows if r["model"] == "H100"), 0)
    asc910c_usd = next((r["avg_usd"] for r in today_table_rows if r["model"] == "Ascend 910C"), 0)
    premium = asc910c_usd / h100_usd if h100_usd else 0
    if high_end_change > 5:
        signal_tone, signal_text = "🔴", f"算力仍处瓶颈期（高端 30 日平均 {high_end_change:+.2f}%）"
    elif high_end_change < -5:
        signal_tone, signal_text = "🟢", f"算力供给宽松（高端型号 30 日平均 {high_end_change:+.2f}%）"
    else:
        signal_tone, signal_text = "🟡", f"算力供需平衡（高端型号 30 日平均 {high_end_change:+.2f}%）"
    premium_label = "国产溢价" if premium > 1.2 else ("基本平价" if premium > 0.8 else "国产折价")
    signal_line = (f"{signal_tone} {signal_text} &nbsp;|&nbsp; "
                   f"国产溢价: 910C / H100 = {premium:.2f}×（{premium_label}）")

    # 渲染表格行
    def trow(cells: Iterable[str]) -> str:
        return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"

    today_tbody = "".join(
        trow([r["model"], r["segment"], r["vendor"],
              f"{r['avg']:.4f}", r["currency"], f"{r['avg_usd']:.4f}",
              f"{r['min']:.4f}", f"{r['max']:.4f}", str(r["n"])])
        for r in today_table_rows
    )
    trend_tbody = "".join(
        trow([r["model"], r["segment"],
              f'<span class="{"up" if r["change"] >= 0 else "down"}">{r["change"]:+.2f}%</span>',
              str(r["n"])])
        for r in trend_rows
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>GPU 算力价格趋势 {fmt_date(TODAY)}</title>
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
  <div class="meta">数据日期: <b>{fmt_date(TODAY)}</b> · 汇率 USD/CNY = {USD_CNY} · 条数: {len(today_rows)}</div>

  <div class="card">
    <h2>🚦 AI 基建瓶颈信号</h2>
    <div class="signal">{signal_line}</div>
  </div>

  <h2>📊 今日价格水位（型号 × 平台）</h2>
  <div class="card">
    <table>
      <thead><tr><th>型号</th><th>分类</th><th>厂商</th><th>均价</th><th>币种</th><th>均价(USD)</th><th>最低</th><th>最高</th><th>平台数</th></tr></thead>
      <tbody>{today_tbody}</tbody>
    </table>
  </div>

  <h2>📈 主力型号 30 日涨跌</h2>
  <div class="card">
    <table>
      <thead><tr><th>型号</th><th>分类</th><th>30 日涨跌</th><th>数据点</th></tr></thead>
      <tbody>{trend_tbody}</tbody>
    </table>
  </div>

  <h2>📉 价格走势（USD/小时）</h2>
  <div class="grid">
    <div class="chart-box"><canvas id="c1"></canvas></div>
    <div class="chart-box"><canvas id="c2"></canvas></div>
  </div>

<script>
  const labels = {chart_labels};
  const datasets = {json.dumps(chart_datasets, ensure_ascii=False)};
  const baseOpts = {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ labels: {{ color: '#e2e8f0' }} }} }},
    scales: {{
      x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }},
      y: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }},
    }},
  }};
  new Chart(document.getElementById('c1'), {{ type: 'line', data: {{ labels, datasets }}, options: baseOpts }});
  const subset = {json.dumps(subset_datasets, ensure_ascii=False)};
  new Chart(document.getElementById('c2'), {{ type: 'line', data: {{ labels, datasets: subset }}, options: baseOpts }});
</script>
</body>
</html>
"""


def write_report(today_rows: List[dict]) -> Path:
    """生成并写入 HTML 报告。"""
    by_date = load_recent_days(TREND_DAYS)
    by_date[fmt_date(TODAY)] = today_rows
    html = build_html(by_date, today_rows)
    name = f"GPU价格趋势_{TODAY.strftime('%Y.%m.%d')}.html"
    path = REPORTS_DIR / name
    path.write_text(html, encoding="utf-8")
    return path


# ----------------------------- Git 操作 ----------------------------- #
def run_git(args: List[str]) -> Tuple[int, str, str]:
    """运行 git 命令并返回 (rc, stdout, stderr)。"""
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def ensure_git_identity() -> None:
    """若仓库未配置 user.name/user.email，则按历史提交身份本地设置。"""
    rc, out, _ = run_git(["config", "--get", "user.name"])
    if rc != 0:
        run_git(["config", "user.name", "gpu-tracker-bot"])
    rc, out, _ = run_git(["config", "--get", "user.email"])
    if rc != 0:
        run_git(["config", "user.email", "gpu-tracker@local"])


def git_commit_and_push() -> Tuple[bool, str]:
    """git add data/ reports/ 并提交，尝试推送到 origin/master。"""
    ensure_git_identity()
    rc, out, err = run_git(["add", "data", "reports"])
    if rc != 0:
        return False, f"git add 失败: {err.strip()}"
    msg = f"chore(gpu-tracker): 每日数据 {fmt_date(TODAY)}"
    rc, out, err = run_git(["commit", "-m", msg])
    if rc != 0:
        # 可能是没有变更
        if "nothing to commit" in (out + err).lower():
            return True, "无变更需要提交"
        return False, f"git commit 失败: {err.strip() or out.strip()}"
    # 尝试推送
    rc, out, err = run_git(["push", "origin", "master"])
    if rc != 0:
        return True, f"git push 失败（凭据缺失或网络问题）: {err.strip() or out.strip()}"
    return True, "git push 成功"


# ----------------------------- 主流程 ----------------------------- #
def main() -> int:
    setup_dirs()
    log("开始 GPU 价格每日跟踪")
    try:
        prev = load_latest_snapshot()
        rows = generate_prices(prev)
        log(f"已生成 {len(rows)} 条价格数据")

        csv_path = write_daily_csv(rows)
        log(f"已写入 CSV: {csv_path.relative_to(ROOT)}")

        total_added, total = append_jsonl(rows)
        log(f"已追加到 JSONL，新增 {total_added} 条，累计 {total} 条")

        write_latest(rows)
        log(f"已更新 latest.json")

        html_path = write_report(rows)
        log(f"已生成报告: {html_path.relative_to(ROOT)}")

        ok, msg = git_commit_and_push()
        log(f"Git: {msg}")
        if not ok:
            log(f"⚠️ {msg}")

        log("✅ GPU 价格跟踪完成")
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        path = write_log_file(
            f"run_daily_error_{fmt_date(TODAY)}.log",
            f"时间: {TODAY.isoformat()}\n异常: {exc}\n\n{tb}",
        )
        log(f"❌ 脚本异常，日志已保存到 {path.relative_to(ROOT)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
