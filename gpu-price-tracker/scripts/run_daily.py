#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GPU 算力价格每日跟踪
- 抓取国际/国产 GPU 租赁价格
- 落盘 CSV / JSONL / latest.json
- 生成 HTML 趋势报告
- 提交并尝试推送到 origin/master
"""
import asyncio
import csv
import json
import logging
import os
import random
import subprocess
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

# ---------- 路径与常量 ----------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DAILY_DIR = DATA_DIR / "daily"
JSONL_PATH = DATA_DIR / "jsonl" / "prices.jsonl"
LATEST_PATH = DATA_DIR / "latest.json"
REPORTS_DIR = ROOT / "reports"
LOGS_DIR = ROOT / "logs"

# 关注的主力型号
MODELS: List[Dict[str, str]] = [
    {"model": "H100", "segment": "国际-高端", "vendor": "NVIDIA"},
    {"model": "H200", "segment": "国际-高端", "vendor": "NVIDIA"},
    {"model": "B200", "segment": "国际-高端", "vendor": "NVIDIA"},
    {"model": "GB200", "segment": "国际-旗舰", "vendor": "NVIDIA"},
    {"model": "GB300", "segment": "国际-旗舰", "vendor": "NVIDIA"},
    {"model": "A100-80G", "segment": "国际-高端", "vendor": "NVIDIA"},
    {"model": "L40S", "segment": "国际-中端", "vendor": "NVIDIA"},
    {"model": "A6000", "segment": "国际-中端", "vendor": "NVIDIA"},
    {"model": "RTX 4090", "segment": "消费级-旗舰", "vendor": "NVIDIA"},
    {"model": "RTX 3090", "segment": "消费级-高端", "vendor": "NVIDIA"},
    {"model": "Ascend 910B", "segment": "国产-高端", "vendor": "华为昇腾"},
    {"model": "Ascend 910C", "segment": "国产-旗舰", "vendor": "华为昇腾"},
    {"model": "海光 DCU", "segment": "国产-中端", "vendor": "海光信息"},
    {"model": "寒武纪 MLU", "segment": "国产-中端", "vendor": "寒武纪"},
]

# 平台映射（mock 基线 USD/GPU/小时）
PLATFORMS = ["RunPod", "Vast.ai", "AWS", "阿里云", "腾讯云", "华为云", "AutoDL", "极智算"]

# 型号基线价（USD/小时）+ 日波动范围
BASE_PRICE_USD = {
    "H100": 3.20, "H200": 4.10, "B200": 5.50, "GB200": 7.80, "GB300": 9.50,
    "A100-80G": 1.95, "L40S": 1.45, "A6000": 0.95,
    "RTX 4090": 0.55, "RTX 3090": 0.30,
    "Ascend 910B": 1.80, "Ascend 910C": 2.60,
    "海光 DCU": 1.10, "寒武纪 MLU": 1.25,
}

# 平台倍率（不同云相对基准）
PLATFORM_MULT = {
    "RunPod": 0.85, "Vast.ai": 0.78, "AWS": 1.55,
    "阿里云": 1.30, "腾讯云": 1.25, "华为云": 1.20,
    "AutoDL": 0.90, "极智算": 0.88,
}

USDCNY = float(os.environ.get("DEFAULT_USDCNY", "7.18"))


# ---------- 日志 ----------
def setup_logger() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"run_{datetime.now().strftime('%Y-%m-%d')}.log"
    logger = logging.getLogger("gpu-tracker")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


LOG = setup_logger()


# ---------- 抓取（mock 降级） ----------
async def fetch_prices() -> List[Dict[str, Any]]:
    """
    抓取各平台价格。无 API 凭据或网络失败时降级到 mock 数据。
    返回：[ {date, model, segment, vendor, platform, price_usd, price_cny, currency, source} ]
    """
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    rows: List[Dict[str, Any]] = []

    # 真实抓取占位（无 key 立即失败并走 mock，与 skill 一致）
    api_keys = {k: os.environ.get(k) for k in ("VAST_API_KEY", "RUNPOD_API_KEY", "AUTODL_TOKEN")}
    if not any(api_keys.values()):
        LOG.info("未配置任何数据源 API Key，使用 mock 数据生成")
    else:
        LOG.info("检测到 API Key 配置，但当前实现尚未直连平台，将以 mock 基线生成")

    # 日内波动：基线 ±8%
    rng = random.Random(int(now.strftime("%Y%m%d")))
    for m in MODELS:
        base = BASE_PRICE_USD.get(m["model"], 1.0)
        for plat in PLATFORMS:
            mult = PLATFORM_MULT.get(plat, 1.0)
            jitter = 1.0 + rng.uniform(-0.08, 0.08)
            price_usd = round(base * mult * jitter, 4)
            # 国产平台以人民币计价更贴近用户
            is_cn_plat = plat in ("阿里云", "腾讯云", "华为云", "AutoDL", "极智算")
            currency = "CNY" if (is_cn_plat or "国产" in m["segment"]) else "USD"
            if currency == "CNY":
                price = round(price_usd * USDCNY, 4)
            else:
                price = price_usd
            rows.append({
                "date": today,
                "model": m["model"],
                "segment": m["segment"],
                "vendor": m["vendor"],
                "platform": plat,
                "price": price,
                "currency": currency,
                "usd_cny": USDCNY,
                "source": "mock",
            })
    LOG.info(f"共生成 {len(rows)} 条价格记录（{len(MODELS)} 型号 × {len(PLATFORMS)} 平台）")
    return rows


# ---------- 落盘 ----------
def write_outputs(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "jsonl").mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    today = rows[0]["date"]
    dt = datetime.strptime(today, "%Y-%m-%d")
    csv_dir = DAILY_DIR / dt.strftime("%Y") / dt.strftime("%m")
    csv_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_dir / f"{today}.csv"

    fieldnames = ["date", "model", "segment", "vendor", "platform",
                  "price", "currency", "usd_cny", "source"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    LOG.info(f"已写入 CSV: {csv_path}")

    # 追加 JSONL
    with open(JSONL_PATH, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    LOG.info(f"已追加 JSONL: {JSONL_PATH}")

    # 更新 latest.json
    latest = {
        "date": today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "usd_cny": USDCNY,
        "row_count": len(rows),
        "models": sorted({r["model"] for r in rows}),
        "platforms": sorted({r["platform"] for r in rows}),
        "rows": rows,
    }
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)
    LOG.info(f"已更新 latest.json: {LATEST_PATH}")
    return {"csv": str(csv_path), "jsonl": str(JSONL_PATH), "latest": str(LATEST_PATH)}


# ---------- 历史汇总 & 报告 ----------
def load_history(days: int = 30) -> List[Dict[str, Any]]:
    if not JSONL_PATH.exists():
        return []
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    with open(JSONL_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("date", "") >= cutoff:
                out.append(r)
    return out


def build_report(history: List[Dict[str, Any]], latest_rows: List[Dict[str, Any]]) -> Path:
    today = latest_rows[0]["date"]
    dt = datetime.strptime(today, "%Y-%m-%d")
    out_path = REPORTS_DIR / f"GPU价格趋势_{dt.strftime('%Y.%m.%d')}.html"

    # 按型号汇总今日价（取各平台均价，按 USD 折算）
    by_model: Dict[str, List[Dict[str, Any]]] = {}
    for r in latest_rows:
        by_model.setdefault(r["model"], []).append(r)

    # 30 天价格序列（USD/小时，按型号+平台）
    series_by_model: Dict[str, List[Dict[str, Any]]] = {}
    for r in history:
        usd = r["price"] if r["currency"] == "USD" else r["price"] / r["usd_cny"]
        series_by_model.setdefault(r["model"], []).append({"date": r["date"], "price": round(usd, 4)})

    # 计算 30 日涨跌（首末价差百分比）
    trend_rows = []
    for model, pts in series_by_model.items():
        pts_sorted = sorted(pts, key=lambda x: x["date"])
        if len(pts_sorted) >= 2:
            first, last = pts_sorted[0]["price"], pts_sorted[-1]["price"]
            chg = (last - first) / first * 100 if first else 0.0
        else:
            chg = 0.0
        seg = next((m["segment"] for m in MODELS if m["model"] == model), "其他")
        trend_rows.append({"model": model, "segment": seg, "change_30d_pct": round(chg, 2),
                           "points": pts_sorted})

    today_table = []
    for model, rs in by_model.items():
        prices = [r["price"] for r in rs]
        avg_price = round(sum(prices) / len(prices), 4)
        ccy = rs[0]["currency"]
        avg_usd = round(avg_price / USDCNY, 4) if ccy == "CNY" else avg_price
        today_table.append({
            "model": model,
            "segment": rs[0]["segment"],
            "vendor": rs[0]["vendor"],
            "avg_price": avg_price,
            "currency": ccy,
            "avg_price_usd": avg_usd,
            "min_price": round(min(prices), 4),
            "max_price": round(max(prices), 4),
            "platforms": len(rs),
        })

    # 30 日涨跌表行（HTML 字符串，避免 f-string 内嵌三目嵌套的解析问题）
    trend_rows_html = []
    for t in sorted(trend_rows, key=lambda x: -x["change_30d_pct"]):
        chg = t["change_30d_pct"]
        cls = "up" if chg > 0 else ("down" if chg < 0 else "")
        trend_rows_html.append(
            f"<tr><td>{t['model']}</td><td>{t['segment']}</td>"
            f"<td class='{cls}'>{chg:+.2f}%</td>"
            f"<td>{len(t['points'])}</td></tr>"
        )
    trend_rows_str = "".join(trend_rows_html)

    # 今日表行
    today_rows_html = []
    for r in today_table:
        today_rows_html.append(
            f"<tr><td>{r['model']}</td><td>{r['segment']}</td><td>{r['vendor']}</td>"
            f"<td>{r['avg_price']}</td><td>{r['currency']}</td><td>{r['avg_price_usd']}</td>"
            f"<td>{r['min_price']}</td><td>{r['max_price']}</td><td>{r['platforms']}</td></tr>"
        )
    today_rows_str = "".join(today_rows_html)

    # 瓶颈信号：高端型号（>=H100）若有 ≥3 个数据点且平均价格上行 > 3%
    high_end = [t for t in trend_rows if t["model"] in ("H100", "H200", "B200", "GB200", "GB300", "A100-80G")]
    up_models = [t for t in high_end if t["change_30d_pct"] > 3]
    bottleneck = "🔴 算力瓶颈仍在（高端型号普涨 > 3%）" if up_models else "🟢 高端价格企稳，瓶颈信号减弱"

    # 国产溢价
    h100_today = next((t for t in today_table if t["model"] == "H100"), None)
    ascend_today = next((t for t in today_table if t["model"] == "Ascend 910C"), None)
    premium_text = "—"
    if h100_today and ascend_today:
        ratio = ascend_today["avg_price_usd"] / h100_today["avg_price_usd"]
        premium_text = f"910C / H100 = {ratio:.2f}×（{'国产溢价' if ratio > 1 else '国产折价'}）"

    # 构造 Chart.js 数据
    labels = sorted({p["date"] for pts in series_by_model.values() for p in pts})
    chart_datasets = []
    palette = ["#ef4444", "#f97316", "#eab308", "#22c55e", "#06b6d4",
               "#3b82f6", "#8b5cf6", "#ec4899", "#14b8a6", "#a3a3a3",
               "#dc2626", "#0ea5e9", "#7c3aed", "#10b981"]
    color_iter = iter(palette)
    for t in trend_rows:
        by_date = {p["date"]: p["price"] for p in t["points"]}
        data = [by_date.get(d) for d in labels]
        chart_datasets.append({
            "label": t["model"],
            "data": data,
            "borderColor": next(color_iter, "#666"),
            "backgroundColor": "rgba(0,0,0,0)",
            "tension": 0.25,
            "spanGaps": True,
        })

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>GPU 算力价格趋势 {today}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
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
  <div class="meta">数据日期: <b>{today}</b> · 汇率 USD/CNY = {USDCNY} · 数据源: {latest_rows[0]['source']} · 条数: {len(latest_rows)}</div>

  <div class="card">
    <h2>🚦 AI 基建瓶颈信号</h2>
    <div class="signal">{bottleneck} &nbsp;|&nbsp; 国产溢价: {premium_text}</div>
  </div>

  <h2>📊 今日价格水位（型号 × 平台）</h2>
  <div class="card">
    <table>
      <thead><tr><th>型号</th><th>分类</th><th>厂商</th><th>均价</th><th>币种</th><th>均价(USD)</th><th>最低</th><th>最高</th><th>平台数</th></tr></thead>
      <tbody>
        {today_rows_str}
      </tbody>
    </table>
  </div>

  <h2>📈 主力型号 30 日涨跌</h2>
  <div class="card">
    <table>
      <thead><tr><th>型号</th><th>分类</th><th>30 日涨跌</th><th>数据点</th></tr></thead>
      <tbody>
        {trend_rows_str}
      </tbody>
    </table>
  </div>

  <h2>📉 价格走势（USD/小时）</h2>
  <div class="grid">
    <div class="chart-box"><canvas id="c1"></canvas></div>
    <div class="chart-box"><canvas id="c2"></canvas></div>
  </div>

<script>
  const labels = {json.dumps(labels, ensure_ascii=False)};
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
  // 第二张图：消费级+国产子集
  const subset = datasets.filter(d => ['RTX 4090','RTX 3090','L40S','A6000','Ascend 910B','Ascend 910C','海光 DCU','寒武纪 MLU'].includes(d.label));
  new Chart(document.getElementById('c2'), {{ type: 'line', data: {{ labels, datasets: subset }}, options: baseOpts }});
</script>
</body>
</html>
"""
    out_path.write_text(html, encoding="utf-8")
    LOG.info(f"已生成 HTML 报告: {out_path} ({out_path.stat().st_size} bytes)")
    return out_path


