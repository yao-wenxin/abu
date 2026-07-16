"""GPU 算力价格趋势报告生成。

读取 data/jsonl/prices.jsonl 历史价格，生成 HTML 趋势报告
（含今日价格表、30日涨跌表、3个 Chart.js 趋势图、AI 基建瓶颈信号解读）。

设计要点：
- 单文件 HTML，外链 Chart.js CDN 即可渲染；
- 数据全部 inline，无外部依赖；
- 30 日窗口 = 近 30 条独立日期。
"""

from __future__ import annotations

import html
import json
import logging
import os
from collections import defaultdict
from datetime import datetime

from data_source import read_jsonl_all

logger = logging.getLogger(__name__)

# 颜色调色板（与历史报告保持一致）
PALETTE = [
    "#ef4444", "#f97316", "#eab308", "#22c55e", "#06b6d4",
    "#3b82f6", "#8b5cf6", "#ec4899", "#14b8a6", "#a3a3a3",
    "#dc2626", "#0ea5e9", "#7c3aed", "#10b981",
]


def _to_usd(price: float, currency: str, usd_cny: float) -> float:
    return price / usd_cny if currency == "CNY" else price


def _aggregate_today(rows: list[dict], usd_cny: float) -> list[dict]:
    """按型号聚合当日价格（跨平台均价/最低/最高/平台数）。"""
    bucket: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        bucket[r["model"]].append(r)
    out = []
    for model, rs in bucket.items():
        usd_prices = [_to_usd(r["price"], r["currency"], usd_cny) for r in rs]
        avg = sum(usd_prices) / len(usd_prices)
        # 均价按混合币种展示：取最长出现的币种
        cny_prices = [r["price"] for r in rs if r["currency"] == "CNY"]
        usd_only = [r["price"] for r in rs if r["currency"] == "USD"]
        if cny_prices and not usd_only:
            avg_native = sum(cny_prices) / len(cny_prices)
            cur = "CNY"
        elif usd_only and not cny_prices:
            avg_native = sum(usd_only) / len(usd_only)
            cur = "USD"
        else:
            # 混合：统一以 CNY 显示
            cny_avg = sum(cny_prices) / len(cny_prices) if cny_prices else 0
            usd_to_cny = sum(usd_only) / len(usd_only) * usd_cny if usd_only else 0
            avg_native = (cny_avg + usd_to_cny) / 2
            cur = "CNY"
        out.append({
            "model": model,
            "segment": rs[0]["segment"],
            "vendor": rs[0]["vendor"],
            "avg": round(avg_native, 4),
            "currency": cur,
            "avg_usd": round(avg, 4),
            "min": round(min(usd_prices), 4),
            "max": round(max(usd_prices), 4),
            "platforms": len(rs),
        })
    # 固定顺序：按 segment & 型号名
    out.sort(key=lambda x: (x["segment"], x["model"]))
    return out


def _aggregate_30d(history: list[dict], today_date: str, usd_cny: float) -> list[dict]:
    """按型号计算 30 日涨跌（用近 30 个独立日期的均价）。"""
    by_model_date: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_model_segment: dict[str, str] = {}
    by_model_vendor: dict[str, str] = {}
    for r in history:
        usd = _to_usd(r["price"], r["currency"], usd_cny)
        by_model_date[r["model"]][r["date"]].append(usd)
        by_model_segment[r["model"]] = r["segment"]
        by_model_vendor[r["model"]] = r["vendor"]

    today = datetime.strptime(today_date, "%Y-%m-%d")
    out = []
    for model, date_map in by_model_date.items():
        # 取最近 30 个独立日期（与 today 比较）
        dated = sorted(((d, sum(ps) / len(ps)) for d, ps in date_map.items()),
                       key=lambda x: x[0])
        # 过滤到今天为止、且在 30 天窗口内
        window = [(d, p) for d, p in dated
                  if (today - datetime.strptime(d, "%Y-%m-%d")).days <= 30]
        if len(window) < 2:
            continue
        first = window[0][1]
        last = window[-1][1]
        change = (last - first) / first * 100 if first else 0
        out.append({
            "model": model,
            "segment": by_model_segment[model],
            "vendor": by_model_vendor[model],
            "change_pct": round(change, 2),
            "data_points": len(window),
        })

    out.sort(key=lambda x: x["change_pct"], reverse=True)
    return out


def _signal(history: list[dict], today_date: str, usd_cny: float) -> str:
    """生成 AI 基建瓶颈信号一句话。"""
    by_model_date: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in history:
        usd = _to_usd(r["price"], r["currency"], usd_cny)
        by_model_date[r["model"]][r["date"]].append(usd)

    today = datetime.strptime(today_date, "%Y-%m-%d")
    high_end_models = ["H100", "H200", "B200", "GB200", "GB300"]
    changes = []
    for model in high_end_models:
        date_map = by_model_date.get(model, {})
        dated = sorted(date_map.items(), key=lambda x: x[0])
        window = [(d, sum(ps) / len(ps)) for d, ps in dated
                  if (today - datetime.strptime(d, "%Y-%m-%d")).days <= 30]
        if len(window) >= 2:
            ch = (window[-1][1] - window[0][1]) / window[0][1] * 100
            changes.append(ch)
    avg_change = sum(changes) / len(changes) if changes else 0
    if avg_change > 5:
        status = "🔴 算力瓶颈加剧（高端型号 30 日平均 +{:.2f}%）".format(avg_change)
    elif avg_change > 1:
        status = "🟡 算力市场平稳（高端型号 30 日平均 +{:.2f}%）".format(avg_change)
    elif avg_change > -1:
        status = "🟢 算力市场回归（高端型号 30 日平均 {:.2f}%）".format(avg_change)
    else:
        status = "🟢 算力供给宽松（高端型号 30 日平均 {:.2f}%）".format(avg_change)

    # 国产溢价
    def latest_avg(name: str) -> float | None:
        date_map = by_model_date.get(name, {})
        if not date_map:
            return None
        last_date = max(date_map.keys())
        return sum(date_map[last_date]) / len(date_map[last_date])

    p_910c = latest_avg("Ascend 910C")
    p_h100 = latest_avg("H100")
    if p_910c and p_h100 and p_h100 > 0:
        ratio = p_910c / p_h100
        premium = "国产溢价" if ratio > 1.5 else "国产折价" if ratio < 0.8 else "国产平价"
        return f"{status} &nbsp;|&nbsp; 国产溢价: 910C / H100 = {ratio:.2f}×（{premium}）"
    return status


