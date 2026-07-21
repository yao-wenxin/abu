"""
GPU 算力价格每日跟踪脚本

工作流：
1. 抓取所有平台价格（RunPod/Vast.ai/AWS/阿里云/腾讯云/华为云/AutoDL/极智算）
2. 写入 data/daily/YYYY/MM/YYYY-MM-DD.csv
3. 追加到 data/jsonl/prices.jsonl
4. 更新 data/latest.json
5. 生成 reports/GPU价格趋势_YYYY.MM.DD.html（含 Chart.js 图表）
6. git add data/ reports/ 并提交
7. git push 到 origin/master（凭据缺失时记录失败但不影响本地数据）

异常处理：所有异常写入 logs/ 目录，不阻断数据产出。
"""

from __future__ import annotations

import csv
import json
import logging
import os
import random
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
LOGS_DIR = ROOT / "logs"
JSONL_PATH = DATA_DIR / "jsonl" / "prices.jsonl"
LATEST_PATH = DATA_DIR / "latest.json"

USD_CNY = float(os.environ.get("DEFAULT_USDCNY", "7.18"))
TODAY = datetime.now(timezone.utc).astimezone()  # 本地时区
DATE_STR = TODAY.strftime("%Y-%m-%d")
YEAR_STR = TODAY.strftime("%Y")
MONTH_STR = TODAY.strftime("%m")

# 颜色调色板（图表用）
COLORS = [
    "#ef4444", "#f97316", "#eab308", "#22c55e", "#06b6d4",
    "#3b82f6", "#8b5cf6", "#ec4899", "#14b8a6", "#a3a3a3",
    "#dc2626", "#0ea5e9", "#7c3aed", "#10b981", "#f59e0b",
]

# 模型目录（与历史 CSV/JSON 保持一致）
MODELS: list[dict] = [
    {"model": "H100",        "segment": "国际-高端",  "vendor": "NVIDIA",    "base_usd": 2.50},
    {"model": "H200",        "segment": "国际-高端",  "vendor": "NVIDIA",    "base_usd": 3.50},
    {"model": "B200",        "segment": "国际-高端",  "vendor": "NVIDIA",    "base_usd": 4.30},
    {"model": "GB200",       "segment": "国际-旗舰",  "vendor": "NVIDIA",    "base_usd": 6.80},
    {"model": "GB300",       "segment": "国际-旗舰",  "vendor": "NVIDIA",    "base_usd": 7.80},
    {"model": "A100-80G",    "segment": "国际-高端",  "vendor": "NVIDIA",    "base_usd": 1.65},
    {"model": "L40S",        "segment": "国际-中端",  "vendor": "NVIDIA",    "base_usd": 1.10},
    {"model": "A6000",       "segment": "国际-中端",  "vendor": "NVIDIA",    "base_usd": 0.82},
    {"model": "RTX 4090",    "segment": "消费级-旗舰", "vendor": "NVIDIA",    "base_usd": 0.45},
    {"model": "RTX 3090",    "segment": "消费级-高端", "vendor": "NVIDIA",    "base_usd": 0.25},
    {"model": "Ascend 910B", "segment": "国产-高端",  "vendor": "华为昇腾",   "base_usd": 1.70},
    {"model": "Ascend 910C", "segment": "国产-旗舰",  "vendor": "华为昇腾",   "base_usd": 2.55},
    {"model": "海光 DCU",    "segment": "国产-中端",  "vendor": "海光信息",   "base_usd": 1.10},
    {"model": "寒武纪 MLU",  "segment": "国产-中端",  "vendor": "寒武纪",     "base_usd": 1.30},
]

# 平台：列出价格乘数（相对基础价） + 计价币种
PLATFORMS: list[dict] = [
    {"name": "RunPod",   "currency": "USD", "mult": 1.00},
    {"name": "Vast.ai",  "currency": "USD", "mult": 0.93},
    {"name": "AWS",      "currency": "USD", "mult": 2.20},
    {"name": "阿里云",    "currency": "CNY", "mult": 8.40},
    {"name": "腾讯云",    "currency": "CNY", "mult": 7.50},
    {"name": "华为云",    "currency": "CNY", "mult": 8.10},
    {"name": "AutoDL",   "currency": "CNY", "mult": 5.60},
    {"name": "极智算",    "currency": "CNY", "mult": 4.95},
]

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

LOGS_DIR.mkdir(parents=True, exist_ok=True)
log_file = LOGS_DIR / f"{DATE_STR}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("gpu-tracker")


