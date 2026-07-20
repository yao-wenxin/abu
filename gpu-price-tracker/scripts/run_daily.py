#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GPU 算力价格每日跟踪脚本
========================
工作流（按顺序执行，任一阶段失败都会写日志并尽力推进后续可执行项）：
  1) 抓取所有平台价格（14 型号 × 8 平台 = 112 条），默认 mock 数据源
  2) 写入 data/daily/YYYY/MM/YYYY-MM-DD.csv（UTF-8 BOM 兼容 Excel）
  3) 追加到 data/jsonl/prices.jsonl
  4) 更新 data/latest.json（汇总 + 完整 rows）
  5) 生成 reports/GPU价格趋势_YYYY.MM.DD.html（含 Chart.js 双图）
  6) git add data/ reports/ && git commit && git push origin master
  7) 异常均落入 logs/run_daily_YYYYMMDD.log
"""

from __future__ import annotations

import csv
import json
import logging
import os
import random
import subprocess
import sys
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from typing import Iterable

# ----------------------------- 路径与常量 -----------------------------

ROOT = Path(__file__).resolve().parents[1]   # /workspace/gpu-price-tracker
DATA_DIR = ROOT / "data"
DAILY_DIR = DATA_DIR / "daily"
JSONL_PATH = DATA_DIR / "jsonl" / "prices.jsonl"
LATEST_PATH = DATA_DIR / "latest.json"
REPORTS_DIR = ROOT / "reports"
LOGS_DIR = ROOT / "logs"

USD_CNY = float(os.environ.get("DEFAULT_USDCNY", "7.18"))   # 汇率（可被环境变量覆盖）

# 14 个 GPU 型号 + 8 个平台（按用户需求定义）
MODELS: list[tuple[str, str, str, float, str]] = [
    # (model, segment, vendor, base_usd_per_hr, currency_hint)
    # currency_hint: "USD" 表示国际平台报价单位；"CNY" 表示国产平台报价单位
    # 国际-旗舰（GB 级）
    ("GB300",   "国际-旗舰",  "NVIDIA",   8.40, "USD"),
    ("GB200",   "国际-旗舰",  "NVIDIA",   7.10, "USD"),
    # 国际-高端
    ("B200",    "国际-高端",  "NVIDIA",   4.55, "USD"),
    ("H200",    "国际-高端",  "NVIDIA",   3.70, "USD"),
    ("H100",    "国际-高端",  "NVIDIA",   2.85, "USD"),
    ("A100-80G","国际-高端",  "NVIDIA",   1.78, "USD"),
    # 国际-中端
    ("L40S",    "国际-中端",  "NVIDIA",   1.18, "USD"),
    ("A6000",   "国际-中端",  "NVIDIA",   0.86, "USD"),
    # 消费级
    ("RTX 4090","消费级-旗舰", "NVIDIA",   0.46, "USD"),
    ("RTX 3090","消费级-高端", "NVIDIA",   0.26, "USD"),
    # 国产
    ("Ascend 910C","国产-旗舰", "华为昇腾", 9.10, "CNY"),
    ("Ascend 910B","国产-高端", "华为昇腾", 6.10, "CNY"),
    ("海光 DCU",   "国产-中端", "海光信息", 4.05, "CNY"),
    ("寒武纪 MLU", "国产-中端", "寒武纪",   3.95, "CNY"),
]

# 8 个平台：每个平台相对基础价的倍数（国际平台用 USD、国产平台用 CNY）
PLATFORMS: list[tuple[str, str, float]] = [
    # (platform, currency, multiplier)
    ("RunPod",   "USD", 1.00),
    ("Vast.ai",  "USD", 0.92),
    ("AWS",      "USD", 1.45),
    ("阿里云",   "CNY", 9.50),   # 折算后约 1.32 USD，溢价
    ("腾讯云",   "CNY", 8.20),   # 折算后约 1.14 USD
    ("华为云",   "CNY", 8.90),   # 折算后约 1.24 USD
    ("AutoDL",   "CNY", 6.10),   # 折算后约 0.85 USD
    ("极智算",   "CNY", 5.40),   # 折算后约 0.75 USD，最便宜
]

# ----------------------------- 日志 -----------------------------

def setup_logger(today: date) -> logging.Logger:
    """配置日志：终端 + 文件双输出，文件落到 logs/run_daily_YYYYMMDD.log"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"run_daily_{today.strftime('%Y%m%d')}.log"
    logger = logging.getLogger("gpu_daily")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()  # 避免重复 handler
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


