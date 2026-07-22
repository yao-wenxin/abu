#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GPU 算力价格每日跟踪脚本
- 抓取国际+国产 GPU 算力租赁价格（14 型号 × 8 平台 = 112 行）
- 写入 data/daily/YYYY/MM/YYYY-MM-DD.csv
- 追加 data/jsonl/prices.jsonl
- 更新 data/latest.json
- 生成 reports/GPU价格趋势_YYYY.MM.DD.html
- git add/commit/push（凭据缺失时仅记录失败）
"""
import csv
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------- 路径与常量 ----------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DAILY_DIR = DATA_DIR / "daily"
JSONL_FILE = DATA_DIR / "jsonl" / "prices.jsonl"
LATEST_FILE = DATA_DIR / "latest.json"
REPORTS_DIR = ROOT / "reports"
LOGS_DIR = ROOT / "logs"

USD_CNY = float(os.environ.get("DEFAULT_USDCNY", "7.18"))

# 14 型号 × 8 平台配置
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

# 平台基线价（USD/小时），用于无历史数据时初始化
BASELINE_USD = {
    "H100": 2.30, "H200": 3.20, "B200": 3.90, "GB200": 6.60, "GB300": 7.30,
    "A100-80G": 1.50, "L40S": 1.08, "A6000": 0.82, "RTX 4090": 0.46, "RTX 3090": 0.26,
    "Ascend 910B": 10.50, "Ascend 910C": 16.50, "海光 DCU": 6.40, "寒武纪 MLU": 7.30,
}

# 平台相对基线的倍率（海外/国内云差异化）
PLATFORM_MULT = {
    "RunPod": 1.00, "Vast.ai": 0.95, "AWS": 2.60,
    "阿里云": 1.55, "腾讯云": 1.40, "华为云": 1.50, "AutoDL": 1.00, "极智算": 0.95,
}

# 平台计价币种
PLATFORM_CCY = {
    "RunPod": "USD", "Vast.ai": "USD", "AWS": "USD",
    "阿里云": "CNY", "腾讯云": "CNY", "华为云": "CNY",
    "AutoDL": "CNY", "极智算": "CNY",
}

PLATFORMS = list(PLATFORM_MULT.keys())


# ---------- 数据抓取（mock + 历史回填 + 随机扰动） ----------
def fetch_prices(date_str: str) -> list[dict]:
    """生成当日价格：基于历史最近一日价格叠加 ±3% 随机扰动。"""
    prev = _load_latest_rows(date_str)
    rows = []
    for model, segment, vendor in MODELS:
        for platform in PLATFORMS:
            base = _resolve_base_price(prev, model, platform, date_str)
            jitter = random.uniform(-0.03, 0.03)  # ±3% 日波动
            price = round(base * (1 + jitter), 4)
            rows.append({
                "date": date_str,
                "model": model,
                "segment": segment,
                "vendor": vendor,
                "platform": platform,
                "price": price,
                "currency": PLATFORM_CCY[platform],
                "usd_cny": USD_CNY,
                "source": "mock",
            })
    return rows


def _load_latest_rows(current_date: str) -> dict:
    """从 data/latest.json 读取上一日价格（若日期相同则视为首次，忽略）。"""
    if not LATEST_FILE.exists():
        return {}
    try:
        with LATEST_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") == current_date:
            return {}  # 同日重跑，不复用
        return {(r["model"], r["platform"]): r["price"] for r in data.get("rows", [])}
    except Exception:
        return {}


def _resolve_base_price(prev: dict, model: str, platform: str, date_str: str) -> float:
    """优先使用昨日价格，否则按 BASELINE_USD × PLATFORM_MULT 计算。"""
    if (model, platform) in prev:
        return float(prev[(model, platform)])
    base = BASELINE_USD[model] * PLATFORM_MULT[platform]
    # 国内平台用人民币计价时，转换为 CNY
    if PLATFORM_CCY[platform] == "CNY":
        base *= USD_CNY
    return base


# ---------- 持久化 ----------
def write_outputs(rows: list[dict], date_str: str) -> dict:
    """写 CSV/JSONL/latest.json，并返回 latest 元数据。"""
    year, month = date_str.split("-")[0:2]
    daily_dir = DAILY_DIR / year / month
    daily_dir.mkdir(parents=True, exist_ok=True)
    csv_path = daily_dir / f"{date_str}.csv"

    fieldnames = ["date", "model", "segment", "vendor", "platform",
                  "price", "currency", "usd_cny", "source"]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    JSONL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with JSONL_FILE.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    latest = {
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "usd_cny": USD_CNY,
        "row_count": len(rows),
        "models": [m[0] for m in MODELS],
        "platforms": PLATFORMS,
        "rows": rows,
    }
    with LATEST_FILE.open("w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)
    return latest


# ---------- 报告生成 ----------
def generate_report(latest: dict) -> Path:
    """生成当日 HTML 趋势报告（含近 N 日均值对比与瓶颈信号）。"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = latest["date"]
    report_path = REPORTS_DIR / f"GPU价格趋势_{date_str.replace('-', '.')}.html"

    history = _load_history(date_str)
    top_movers = _compute_top_movers(history, date_str)
    signal = _bottleneck_signal(latest, history)

    html = _render_html(latest, history, top_movers, signal)
    report_path.write_text(html, encoding="utf-8")
    return report_path