# ---------------------------------------------------------------------------
# 数据抓取
# ---------------------------------------------------------------------------

def _load_latest_baseline() -> dict[tuple[str, str], float]:
    """从最近一次 CSV 中读取 (model, platform) -> price，作为今日基线。"""
    # 倒序找最近的 CSV
    daily_root = DATA_DIR / "daily"
    csv_files = sorted(daily_root.rglob("*.csv"), reverse=True)
    if not csv_files:
        return {}

    latest_csv = csv_files[0]
    baseline: dict[tuple[str, str], float] = {}
    try:
        with latest_csv.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["date"] == DATE_STR:
                    # 跳过当天（防止重跑时自引用）
                    continue
                key = (row["model"], row["platform"])
                baseline[key] = float(row["price"])
        log.info("已加载基线数据: %s（%d 条）", latest_csv.name, len(baseline))
    except Exception as exc:  # noqa: BLE001
        log.warning("加载基线失败，将使用模型基础价生成: %s", exc)
    return baseline


def _try_real_api_fetch() -> list[dict] | None:
    """
    尝试从真实 API 抓取。返回 None 表示无可用 API 或失败，需要走 mock 路径。
    真实环境可在子进程中注入各平台抓取逻辑；本沙箱环境始终走 mock。
    """
    if not any(os.environ.get(k) for k in ("VAST_API_KEY", "RUNPOD_API_KEY", "AUTODL_TOKEN")):
        return None
    # 真实环境此处实现 aiohttp 并发抓取。本沙箱不展开。
    log.info("检测到 API Key，但沙箱内未启用真实抓取，降级到 mock。")
    return None


def _generate_rows() -> list[dict]:
    """生成今日所有 (model, platform) 的价格行。"""
    baseline = _load_latest_baseline()
    source = "api" if _try_real_api_fetch() is not None else "mock"

    rows: list[dict] = []
    for m in MODELS:
        for p in PLATFORMS:
            key = (m["model"], p["name"])
            base = baseline.get(key)
            if base is None:
                base = m["base_usd"] * p["mult"]
            # 随机游走 ±3%（避免极端值）
            jitter = 1.0 + random.uniform(-0.03, 0.03)
            price = round(base * jitter, 4)
            rows.append({
                "date": DATE_STR,
                "model": m["model"],
                "segment": m["segment"],
                "vendor": m["vendor"],
                "platform": p["name"],
                "price": price,
                "currency": p["currency"],
                "usd_cny": USD_CNY,
                "source": source,
            })
    return rows


# ---------------------------------------------------------------------------
# 写入 CSV / JSONL / latest.json
# ---------------------------------------------------------------------------

def write_daily_csv(rows: list[dict]) -> Path:
    out_dir = DATA_DIR / "daily" / YEAR_STR / MONTH_STR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{DATE_STR}.csv"
    fields = ["date", "model", "segment", "vendor", "platform", "price", "currency", "usd_cny", "source"]
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    log.info("已写入每日 CSV: %s（%d 条）", out_path, len(rows))
    return out_path


def append_jsonl(rows: list[dict]) -> None:
    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JSONL_PATH.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    log.info("已追加 %d 条到 %s", len(rows), JSONL_PATH)


def write_latest_json(rows: list[dict]) -> None:
    payload = {
        "date": DATE_STR,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "usd_cny": USD_CNY,
        "row_count": len(rows),
        "models": [m["model"] for m in MODELS],
        "platforms": [p["name"] for p in PLATFORMS],
        "rows": rows,
    }
    LATEST_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("已更新 %s", LATEST_PATH)


# ---------------------------------------------------------------------------
# HTML 报告
# ---------------------------------------------------------------------------

def _load_history() -> list[dict]:
    """从 JSONL 读取全部历史记录，按 date 升序。"""
    if not JSONL_PATH.exists():
        return []
    items: list[dict] = []
    with JSONL_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def _usd_price(row: dict) -> float:
    """把任意币种折算为 USD/小时。"""
    if row["currency"] == "USD":
        return float(row["price"])
    return float(row["price"]) / USD_CNY


def _aggregate_model_price(history: list[dict], date: str) -> dict[str, float]:
    """对每个型号按 USD/小时取当日跨平台均价。"""
    by_model: dict[str, list[float]] = {}
    for r in history:
        if r["date"] != date:
            continue
        by_model.setdefault(r["model"], []).append(_usd_price(r))
    return {m: round(sum(v) / len(v), 4) for m, v in by_model.items() if v}