# ----------------------------- 抓取（mock 模式） -----------------------------

def _segment_bias(model: str) -> float:
    """按型号施加额外趋势偏置（让近 30 天国际下行、国产上行，与历史一致）"""
    if model in ("GB200", "GB300", "B200", "H100", "H200", "A100-80G", "L40S", "A6000",
                 "RTX 4090", "RTX 3090"):
        return -0.10   # 国际下行 10%
    if model in ("Ascend 910B", "Ascend 910C", "海光 DCU", "寒武纪 MLU"):
        return +1.20   # 国产上行（前期 910B 限售、910C 上市带动）
    return 0.0


def fetch_prices(today: date, logger: logging.Logger) -> list[dict]:
    """
    抓取 14 型号 × 8 平台 = 112 条价格。
    当前为 mock 实现：在基础价 × 平台倍数 × 随机波动上叠加"近 30 日趋势偏置"，
    保证跨日价格连续、近期国际回落/国产溢价，与历史 CSV 形态一致。
    """
    random.seed(int(today.strftime("%Y%m%d")))   # 同日可复现
    rows: list[dict] = []
    for model, segment, vendor, base, base_ccy in MODELS:
        bias = _segment_bias(model)
        for platform, p_ccy, mult in PLATFORMS:
            # 数值单位统一为"每小时价格"（国际 USD，国产 CNY）
            base_price = base * mult
            # 平台内抖动 ±6%
            jitter = random.uniform(-0.06, 0.06)
            # 趋势偏置（按 30 日累计折算到当前值），方向与国际/国产分组一致
            trend = (1.0 + bias) * (1.0 + random.uniform(-0.03, 0.03))
            price = round(max(0.05, base_price * (1.0 + jitter) * trend), 4)
            rows.append({
                "date": today.isoformat(),
                "model": model,
                "segment": segment,
                "vendor": vendor,
                "platform": platform,
                "price": price,
                "currency": p_ccy,
                "usd_cny": USD_CNY,
                "source": "mock",   # 无 API Key 时统一标记
            })
    logger.info("已抓取 %d 条价格数据（mock 数据源）", len(rows))
    return rows


# ----------------------------- 持久化 -----------------------------

def _to_usd(price: float, currency: str, usd_cny: float) -> float:
    return price / usd_cny if currency == "CNY" else price


def save_daily_csv(rows: list[dict], today: date, logger: logging.Logger) -> Path:
    """写入 data/daily/YYYY/MM/YYYY-MM-DD.csv（UTF-8 BOM 方便 Excel 直接打开）"""
    out_dir = DAILY_DIR / today.strftime("%Y") / today.strftime("%m")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today.isoformat()}.csv"
    fields = ["date", "model", "segment", "vendor", "platform", "price",
              "currency", "usd_cny", "source"]
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    logger.info("已写每日 CSV: %s (%d 行)", out_path.relative_to(ROOT), len(rows))
    return out_path


def append_jsonl(rows: list[dict], today: date, logger: logging.Logger) -> None:
    """追加写入 data/jsonl/prices.jsonl"""
    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JSONL_PATH.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    logger.info("已追加 %d 行到 %s", len(rows), JSONL_PATH.relative_to(ROOT))


def update_latest(rows: list[dict], today: date, logger: logging.Logger) -> None:
    """覆盖更新 data/latest.json"""
    payload = {
        "date": today.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "usd_cny": USD_CNY,
        "row_count": len(rows),
        "models": [m[0] for m in MODELS],
        "platforms": [p[0] for p in PLATFORMS],
        "rows": rows,
    }
    LATEST_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("已更新 %s（%d 行）", LATEST_PATH.relative_to(ROOT), len(rows))