# ---------- Git ----------
def git_commit_and_push(commit_msg: str) -> None:
    try:
        subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                       cwd=ROOT, check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        LOG.warning("当前目录不是 git 仓库，跳过提交。")
        return

    # 配置本地身份（仅在缺失时设置，不影响用户全局配置）
    try:
        name = subprocess.run(["git", "config", "user.name"], cwd=ROOT,
                              capture_output=True, text=True).stdout.strip()
        email = subprocess.run(["git", "config", "user.email"], cwd=ROOT,
                               capture_output=True, text=True).stdout.strip()
        if not name:
            subprocess.run(["git", "config", "user.name", "gpu-tracker-bot"],
                           cwd=ROOT, check=False)
        if not email:
            subprocess.run(["git", "config", "user.email", "gpu-tracker@local"],
                           cwd=ROOT, check=False)
    except Exception as e:
        LOG.warning(f"配置 git 身份时异常（非致命）: {e}")

    add = subprocess.run(["git", "add", "-A", "data/", "reports/"],
                         cwd=ROOT, capture_output=True, text=True)
    if add.returncode != 0:
        LOG.warning(f"git add 失败: {add.stderr.strip()}")
        return

    diff = subprocess.run(["git", "diff", "--cached", "--quiet"],
                          cwd=ROOT, capture_output=True)
    if diff.returncode == 0:
        LOG.info("无变更需要提交")
        return

    commit = subprocess.run(["git", "commit", "-m", commit_msg],
                            cwd=ROOT, capture_output=True, text=True)
    if commit.returncode != 0:
        LOG.warning(f"git commit 失败: {commit.stderr.strip()}")
        return
    LOG.info(f"git commit 成功: {commit_msg}")

    push = subprocess.run(["git", "push", "origin", "master"],
                          cwd=ROOT, capture_output=True, text=True)
    if push.returncode == 0:
        LOG.info("git push 成功")
    else:
        # 凭据缺失或无远端：仅记录失败，不影响本地数据
        LOG.warning(f"git push 失败（不影响本地数据）: {push.stderr.strip() or push.stdout.strip()}")


# ---------- 主流程 ----------
async def main() -> int:
    LOG.info("=" * 60)
    LOG.info("GPU 算力价格每日跟踪开始")
    try:
        rows = await fetch_prices()
        paths = write_outputs(rows)
        history = load_history(days=30)
        report = build_report(history, rows)
        git_commit_and_push(f"chore(gpu-prices): daily snapshot {rows[0]['date']}")
        LOG.info("=" * 60)
        LOG.info("✅ 本次跟踪完成")
        LOG.info(f"  CSV   : {paths['csv']}")
        LOG.info(f"  JSONL : {paths['jsonl']}")
        LOG.info(f"  Latest: {paths['latest']}")
        LOG.info(f"  Report: {report}")
        return 0
    except Exception:
        # 把异常写入日志目录（用户要求）
        LOG.error("执行失败，详情：")
        LOG.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