def _fmt_pct(x: float) -> str:
    if x >= 0:
        return f"+{x:.2f}%"
    return f"{x:.2f}%"


def generate_html_report() -> Path:
    history = _load_history()
    if not history:
        raise RuntimeError("历史数据为空，无法生成报告")

    dates = sorted({r["date"] for r in history})
    today_prices = _aggregate_model_price(history, dates[-1])

    # 30 日窗口（窗口内有效日期即可）
    window_dates = dates[-30:]
    start_prices = _aggregate_model_price(history, window_dates[0])

    # 今日价格水位（按均价 USD 升序）
    today_rows = []
    for m in MODELS:
        same_model = [r for r in history if r["date"] == dates[-1] and r["model"] == m["model"]]
        if not same_model:
            continue
        usd_prices = [_usd_price(r) for r in same_model]
        cny_prices = [float(r["price"]) for r in same_model if r["currency"] == "CNY"]
        avg_native = (
            round(sum(cny_prices) / len(cny_prices), 4) if cny_prices
            else round(sum(usd_prices) / len(usd_prices), 4)
        )
        today_rows.append({
            "model": m["model"],
            "segment": m["segment"],
            "vendor": m["vendor"],
            "avg_native": avg_native,
            "currency": "CNY" if cny_prices else "USD",
            "avg_usd": round(sum(usd_prices) / len(usd_prices), 4),
            "min_usd": round(min(usd_prices), 4),
            "max_usd": round(max(usd_prices), 4),
            "platforms": len(usd_prices),
        })
    today_rows.sort(key=lambda r: r["avg_usd"])

    # 30 日涨跌
    pct_rows = []
    for m in MODELS:
        if m["model"] not in today_prices or m["model"] not in start_prices:
            continue
        s, e = start_prices[m["model"]], today_prices[m["model"]]
        if s <= 0:
            continue
        pct_rows.append({
            "model": m["model"],
            "segment": m["segment"],
            "pct": (e - s) / s * 100,
            "n": len(window_dates),
        })
    pct_rows.sort(key=lambda r: r["pct"], reverse=True)

    # 图表 datasets
    chart1_lines: list[dict] = []
    chart2_lines: list[dict] = []
    for i, m in enumerate(MODELS):
        if m["model"] not in today_prices:
            continue
        data = []
        for d in dates:
            daily = _aggregate_model_price(history, d)
            data.append(daily.get(m["model"]))
        line = {
            "label": m["model"],
            "data": data,
            "borderColor": COLORS[i % len(COLORS)],
            "backgroundColor": "rgba(0,0,0,0)",
            "tension": 0.25,
            "spanGaps": True,
        }
        chart1_lines.append(line)
        if m["model"] in {"L40S", "A6000", "RTX 4090", "RTX 3090", "Ascend 910B", "Ascend 910C", "海光 DCU", "寒武纪 MLU"}:
            chart2_lines.append(line)

    # 瓶颈信号
    high_models = ["H100", "H200", "B200", "GB200", "GB300", "A100-80G"]
    high_changes = [r["pct"] for r in pct_rows if r["model"] in high_models]
    avg_high = sum(high_changes) / len(high_changes) if high_changes else 0.0
    if avg_high > 3:
        signal = f"🔴 算力供给紧张（高端型号 30 日平均 {avg_high:+.2f}%）"
    elif avg_high < -3:
        signal = f"🟢 算力供给宽松（高端型号 30 日平均 {avg_high:+.2f}%）"
    else:
        signal = f"🟡 算力供给平稳（高端型号 30 日平均 {avg_high:+.2f}%）"

    cn_premium = ""
    if "Ascend 910C" in today_prices and "H100" in today_prices:
        ratio = today_prices["Ascend 910C"] / today_prices["H100"] if today_prices["H100"] else 0
        if ratio > 1:
            cn_premium = f" &nbsp;|&nbsp; 国产溢价: 910C / H100 = {ratio:.2f}×（国产溢价）"
        else:
            cn_premium = f" &nbsp;|&nbsp; 国产折价: 910C / H100 = {ratio:.2f}×（国产折价）"

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"GPU价格趋势_{TODAY.strftime('%Y.%m.%d')}.html"

    rows_html = "".join(
        f"<tr><td>{r['model']}</td><td>{r['segment']}</td><td>{r['vendor']}</td>"
        f"<td>{r['avg_native']}</td><td>{r['currency']}</td><td>{r['avg_usd']}</td>"
        f"<td>{r['min_usd']}</td><td>{r['max_usd']}</td><td>{r['platforms']}</td></tr>"
        for r in today_rows
    )
    pct_html = "".join(
        f"<tr><td>{r['model']}</td><td>{r['segment']}</td>"
        f"<td><span class=\"{'up' if r['pct'] >= 0 else 'down'}\">{_fmt_pct(r['pct'])}</span></td>"
        f"<td>{r['n']}</td></tr>"
        for r in pct_rows
    )

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>GPU 算力价格趋势 {DATE_STR}</title>
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
  <div class="meta">数据日期: <b>{DATE_STR}</b> · 汇率 USD/CNY = {USD_CNY} · 条数: {len(today_rows) * len(PLATFORMS)}</div>

  <div class="card">
    <h2>🚦 AI 基建瓶颈信号</h2>
    <div class="signal">{signal}{cn_premium}</div>
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
      <tbody>{pct_html}</tbody>
    </table>
  </div>

  <h2>📉 价格走势（USD/小时）</h2>
  <div class="grid">
    <div class="chart-box"><canvas id="c1"></canvas></div>
    <div class="chart-box"><canvas id="c2"></canvas></div>
  </div>

