#!/usr/bin/env python3
"""GPU 算力价格每日跟踪脚本。

执行流程：
1) 抓取 14 个 GPU 型号 × 8 个平台的算力租赁价格（mock 数据源）
2) 写入 data/daily/YYYY/MM/YYYY-MM-DD.csv 与 data/jsonl/prices.jsonl
3) 更新 data/latest.json
4) 生成 reports/GPU价格趋势_YYYY.MM.DD.html（含 Chart.js 图表）
5) git add data/ reports/ 并提交
6) git push 到 origin/master（凭据缺失则记录失败）
异常处理：所有异常统一写入 logs/ 目录日志并重新抛出。
"""
from __future__ import annotations

import csv
import json
import logging
import random
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# ---------- 路径常量 ----------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DAILY_DIR = DATA_DIR / "daily"
JSONL_PATH = DATA_DIR / "jsonl" / "prices.jsonl"
LATEST_PATH = DATA_DIR / "latest.json"
REPORTS_DIR = ROOT / "reports"
LOGS_DIR = ROOT / "logs"

# 14 个目标型号（来自用户指定清单）
MODELS = [
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

# 8 个目标平台（国际三家 + 国内五家）
PLATFORMS = [
    ("RunPod", "USD"),
    ("Vast.ai", "USD"),
    ("AWS", "USD"),
    ("阿里云", "CNY"),
    ("腾讯云", "CNY"),
    ("华为云", "CNY"),
    ("AutoDL", "CNY"),
    ("极智算", "CNY"),
]

USD_CNY = 7.18
SOURCE = "mock"


# ---------- 日志 ----------
def setup_logger() -> logging.Logger:
    """配置日志：同时输出到 stdout 与 logs/run_daily_YYYYMMDD_HHMMSS.log。"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"run_daily_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = logging.getLogger("gpu_tracker")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.handlers.clear()
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


# ---------- 数据生成 ----------
def load_baseline(logger: logging.Logger) -> dict[tuple[str, str, str], float]:
    """以 data/latest.json 为基准价，缺失时回退到内置合理价位。"""
    baseline: dict[tuple[str, str, str], float] = {}

    # 内置参考价（USD/小时 或 CNY/小时，按平台原始货币）
    default = {
        ("H100", "RunPod"): 2.55, ("H100", "Vast.ai"): 2.35, ("H100", "AWS"): 5.10,
        ("H100", "阿里云"): 31.5, ("H100", "腾讯云"): 27.0, ("H100", "华为云"): 25.5,
        ("H100", "AutoDL"): 21.5, ("H100", "极智算"): 19.0,
        ("H200", "RunPod"): 3.70, ("H200", "Vast.ai"): 3.00, ("H200", "AWS"): 6.20,
        ("H200", "阿里云"): 39.0, ("H200", "腾讯云"): 38.5, ("H200", "华为云"): 32.5,
        ("H200", "AutoDL"): 27.0, ("H200", "极智算"): 24.0,
        ("B200", "RunPod"): 4.45, ("B200", "Vast.ai"): 4.10, ("B200", "AWS"): 9.20,
        ("B200", "阿里云"): 54.0, ("B200", "腾讯云"): 52.5, ("B200", "华为云"): 44.5,
        ("B200", "AutoDL"): 36.0, ("B200", "极智算"): 36.5,
        ("GB200", "RunPod"): 6.60, ("GB200", "Vast.ai"): 6.20, ("GB200", "AWS"): 11.60,
        ("GB200", "阿里云"): 71.5, ("GB200", "腾讯云"): 70.5, ("GB200", "华为云"): 65.0,
        ("GB200", "AutoDL"): 50.5, ("GB200", "极智算"): 47.5,
        ("GB300", "RunPod"): 7.80, ("GB300", "Vast.ai"): 7.40, ("GB300", "AWS"): 15.50,
        ("GB300", "阿里云"): 87.0, ("GB300", "腾讯云"): 79.5, ("GB300", "华为云"): 76.5,
        ("GB300", "AutoDL"): 58.0, ("GB300", "极智算"): 56.0,
        ("A100-80G", "RunPod"): 1.75, ("A100-80G", "Vast.ai"): 1.45, ("A100-80G", "AWS"): 3.20,
        ("A100-80G", "阿里云"): 19.5, ("A100-80G", "腾讯云"): 16.5, ("A100-80G", "华为云"): 16.0,
        ("A100-80G", "AutoDL"): 13.5, ("A100-80G", "极智算"): 12.5,
        ("L40S", "RunPod"): 1.17, ("L40S", "Vast.ai"): 1.07, ("L40S", "AWS"): 2.10,
        ("L40S", "阿里云"): 13.0, ("L40S", "腾讯云"): 14.0, ("L40S", "华为云"): 13.0,
        ("L40S", "AutoDL"): 9.0, ("L40S", "极智算"): 9.1,
        ("A6000", "RunPod"): 0.74, ("A6000", "Vast.ai"): 0.72, ("A6000", "AWS"): 1.57,
        ("A6000", "阿里云"): 9.2, ("A6000", "腾讯云"): 8.1, ("A6000", "华为云"): 8.6,
        ("A6000", "AutoDL"): 5.7, ("A6000", "极智算"): 5.5,
        ("RTX 4090", "RunPod"): 0.46, ("RTX 4090", "Vast.ai"): 0.41, ("RTX 4090", "AWS"): 0.87,
        ("RTX 4090", "阿里云"): 5.5, ("RTX 4090", "腾讯云"): 4.7, ("RTX 4090", "华为云"): 5.0,
        ("RTX 4090", "AutoDL"): 3.7, ("RTX 4090", "极智算"): 3.4,
        ("RTX 3090", "RunPod"): 0.26, ("RTX 3090", "Vast.ai"): 0.23, ("RTX 3090", "AWS"): 0.47,
        ("RTX 3090", "阿里云"): 2.9, ("RTX 3090", "腾讯云"): 2.7, ("RTX 3090", "华为云"): 2.5,
        ("RTX 3090", "AutoDL"): 2.0, ("RTX 3090", "极智算"): 2.0,
        ("Ascend 910B", "RunPod"): 10.2, ("Ascend 910B", "Vast.ai"): 10.2, ("Ascend 910B", "AWS"): 21.5,
        ("Ascend 910B", "阿里云"): 15.7, ("Ascend 910B", "腾讯云"): 15.5, ("Ascend 910B", "华为云"): 14.9,
        ("Ascend 910B", "AutoDL"): 11.5, ("Ascend 910B", "极智算"): 10.6,
        ("Ascend 910C", "RunPod"): 15.9, ("Ascend 910C", "Vast.ai"): 15.2, ("Ascend 910C", "AWS"): 30.1,
        ("Ascend 910C", "阿里云"): 24.9, ("Ascend 910C", "腾讯云"): 22.8, ("Ascend 910C", "华为云"): 22.5,
        ("Ascend 910C", "AutoDL"): 16.2, ("Ascend 910C", "极智算"): 15.8,
        ("海光 DCU", "RunPod"): 6.6, ("海光 DCU", "Vast.ai"): 6.6, ("海光 DCU", "AWS"): 11.4,
        ("海光 DCU", "阿里云"): 10.5, ("海光 DCU", "腾讯云"): 10.2, ("海光 DCU", "华为云"): 9.8,
        ("海光 DCU", "AutoDL"): 7.3, ("海光 DCU", "极智算"): 6.8,
        ("寒武纪 MLU", "RunPod"): 7.6, ("寒武纪 MLU", "Vast.ai"): 7.1, ("寒武纪 MLU", "AWS"): 14.7,
        ("寒武纪 MLU", "阿里云"): 11.6, ("寒武纪 MLU", "腾讯云"): 11.8, ("寒武纪 MLU", "华为云"): 11.4,
        ("寒武纪 MLU", "AutoDL"): 8.4, ("寒武纪 MLU", "极智算"): 7.6,
    }

    if LATEST_PATH.exists():
        try:
            with LATEST_PATH.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            for row in payload.get("rows", []):
                key = (row["model"], row["platform"], row["currency"])
                baseline[key] = float(row["price"])
            logger.info("已从 latest.json 加载 %d 条基准价", len(baseline))
        except Exception as exc:  # noqa: BLE001
            logger.warning("latest.json 解析失败：%s，将使用默认价位", exc)

    # 用默认值补齐缺失组合
    for (model, platform), price in default.items():
        currency = "USD" if platform in {"RunPod", "Vast.ai", "AWS"} else "CNY"
        key = (model, platform, currency)
        baseline.setdefault(key, price)
    return baseline


def fetch_prices(logger: logging.Logger, date_str: str) -> list[dict]:
    """生成 14 × 8 = 112 条价格数据（mock，相对昨日 ±5% 抖动）。"""
    baseline = load_baseline(logger)
    rows: list[dict] = []
    rng = random.Random(hash(date_str) & 0xFFFFFFFF)
    for model, segment, vendor in MODELS:
        for platform, currency in PLATFORMS:
            base = baseline.get((model, platform, currency))
            if base is None:
                logger.warning("缺基准价：%s/%s/%s", model, platform, currency)
                continue
            # ±5% 抖动，保留 4 位小数
            price = round(base * (1 + rng.uniform(-0.05, 0.05)), 4)
            rows.append({
                "date": date_str,
                "model": model,
                "segment": segment,
                "vendor": vendor,
                "platform": platform,
                "price": price,
                "currency": currency,
                "usd_cny": USD_CNY,
                "source": SOURCE,
            })
    logger.info("已生成 %d 条价格数据", len(rows))
    return rows


# ---------- 持久化 ----------
def write_csv(rows: list[dict], date_str: str, logger: logging.Logger) -> Path:
    """写入 data/daily/YYYY/MM/YYYY-MM-DD.csv。"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    out = DAILY_DIR / f"{dt.year:04d}" / f"{dt.month:02d}" / f"{date_str}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = ["date", "model", "segment", "vendor", "platform", "price",
              "currency", "usd_cny", "source"]
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("已写入 CSV：%s", out.relative_to(ROOT))
    return out


def write_jsonl(rows: list[dict], logger: logging.Logger) -> Path:
    """追加写入 data/jsonl/prices.jsonl。"""
    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JSONL_PATH.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    logger.info("已追加 %d 行到 JSONL：%s", len(rows), JSONL_PATH.relative_to(ROOT))
    return JSONL_PATH


def write_latest(rows: list[dict], date_str: str, logger: logging.Logger) -> Path:
    """更新 data/latest.json（最新一日的快照）。"""
    payload = {
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "usd_cny": USD_CNY,
        "row_count": len(rows),
        "models": [m[0] for m in MODELS],
        "platforms": [p[0] for p in PLATFORMS],
        "rows": rows,
    }
    LATEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LATEST_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info("已更新 latest.json（%d 行）", len(rows))
    return LATEST_PATH


# ---------- 30 日趋势（用于报告） ----------
def load_history(days: int = 30) -> dict[str, list[tuple[str, float]]]:
    """读取近 N 天 CSV，输出 model -> [(date, usd_price), ...]。"""
    series: dict[str, list[tuple[str, float]]] = defaultdict(list)
    if not DAILY_DIR.exists():
        return series
    files = sorted(DAILY_DIR.rglob("*.csv"))[-days:]
    for f in files:
        try:
            date_str = f.stem
            with f.open("r", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    price = float(row["price"])
                    if row["currency"] == "CNY":
                        price = price / USD_CNY
                    series[row["model"]].append((date_str, price))
        except Exception:  # noqa: BLE001
            continue
    for m in series:
        series[m].sort(key=lambda x: x[0])
    return series


# ---------- 报告 ----------
def build_report(rows: list[dict], date_str: str, logger: logging.Logger) -> Path:
    """生成 reports/GPU价格趋势_YYYY.MM.DD.html。"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"GPU价格趋势_{date_str.replace('-', '.')}.html"

    # 当日价格水位（型号聚合：均价/最低/最高/平台数）
    bucket: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        bucket[r["model"]].append(r)
    today_rows: list[tuple[str, str, str, float, str, float, float, float, int]] = []
    for model, segment, vendor in MODELS:
        items = bucket.get(model, [])
        if not items:
            continue
        prices_native = [it["price"] for it in items]
        prices_usd = [(it["price"] / USD_CNY) if it["currency"] == "CNY" else it["price"]
                      for it in items]
        avg_native = sum(prices_native) / len(prices_native)
        avg_usd = sum(prices_usd) / len(prices_usd)
        cur = items[0]["currency"]
        today_rows.append((
            model, segment, vendor, avg_native, cur, avg_usd,
            min(prices_native), max(prices_native), len(items),
        ))

    # 30 日涨跌
    history = load_history(30)
    trend_rows: list[tuple[str, str, float, int]] = []
    for model, segment, _vendor in MODELS:
        s = history.get(model, [])
        if len(s) < 2:
            continue
        first, last = s[0][1], s[-1][1]
        pct = (last - first) / first * 100 if first else 0.0
        trend_rows.append((model, segment, pct, len(s)))
    trend_rows.sort(key=lambda x: x[2], reverse=True)

    # 瓶颈信号
    high_end = [r for r in trend_rows if r[1] in {"国际-高端", "国际-旗舰"}]
    avg_high = sum(r[2] for r in high_end) / len(high_end) if high_end else 0
    h100_usd = next((r[5] for r in today_rows if r[0] == "H100"), 0)
    a910c_usd = next((r[5] for r in today_rows if r[0] == "Ascend 910C"), 0)
    premium = a910c_usd / h100_usd if h100_usd else 0
    if avg_high > 3:
        signal = f"🔴 算力瓶颈仍在（高端型号 30 日平均 {avg_high:+.2f}%）"
    elif avg_high < -1:
        signal = f"🟢 算力瓶颈缓解（高端型号 30 日平均 {avg_high:+.2f}%）"
    else:
        signal = f"🟡 算力市场平稳（高端型号 30 日平均 {avg_high:+.2f}%）"
    if premium > 1:
        signal += f" &nbsp;|&nbsp; 国产溢价: 910C / H100 = {premium:.2f}×（国产溢价）"
    else:
        signal += f" &nbsp;|&nbsp; 国产溢价: 910C / H100 = {premium:.2f}×（国产折价）"

    # Chart.js 数据
    chart_labels = sorted({d for s in history.values() for d, _ in s})
    chart_datasets: list[dict] = []
    colors = ["#ef4444", "#f97316", "#eab308", "#22c55e", "#06b6d4",
              "#3b82f6", "#8b5cf6", "#ec4899", "#14b8a6", "#a3a3a3",
              "#dc2626", "#0ea5e9", "#7c3aed", "#10b981"]
    for (model, _seg, _v), color in zip(MODELS, colors):
        s = history.get(model, [])
        by_date = dict(s)
        # 取该型号全平台当日均价
        last_data = next((r for r in today_rows if r[0] == model), None)
        last_val = last_data[5] if last_data else 0
        full = [by_date.get(d, last_val if d == date_str else None) for d in chart_labels]
        chart_datasets.append({
            "label": model, "data": full, "borderColor": color,
            "backgroundColor": "rgba(0,0,0,0)", "tension": 0.25, "spanGaps": True,
        })

    # HTML
    def fmt_rows(rs: Iterable, cols: int) -> str:
        return "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r[:cols]) + "</tr>" for r in rs)

    today_html = "".join(
        f"<tr><td>{m}</td><td>{s}</td><td>{v}</td><td>{avg:.4f}</td>"
        f"<td>{cur}</td><td>{au:.4f}</td><td>{lo:.4f}</td><td>{hi:.4f}</td><td>{n}</td></tr>"
        for m, s, v, avg, cur, au, lo, hi, n in today_rows
    )
    trend_html = "".join(
        f"<tr><td>{m}</td><td>{s}</td>"
        f"<td class='{'up' if p > 0 else 'down'}'>{p:+.2f}%</td><td>{n}</td></tr>"
        for m, s, p, n in trend_rows
    )
    datasets_json = json.dumps(chart_datasets, ensure_ascii=False)

    html = f"""<!doctype html>
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
  <div class="meta">数据日期: <b>{date_str}</b> · 汇率 USD/CNY = {USD_CNY} · 数据源: {SOURCE} · 条数: {len(rows)}</div>

  <div class="card">
    <h2>🚦 AI 基建瓶颈信号</h2>
    <div class="signal">{signal}</div>
  </div>

  <h2>📊 今日价格水位（型号 × 平台）</h2>
  <div class="card">
    <table>
      <thead><tr><th>型号</th><th>分类</th><th>厂商</th><th>均价</th><th>币种</th><th>均价(USD)</th><th>最低</th><th>最高</th><th>平台数</th></tr></thead>
      <tbody>{today_html}</tbody>
    </table>
  </div>

  <h2>📈 主力型号 30 日涨跌</h2>
  <div class="card">
    <table>
      <thead><tr><th>型号</th><th>分类</th><th>30 日涨跌</th><th>数据点</th></tr></thead>
      <tbody>{trend_html}</tbody>
    </table>
  </div>

  <h2>📉 价格走势（USD/小时）</h2>
  <div class="grid">
    <div class="chart-box"><canvas id="c1"></canvas></div>
    <div class="chart-box"><canvas id="c2"></canvas></div>
  </div>

<script>
  const labels = {json.dumps(chart_labels)};
  const datasets = {datasets_json};
  const baseOpts = {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ labels: {{ color: '#e2e8f0' }} }} }},
    scales: {{
      x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }},
      y: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }},
    }},
  }};
  new Chart(document.getElementById('c1'), {{ type: 'line', data: {{ labels, datasets }}, options: baseOpts }});
  const subset = datasets.filter(d => ['RTX 4090','RTX 3090','L40S','A6000','Ascend 910B','Ascend 910C','海光 DCU','寒武纪 MLU'].includes(d.label));
  new Chart(document.getElementById('c2'), {{ type: 'line', data: {{ labels, datasets: subset }}, options: baseOpts }});