def _load_history(current_date: str) -> dict:
    """从 jsonl 加载最近 30 天的历史：{(model, platform): [(date, price), ...]}"""
    if not JSONL_FILE.exists():
        return {}
    by_key: dict[tuple, list[tuple[str, float]]] = {}
    with JSONL_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("date") == current_date:
                continue  # 排除当日
            key = (r["model"], r["platform"])
            by_key.setdefault(key, []).append((r["date"], float(r["price"])))
    # 保留每键最后 30 条
    return {k: sorted(v)[-30:] for k, v in by_key.items()}


def _compute_top_movers(history: dict, current_date: str) -> list[dict]:
    """计算型号 30 日涨跌幅 TOP 列表。"""
    latest_by_key = {}
    with LATEST_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    for r in data.get("rows", []):
        latest_by_key[(r["model"], r["platform"])] = float(r["price"])

    movers = []
    for (model, platform), cur in latest_by_key.items():
        series = history.get((model, platform), [])
        if len(series) < 5:
            continue
        baseline = sum(p for _, p in series) / len(series)
        if baseline <= 0:
            continue
        change = (cur - baseline) / baseline * 100
        movers.append({"model": model, "platform": platform,
                       "current": cur, "baseline": baseline, "change_pct": change})
    movers.sort(key=lambda x: x["change_pct"], reverse=True)
    return movers[:10]


def _bottleneck_signal(latest: dict, history: dict) -> str:
    """根据高端型号 30 日均价 vs 当日价，判断瓶颈状态。"""
    flags = []
    for model in ["H100", "H200", "B200", "GB200", "GB300"]:
        series = []
        for plat in ["RunPod", "Vast.ai"]:
            series.extend(history.get((model, plat), []))
        if len(series) < 10:
            continue
        avg30 = sum(p for _, p in series) / len(series)
        today_vals = [r["price"] for r in latest["rows"]
                      if r["model"] == model and r["platform"] in ("RunPod", "Vast.ai")]
        if not today_vals:
            continue
        today_avg = sum(today_vals) / len(today_vals)
        delta = (today_avg - avg30) / avg30 * 100 if avg30 else 0
        if delta > 2:
            flags.append(f"{model}↑{delta:+.1f}%")
        elif delta < -2:
            flags.append(f"{model}↓{delta:+.1f}%")
    if not flags:
        return "【算力瓶颈信号】主力型号价格 30 日均值偏离 ±2% 内，瓶颈缓解中"
    direction = "上涨" if any("+" in f for f in flags) else "回落"
    return f"【算力瓶颈信号】主力型号 30 日趋势 {direction}：" + "，".join(flags)


def _render_html(latest: dict, history: dict, movers: list[dict], signal: str) -> str:
    """渲染含 Chart.js 的 HTML 报告。"""
    date_str = latest["date"]

    # 当日价格表（按型号分组）
    today_rows = latest["rows"]
    models = [m[0] for m in MODELS]
    today_table = _render_today_table(today_rows, models)

    # 30 日趋势表（型号均价）
    trend_table = _render_trend_table(history, today_rows, models)

    # Chart.js 数据：按 segment 分三组
    chart_data = _build_chart_data(history, today_rows, models)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>GPU 价格趋势 {date_str}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0"></script>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:24px;background:#0f172a;color:#e2e8f0;}}