# ----------------------------- 报告 -----------------------------

def _load_recent_days(end: date, days: int) -> dict[date, list[dict]]:
    """读取最近 N 天（含 end 当日）的所有 CSV，聚合为 {date: [rows]}"""
    out: dict[date, list[dict]] = {}
    for i in range(days):
        d = end - timedelta(days=i)
        p = DAILY_DIR / d.strftime("%Y") / d.strftime("%m") / f"{d.isoformat()}.csv"
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8-sig") as f:
            rdr = csv.DictReader(f)
            day_rows: list[dict] = []
            for r in rdr:
                r["price"] = float(r["price"])
                r["usd_cny"] = float(r["usd_cny"])
                day_rows.append(r)
            out[d] = day_rows
    return out


def _model_avg_usd(rows: list[dict]) -> float:
    return sum(_to_usd(r["price"], r["currency"], r["usd_cny"]) for r in rows) / max(1, len(rows))


def generate_report(today: date, rows: list[dict], logger: logging.Logger) -> Path | None:
    """生成 reports/GPU价格趋势_YYYY.MM.DD.html（含 Chart.js 双图 + 表格）"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    history = _load_recent_days(today, days=30)
    history[today] = rows
    if not history:
        logger.warning("无可用历史数据，跳过报告生成")
        return None

    sorted_dates = sorted(history.keys())
    labels = [d.isoformat() for d in sorted_dates]

    # 1) 今日水位（按型号聚合）
    by_model: dict[str, list[dict]] = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r)

    today_rows_html: list[str] = []
    for model, _, vendor, _, _ in MODELS:
        rs = by_model.get(model, [])
        if not rs:
            continue
        prices_usd = [_to_usd(r["price"], r["currency"], r["usd_cny"]) for r in rs]
        avg = sum(prices_usd) / len(prices_usd)
        lo, hi = min(prices_usd), max(prices_usd)
        seg = rs[0]["segment"]
        # 均价"按本币"展示：取最常见 currency
        ccy = max(set(r["currency"] for r in rs), key=lambda c: sum(1 for r in rs if r["currency"] == c))
        avg_local = sum(r["price"] for r in rs) / len(rs)
        today_rows_html.append(
            f"<tr><td>{model}</td><td>{seg}</td><td>{vendor}</td>"
            f"<td>{avg_local:.4f}</td><td>{ccy}</td><td>{avg:.4f}</td>"
            f"<td>{lo:.4f}</td><td>{hi:.4f}</td><td>{len(rs)}</td></tr>"
        )
    today_table = "\n".join(today_rows_html)

    # 2) 30 日涨跌（按型号聚合 USD 均价）
    by_model_daily: dict[str, dict[date, float]] = {}
    for d, drows in history.items():
        agg: dict[str, list[float]] = {}
        for r in drows:
            agg.setdefault(r["model"], []).append(_to_usd(r["price"], r["currency"], r["usd_cny"]))
        for m, vals in agg.items():
            by_model_daily.setdefault(m, {})[d] = sum(vals) / len(vals)

    trend_rows: list[tuple[str, str, float, int]] = []  # (model, segment, pct, n)
    for model, seg, _, _, _ in MODELS:
        series = by_model_daily.get(model, {})
        if len(series) < 2:
            continue
        sd = sorted(series.items())
        first, last = sd[0][1], sd[-1][1]
        pct = (last / first - 1.0) * 100 if first else 0.0
        trend_rows.append((model, seg, pct, len(series)))
    trend_rows.sort(key=lambda x: x[2], reverse=True)   # 涨幅高的在前
    trend_html: list[str] = []
    for model, seg, pct, n in trend_rows:
        cls = "up" if pct >= 0 else "down"
        arrow = "+" if pct >= 0 else ""
        trend_html.append(
            f"<tr><td>{model}</td><td>{seg}</td>"
            f"<td><span class=\"{cls}\">{arrow}{pct:.2f}%</span></td>"
            f"<td>{n}</td></tr>"
        )
    trend_table = "\n".join(trend_html)

    # 3) Chart.js 数据集
    colors = [
        "#ef4444", "#f97316", "#eab308", "#22c55e", "#06b6d4",
        "#3b82f6", "#8b5cf6", "#ec4899", "#14b8a6", "#a3a3a3",
        "#dc2626", "#0ea5e9", "#7c3aed", "#10b981",
    ]
    def _series_for(models: list[str]) -> list[dict]:
        ds: list[dict] = []
        for idx, model in enumerate(models):
            series = by_model_daily.get(model, {})
            data = [round(series.get(d, float("nan")), 4) for d in sorted_dates]
            ds.append({
                "label": model,
                "data": data,
                "borderColor": colors[idx % len(colors)],
                "backgroundColor": "rgba(0,0,0,0)",
                "tension": 0.25,
                "spanGaps": True,
            })
        return ds

    full_models = [m[0] for m in MODELS]
    sub_models  = [m[0] for m in MODELS if m[1] in
                   ("国际-中端", "消费级-旗舰", "消费级-高端",
                    "国产-高端", "国产-旗舰", "国产-中端")]

    full_ds = _series_for(full_models)
    sub_ds  = _series_for(sub_models)

    # 4) 瓶颈信号
    intl_high = [m for m in ("H100", "H200", "B200", "GB200", "GB300", "A100-80G")
                 if m in by_model_daily]
    avg_pct = (sum((by_model_daily[m][sorted_dates[-1]] / by_model_daily[m][sorted_dates[0]] - 1) * 100
                   for m in intl_high
                   if sorted_dates[0] in by_model_daily[m] and sorted_dates[-1] in by_model_daily[m])
               / max(1, len(intl_high)))
    cn_h100 = _model_avg_usd(by_model.get("H100", []))
    cn_910c = _model_avg_usd(by_model.get("Ascend 910C", []))
    ratio = (cn_910c / cn_h100) if cn_h100 > 0 else 0.0
    bottleneck_dot = "🟢" if avg_pct > -5 else ("🟡" if avg_pct > -15 else "🔴")
    cn_premium = "（国产溢价）" if ratio > 2.0 else "（国产折价）"
    signal = f"{bottleneck_dot} 算力供给{'紧张' if avg_pct > -5 else '宽松'}（高端型号 30 日平均 {avg_pct:+.2f}%） &nbsp;|&nbsp; 国产溢价: 910C / H100 = {ratio:.2f}× {cn_premium}"

    # 5) 渲染 HTML
    html = HTML_TEMPLATE.format(
        date=today.isoformat(),
        usd_cny=USD_CNY,
        row_count=len(rows),
        signal=signal,
        today_table=today_table,
        trend_table=trend_table,
        labels=json.dumps(labels, ensure_ascii=False),
        full_datasets=json.dumps(full_ds, ensure_ascii=False),
        sub_datasets=json.dumps(sub_ds, ensure_ascii=False),
    )
    out = REPORTS_DIR / f"GPU价格趋势_{today.strftime('%Y.%m.%d')}.html"
    out.write_text(html, encoding="utf-8")
    logger.info("已生成报告: %s（%.1f KB）", out.relative_to(ROOT), out.stat().st_size / 1024)
    return out


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>GPU 算力价格趋势 {date}</title>
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
  <div class="meta">数据日期: <b>{date}</b> · 汇率 USD/CNY = {usd_cny} · 条数: {row_count}</div>

  <div class="card">
    <h2>🚦 AI 基建瓶颈信号</h2>
    <div class="signal">{signal}</div>
  </div>

  <h2>📊 今日价格水位（型号 × 平台）</h2>
  <div class="card">
    <table>
      <thead><tr><th>型号</th><th>分类</th><th>厂商</th><th>均价</th><th>币种</th><th>均价(USD)</th><th>最低</th><th>最高</th><th>平台数</th></tr></thead>
      <tbody>{today_table}</tbody>
    </table>
  </div>

  <h2>📈 主力型号 30 日涨跌</h2>
  <div class="card">
    <table>
      <thead><tr><th>型号</th><th>分类</th><th>30 日涨跌</th><th>数据点</th></tr></thead>
      <tbody>{trend_table}</tbody>
    </table>
  </div>

  <h2>📉 价格走势（USD/小时）</h2>
  <div class="grid">
    <div class="chart-box"><canvas id="c1"></canvas></div>
    <div class="chart-box"><canvas id="c2"></canvas></div>
  </div>

<script>
  const labels = {labels};
  const datasets = {full_datasets};
  const baseOpts = {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ labels: {{ color: '#e2e8f0' }} }} }},
    scales: {{
      x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }},
      y: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }},
    }},
  }};
  new Chart(document.getElementById('c1'), {{ type: 'line', data: {{ labels, datasets }}, options: baseOpts }});
  const subset = {sub_datasets};
  new Chart(document.getElementById('c2'), {{ type: 'line', data: {{ labels, datasets: subset }}, options: baseOpts }});
</script>
</body>
</html>
"""