def _build_trend_datasets(history: list[dict], today_date: str, usd_cny: float) -> tuple[list[str], list[dict]]:
    """构造 Chart.js 用的 labels & datasets（按型号聚合 USD/小时 日均）。"""
    by_model_date: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in history:
        usd = _to_usd(r["price"], r["currency"], usd_cny)
        by_model_date[r["model"]][r["date"]].append(usd)

    # 收集所有日期，排序
    all_dates: set[str] = set()
    for m in by_model_date.values():
        all_dates.update(m.keys())
    labels = sorted(all_dates)
    # 限制最近 30 个日期
    today = datetime.strptime(today_date, "%Y-%m-%d")
    labels = [d for d in labels
              if (today - datetime.strptime(d, "%Y-%m-%d")).days <= 30]
    labels.sort()

    datasets = []
    models = sorted(by_model_date.keys())
    for i, model in enumerate(models):
        series = []
        for d in labels:
            ps = by_model_date[model].get(d, [])
            series.append(round(sum(ps) / len(ps), 4) if ps else None)
        datasets.append({
            "label": model,
            "data": series,
            "borderColor": PALETTE[i % len(PALETTE)],
            "backgroundColor": "rgba(0,0,0,0)",
            "tension": 0.25,
            "spanGaps": True,
        })
    return labels, datasets


def render_report(history: list[dict], today_date: str, usd_cny: float,
                  out_path: str) -> str:
    today_rows = [r for r in history if r["date"] == today_date]
    today_agg = _aggregate_today(today_rows, usd_cny)
    trend_30d = _aggregate_30d(history, today_date, usd_cny)
    signal = _signal(history, today_date, usd_cny)
    labels, datasets = _build_trend_datasets(history, today_date, usd_cny)

    # 构造 HTML
    today_rows_html = "".join(
        f"<tr><td>{html.escape(r['model'])}</td><td>{html.escape(r['segment'])}</td>"
        f"<td>{html.escape(r['vendor'])}</td><td>{r['avg']}</td><td>{r['currency']}</td>"
        f"<td>{r['avg_usd']}</td><td>{r['min']}</td><td>{r['max']}</td>"
        f"<td>{r['platforms']}</td></tr>"
        for r in today_agg
    )
    trend_rows_html = "".join(
        f"<tr><td>{html.escape(r['model'])}</td><td>{html.escape(r['segment'])}</td>"
        f"<td class=\"{'up' if r['change_pct'] >= 0 else 'down'}\">"
        f"{'+' if r['change_pct'] >= 0 else ''}{r['change_pct']}%</td>"
        f"<td>{r['data_points']}</td></tr>"
        for r in trend_30d
    )

    # 第三个子图：中端 + 国产
    mid_models = ["RTX 4090", "RTX 3090", "L40S", "A6000",
                  "Ascend 910B", "Ascend 910C", "海光 DCU", "寒武纪 MLU"]
    sub_datasets = [d for d in datasets if d["label"] in mid_models]

    datasets_json = json.dumps(datasets, ensure_ascii=False)
    labels_json = json.dumps(labels, ensure_ascii=False)
    sub_datasets_json = json.dumps(sub_datasets, ensure_ascii=False)

    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>GPU 算力价格趋势 {today_date}</title>
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
  <div class="meta">数据日期: <b>{today_date}</b> · 汇率 USD/CNY = {usd_cny} · 条数: {len(today_rows)}</div>

  <div class="card">
    <h2>🚦 AI 基建瓶颈信号</h2>
    <div class="signal">{signal}</div>
  </div>

  <h2>📊 今日价格水位（型号 × 平台）</h2>
  <div class="card">
    <table>
      <thead><tr><th>型号</th><th>分类</th><th>厂商</th><th>均价</th><th>币种</th><th>均价(USD)</th><th>最低</th><th>最高</th><th>平台数</th></tr></thead>
      <tbody>{today_rows_html}</tbody>
    </table>
  </div>

  <h2>📈 主力型号 30 日涨跌</h2>
  <div class="card">
    <table>
      <thead><tr><th>型号</th><th>分类</th><th>30 日涨跌</th><th>数据点</th></tr></thead>
      <tbody>{trend_rows_html}</tbody>
    </table>
  </div>

  <h2>📉 价格走势（USD/小时）</h2>
  <div class="grid">
    <div class="chart-box"><canvas id="c1"></canvas></div>
    <div class="chart-box"><canvas id="c2"></canvas></div>
  </div>

<script>
  const labels = {labels_json};
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
  const subset = {sub_datasets_json};
  new Chart(document.getElementById('c2'), {{ type: 'line', data: {{ labels, datasets: subset }}, options: baseOpts }});
</script>
</body>
</html>
"""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    logger.info("wrote report -> %s (%d bytes)", out_path, len(html_doc))
    return out_path