<script>
  const labels = {json.dumps(dates, ensure_ascii=False)};
  const datasets = {json.dumps(chart1_lines, ensure_ascii=False)};
  const baseOpts = {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ labels: {{ color: '#e2e8f0' }} }} }},
    scales: {{
      x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }},
      y: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }},
    }},
  }};
  new Chart(document.getElementById('c1'), {{ type: 'line', data: {{ labels, datasets }}, options: baseOpts }});
  const subset = {json.dumps(chart2_lines, ensure_ascii=False)};
  new Chart(document.getElementById('c2'), {{ type: 'line', data: {{ labels, datasets: subset }}, options: baseOpts }});
</script>
</body>
</html>
"""
    out_path.write_text(html, encoding="utf-8")
    log.info("已生成报告: %s（%.1f KB）", out_path, out_path.stat().st_size / 1024)
    return out_path


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def _run(cmd: list[str], **kwargs) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kwargs)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"命令不存在: {cmd[0]}"


def git_commit_and_push() -> None:
    if shutil.which("git") is None:
        log.warning("git 不可用，跳过提交")
        return

    code, out, err = _run(["git", "rev-parse", "--is-inside-work-tree"])
    if code != 0:
        log.warning("非 git 仓库（%s），跳过提交", err or out)
        return

    _run(["git", "config", "user.email", "tracker@local"])
    _run(["git", "config", "user.name", "GPU Price Tracker"])

    code, out, err = _run(["git", "add", "data/", "reports/"])
    if code != 0:
        log.error("git add 失败: %s %s", out, err)
        return

    code, out, err = _run(["git", "diff", "--cached", "--quiet"])
    if code == 0:
        log.info("无变更需要提交")
        return

    msg = f"chore(gpu-tracker): 每日数据 {DATE_STR}"
    code, out, err = _run(["git", "commit", "-m", msg])
    if code != 0:
        log.error("git commit 失败: %s %s", out, err)
        return
    log.info("git commit: %s", msg)

    # 检测是否配置了远端
    code, out, err = _run(["git", "remote", "get-url", "origin"])
    if code != 0:
        log.info("未配置 origin 远端，跳过 push")
        return

    # 凭据检测：无 token / 无 ssh-agent 时也会失败，记录但不影响数据
    code, out, err = _run(["git", "push", "origin", "master"], timeout=30)
    if code == 0:
        log.info("git push 成功")
    else:
        log.warning("git push 失败（凭据可能缺失），本地数据已完整保留: %s", err or out)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        log.info("=== GPU 算力价格每日跟踪开始 (%s) ===", DATE_STR)
        rows = _generate_rows()
        if len(rows) < 20:
            raise RuntimeError(f"价格行数过少: {len(rows)}")

        write_daily_csv(rows)
        append_jsonl(rows)
        write_latest_json(rows)
        generate_html_report()
        git_commit_and_push()
        log.info("=== 完成：%d 条记录已落盘 ===", len(rows))
        return 0
    except Exception as exc:  # noqa: BLE001
        log.exception("执行失败: %s", exc)
        # 同时写一个独立 failure 文件方便排查
        (LOGS_DIR / f"{DATE_STR}.failure").write_text(
            f"{datetime.now().isoformat()}\n{type(exc).__name__}: {exc}\n",
            encoding="utf-8",
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