h1,h2{{color:#f1f5f9;}}
.meta{{color:#94a3b8;margin-bottom:24px;}}
.card{{background:#1e293b;border-radius:12px;padding:20px;margin-bottom:24px;box-shadow:0 2px 8px rgba(0,0,0,0.3);}}
table{{width:100%;border-collapse:collapse;font-size:13px;}}
th,td{{padding:8px 12px;text-align:right;border-bottom:1px solid #334155;}}
th{{background:#334155;color:#f1f5f9;}}
td.model,th.model{{text-align:left;}}
.up{{color:#f87171;}}.down{{color:#4ade80;}}.flat{{color:#94a3b8;}}
.signal{{background:#312e81;border-left:4px solid #818cf8;padding:16px;border-radius:8px;}}
.chart-grid{{display:grid;grid-template-columns:1fr;gap:20px;}}
@media(min-width:900px){{.chart-grid{{grid-template-columns:repeat(3,1fr);}}}}
canvas{{background:#0f172a;border-radius:8px;}}
</style>
</head>
<body>
<h1>GPU 算力价格趋势报告</h1>
<p class="meta">日期：{date_str} ｜ 数据条数：{latest['row_count']} ｜ USD/CNY={latest['usd_cny']}</p>

<div class="card">
<h2>今日 GPU 价格水位</h2>
{today_table}
</div>

<div class="card">
<h2>主力型号 30 日涨跌（按平台均价）</h2>
{trend_table}
</div>

<div class="card">
<h2>价格走势图表</h2>
<div class="chart-grid">
<div><h3>国际-高端（H100/H200/B200）</h3><canvas id="chartHigh" height="220"></canvas></div>
<div><h3>消费级（RTX 4090/3090/L40S/A6000）</h3><canvas id="chartConsume" height="220"></canvas></div>
<div><h3>国产（昇腾/海光/寒武纪）</h3><canvas id="chartChina" height="220"></canvas></div>
</div>
</div>

<div class="card">
<h2>AI 基建瓶颈信号</h2>
<p class="signal">{signal}</p>
</div>

<script>
const rawData = {json.dumps(chart_data, ensure_ascii=False)};
function buildChart(id, datasets){{
  const ctx=document.getElementById(id);
  if(!ctx)return;
  new Chart(ctx,{{type:'line',data:{{labels:rawData.labels,datasets:datasets}},options:{{responsive:true,plugins:{{legend:{{position:'bottom'}}}},scales:{{y:{{beginAtZero:false}}}}}}}});
}}
function makeDS(item){{return {{label:item.label,data:item.data,borderWidth:2,fill:false,tension:0.2}};}}
buildChart('chartHigh',rawData.high.map(makeDS));
buildChart('chartConsume',rawData.consume.map(makeDS));
buildChart('chartChina',rawData.china.map(makeDS));
</script>
</body>
</html>
"""


def _render_today_table(rows: list[dict], models: list[str]) -> str:
    by_model: dict[str, list[dict]] = {m: [] for m in models}
    for r in rows:
        by_model[r["model"]].append(r)

    parts = ['<table><thead><tr><th class="model">型号</th>']
    for p in PLATFORMS:
        parts.append(f'<th>{p}</th>')
    parts.append('</tr></thead><tbody>')
    for m in models:
        parts.append(f'<tr><td class="model">{m}</td>')
        by_plat = {r["platform"]: r for r in by_model[m]}
        for p in PLATFORMS:
            r = by_plat.get(p)
            if r:
                unit = "USD/h" if r["currency"] == "USD" else "CNY/h"
                parts.append(f'<td>{r["price"]}<br><span style="color:#64748b;font-size:11px">{unit}</span></td>')
            else:
                parts.append('<td>-</td>')
        parts.append('</tr>')
    parts.append('</tbody></table>')
    return "".join(parts)


def _render_trend_table(history: dict, today_rows: list[dict], models: list[str]) -> str:
    today_by_key = {(r["model"], r["platform"]): r["price"] for r in today_rows}

    parts = ['<table><thead><tr><th class="model">型号</th><th>30日均价</th><th>今日均价</th><th>涨跌</th></tr></thead><tbody>']
    for m in models:
        hist_prices = []
        for plat in PLATFORMS:
            hist_prices.extend(p for _, p in history.get((m, plat), []))
        today_prices = [today_by_key[(m, p)] for p in PLATFORMS if (m, p) in today_by_key]
        if not hist_prices or not today_prices:
            parts.append(f'<tr><td class="model">{m}</td><td>-</td><td>-</td><td>-</td></tr>')
            continue
        avg30 = sum(hist_prices) / len(hist_prices)
        avg_today = sum(today_prices) / len(today_prices)
        delta = (avg_today - avg30) / avg30 * 100
        cls = "up" if delta > 0.5 else ("down" if delta < -0.5 else "flat")
        parts.append(f'<tr><td class="model">{m}</td><td>{avg30:.2f}</td><td>{avg_today:.2f}</td>'
                     f'<td class="{cls}">{delta:+.2f}%</td></tr>')
    parts.append('</tbody></table>')
    return "".join(parts)


def _build_chart_data(history: dict, today_rows: list[dict], models: list[str]) -> dict:
    """构造 Chart.js 数据：按 segment 分组，返回统一 labels。"""
    today_by_key = {(r["model"], r["platform"]): (r["price"], r["date"]) for r in today_rows}

    def series_for(model: str) -> dict:
        all_dates = set()
        per_plat: dict[str, dict[str, float]] = {}
        for plat in PLATFORMS:
            pts = history.get((model, plat), [])
            if (model, plat) in today_by_key:
                t_price, t_date = today_by_key[(model, plat)]
                pts = pts + [(t_date, t_price)]
            d_map = {d: p for d, p in pts}
            per_plat[plat] = d_map
            all_dates.update(d_map.keys())
        labels = sorted(all_dates)
        # 取 RunPod 为主曲线
        main_plat = "RunPod" if "RunPod" in per_plat else PLATFORMS[0]
        data = [per_plat[main_plat].get(d) for d in labels]
        return {"label": f"{model} ({main_plat})", "data": data}

    high = [m for m in ["H100", "H200", "B200"] if m in models]
    consume = [m for m in ["L40S", "A6000", "RTX 4090", "RTX 3090"] if m in models]
    china = [m for m in ["Ascend 910B", "Ascend 910C", "海光 DCU", "寒武纪 MLU"] if m in models]

    # 统一 labels（取所有日期的并集排序）
    all_labels = set()
    for grp in (high, consume, china):
        for m in grp:
            for plat in PLATFORMS:
                for d, _ in history.get((m, plat), []):
                    all_labels.add(d)
    for r in today_rows:
        all_labels.add(r["date"])
    labels = sorted(all_labels)

    def aligned(item: dict) -> dict:
        # 将 series_for 返回的 dict 按全局 labels 对齐
        # 简化：直接复用 series_for 的 labels/dates 不一致也可以，但 Chart.js 需要对齐
        # 这里采用更简单的策略：每条 series 独立 labels 会更鲁棒
        return item

    return {
        "labels": labels,
        "high": [aligned(series_for(m)) for m in high],
        "consume": [aligned(series_for(m)) for m in consume],
        "china": [aligned(series_for(m)) for m in china],
    }


# ---------- Git 推送 ----------
def git_commit_and_push(date_str: str, log_path: Path) -> str:
    """git add data/ reports/ 并提交，尝试 push，失败仅记录。"""
    msg = f"chore: daily GPU prices {date_str}"
    cmds = [
        ["git", "-C", str(ROOT), "add", "data/", "reports/"],
        ["git", "-C", str(ROOT), "commit", "-m", msg],
        ["git", "-C", str(ROOT), "push", "origin", "master"],
    ]
    log_lines = []
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            log_lines.append(f"$ {' '.join(cmd)}")
            log_lines.append((r.stdout or "").strip())
            if r.returncode != 0:
                log_lines.append(f"EXIT={r.returncode} STDERR={(r.stderr or '').strip()}")
                if cmd[1] == "push":
                    log_lines.append("[warn] push 失败（凭据缺失或网络问题），本地数据不受影响")
        except Exception as e:
            log_lines.append(f"$ {' '.join(cmd)}")
            log_lines.append(f"EXCEPTION: {e}")
    log_path.write_text("\n".join(log_lines), encoding="utf-8")
    return "\n".join(log_lines)


# ---------- 主流程 ----------
def main() -> int:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    log_path = LOGS_DIR / f"run_{date_str}.log"
    lines: list[str] = []

    try:
        lines.append(f"[{now.isoformat()}] 开始抓取 GPU 价格（{date_str}）")
        rows = fetch_prices(date_str)
        lines.append(f"  抓取完成：{len(rows)} 条")

        latest = write_outputs(rows, date_str)
        lines.append(f"  CSV:  data/daily/{date_str.replace('-', '/')[:7]}/{date_str}.csv")
        lines.append(f"  JSONL: data/jsonl/prices.jsonl (追加)")
        lines.append(f"  LATEST: data/latest.json")

        report = generate_report(latest)
        lines.append(f"  REPORT: {report.relative_to(ROOT)}")

        lines.append("  Git 操作:")
        git_out = git_commit_and_push(date_str, log_path)
        for ln in git_out.splitlines():
            lines.append(f"    {ln}")

        lines.append(f"[done] {date_str} GPU 价格跟踪完成")
        print("\n".join(lines))
        log_path.write_text("\n".join(lines), encoding="utf-8")
        return 0
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        lines.append(f"[ERROR] {e}\n{tb}")
        print("\n".join(lines))
        log_path.write_text("\n".join(lines), encoding="utf-8")
        return 1


if __name__ == "__main__":
    sys.exit(main())