</script>
</body>
</html>
"""
    out.write_text(html, encoding="utf-8")
    logger.info("已生成报告：%s（%.1f KB）", out.relative_to(ROOT), out.stat().st_size / 1024)
    return out


# ---------- Git ----------
def git_run(args: list[str], logger: logging.Logger) -> tuple[int, str, str]:
    """执行 git 命令并返回 (rc, stdout, stderr)。"""
    try:
        cp = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False,
        )
        return cp.returncode, cp.stdout.strip(), cp.stderr.strip()
    except FileNotFoundError:
        return 127, "", "git 未安装"


def git_commit_and_push(logger: logging.Logger) -> None:
    """git add data/ reports/ 并提交；推送失败仅记录。"""
    rc, out, err = git_run(["add", "data/", "reports/"], logger)
    if rc != 0:
        logger.error("git add 失败：%s", err)
        return
    logger.info("git add 完成：%s", out or "无输出")

    msg = f"chore(gpu-tracker): 每日数据 {datetime.now().strftime('%Y-%m-%d')}"
    rc, out, err = git_run(["commit", "-m", msg], logger)
    if rc == 0:
        logger.info("git commit 完成：%s", out.splitlines()[-1] if out else msg)
    elif "nothing to commit" in (out + err):
        logger.info("无变更需要提交")
        return
    else:
        logger.error("git commit 失败：%s / %s", out, err)
        return

    rc, out, err = git_run(["push", "origin", "master"], logger)
    if rc == 0:
        logger.info("git push 完成：%s", out)
    else:
        logger.warning("git push 失败（凭据可能缺失，不影响本地数据）：%s / %s", out, err)


# ---------- Main ----------
def main() -> int:
    logger = setup_logger()
    date_str = datetime.now().strftime("%Y-%m-%d")
    logger.info("=== GPU 算力价格每日跟踪开始：%s ===", date_str)
    try:
        rows = fetch_prices(logger, date_str)
        if not rows:
            raise RuntimeError("未生成任何价格数据，请检查模型/平台配置")
        write_csv(rows, date_str, logger)
        write_jsonl(rows, logger)
        write_latest(rows, date_str, logger)
        build_report(rows, date_str, logger)
        git_commit_and_push(logger)
        logger.info("=== 完成 ===")
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.error("脚本异常：%s", exc)
        logger.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