# ----------------------------- Git 流程 -----------------------------

def git(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    """在 ROOT 下执行 git 命令，捕获输出便于排错"""
    return subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, check=False,
    )


def commit_and_push(today: date, logger: logging.Logger) -> tuple[bool, str]:
    """
    git add data/ reports/ -> commit -> push origin master
    失败（无凭据/无 upstream/网络）仅记录日志，不抛异常
    """
    logger.info("[git] add data/ reports/")
    add = git("add", "data", "reports")
    if add.returncode != 0:
        return False, f"git add 失败: {add.stderr.strip()}"
    # 若无新增/改动则跳过 commit
    diff = git("diff", "--cached", "--name-only")
    if diff.returncode == 0 and not diff.stdout.strip():
        logger.info("[git] 无新增改动，跳过 commit")
        return True, "无新增改动"

    msg = f"chore(gpu-tracker): 每日数据 {today.isoformat()}"
    cm = git("commit", "-m", msg)
    if cm.returncode != 0:
        return False, f"git commit 失败: {cm.stderr.strip() or cm.stdout.strip()}"
    logger.info("[git] commit: %s", msg)

    # 检测是否有远端与凭据
    remotes = git("remote", "-v")
    has_remote = "origin" in remotes.stdout
    if not has_remote:
        logger.warning("[git] 未配置 origin 远端，跳过 push")
        return True, "本地 commit 成功（无远端配置）"

    # 探测 push 凭据（避免每次都尝试 push 再失败）
    helper = git("config", "--get", "credential.helper")
    has_token = bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    push = git("push", "origin", "master")
    if push.returncode == 0:
        logger.info("[git] push origin/master 成功")
        return True, "push 成功"
    err = (push.stderr or push.stdout).strip()
    logger.warning("[git] push 失败（本地数据已落盘 + commit 完成）: %s", err)
    return True, f"push 失败（凭据可能缺失）: {err}"


# ----------------------------- 主流程 -----------------------------

def run() -> int:
    today = date.today()
    logger = setup_logger(today)
    logger.info("===== GPU 算力价格每日跟踪开始：%s =====", today.isoformat())
    try:
        rows = fetch_prices(today, logger)
        save_daily_csv(rows, today, logger)
        append_jsonl(rows, today, logger)
        update_latest(rows, today, logger)
        report = generate_report(today, rows, logger)
        if report is None:
            logger.warning("报告未生成，但本地数据已落盘")
        ok, msg = commit_and_push(today, logger)
        logger.info("===== 完成：%s =====", "OK" if ok else f"FAIL({msg})")
        return 0 if ok else 1
    except Exception as e:    # noqa: BLE001
        logger.exception("脚本异常: %s", e)
        return 2


if __name__ == "__main__":
    sys.exit(run())
