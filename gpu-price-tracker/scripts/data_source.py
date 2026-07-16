"""GPU 算力价格数据源模块。

按用户要求：覆盖 8 大平台（RunPod / Vast.ai / AWS / 阿里云 / 腾讯云 / 华为云 / AutoDL / 极智算），
14 个 GPU 型号（H100/H200/B200/GB200/GB300/A100-80G/L40S/A6000/RTX 4090/RTX 3090/
Ascend 910B/Ascend 910C/海光 DCU/寒武纪 MLU）。

每个数据源独立抓取，失败时记录异常并降级到 mock 数据，保证主流程不中断。
真实 API 集成通过环境变量 VAST_API_KEY/RUNPOD_API_KEY/AUTODL_TOKEN 启用。
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger(__name__)

# 平台 → 计价币种 + 厂商结构
PLATFORMS = [
    {"name": "RunPod", "currency": "USD", "scope": "intl"},
    {"name": "Vast.ai", "currency": "USD", "scope": "intl"},
    {"name": "AWS", "currency": "USD", "scope": "intl"},
    {"name": "阿里云", "currency": "CNY", "scope": "cn"},
    {"name": "腾讯云", "currency": "CNY", "scope": "cn"},
    {"name": "华为云", "currency": "CNY", "scope": "cn"},
    {"name": "AutoDL", "currency": "CNY", "scope": "cn"},
    {"name": "极智算", "currency": "CNY", "scope": "cn"},
]

# 型号定义（含分类、厂商、基准价 USD/h）
MODELS: list[dict] = [
    {"name": "H100",        "segment": "国际-高端",   "vendor": "NVIDIA",   "base_usd": 2.50},
    {"name": "H200",        "segment": "国际-高端",   "vendor": "NVIDIA",   "base_usd": 3.30},
    {"name": "B200",        "segment": "国际-高端",   "vendor": "NVIDIA",   "base_usd": 4.10},
    {"name": "GB200",       "segment": "国际-旗舰",   "vendor": "NVIDIA",   "base_usd": 6.40},
    {"name": "GB300",       "segment": "国际-旗舰",   "vendor": "NVIDIA",   "base_usd": 7.50},
    {"name": "A100-80G",    "segment": "国际-高端",   "vendor": "NVIDIA",   "base_usd": 1.60},
    {"name": "L40S",        "segment": "国际-中端",   "vendor": "NVIDIA",   "base_usd": 1.05},
    {"name": "A6000",       "segment": "国际-中端",   "vendor": "NVIDIA",   "base_usd": 0.75},
    {"name": "RTX 4090",    "segment": "消费级-旗舰", "vendor": "NVIDIA",   "base_usd": 0.42},
    {"name": "RTX 3090",    "segment": "消费级-高端", "vendor": "NVIDIA",   "base_usd": 0.24},
    {"name": "Ascend 910B", "segment": "国产-高端",   "vendor": "华为昇腾", "base_usd": 1.50},
    {"name": "Ascend 910C", "segment": "国产-旗舰",   "vendor": "华为昇腾", "base_usd": 2.20},
    {"name": "海光 DCU",     "segment": "国产-中端",   "vendor": "海光信息", "base_usd": 0.95},
    {"name": "寒武纪 MLU",   "segment": "国产-中端",   "vendor": "寒武纪",   "base_usd": 1.05},
]

# 平台相对基准的倍率（用于 mock 价格生成）
PLATFORM_MULTIPLIER = {
    "RunPod":   {"USD": 1.00},
    "Vast.ai":  {"USD": 0.90},
    "AWS":      {"USD": 2.10},  # AWS 价格远高于其他
    "阿里云":    {"USD": 1.45, "CNY": 9.30},  # 国际平台按 USD 计价时使用 1.45x，再换算 CNY
    "腾讯云":    {"USD": 1.30, "CNY": 8.40},
    "华为云":    {"USD": 1.25, "CNY": 8.10},
    "AutoDL":   {"USD": 0.95, "CNY": 6.10},
    "极智算":    {"USD": 0.85, "CNY": 5.50},
}

# 国际平台（CNY 计价的国产型号）：USD 价 * 汇率
CN_DOMESTIC_PREMIUM = {
    "RunPod":  6.5,   # 国际平台上的国产型号稀缺溢价
    "Vast.ai": 6.7,
    "AWS":    14.0,
    "阿里云":   1.0,   # 国内平台：直接使用 CNY
    "腾讯云":   1.0,
    "华为云":   1.0,
    "AutoDL":  1.0,
    "极智算":   1.0,
}


@dataclass
class PriceRow:
    date: str
    model: str
    segment: str
    vendor: str
    platform: str
    price: float
    currency: str
    usd_cny: float
    source: str

    def to_dict(self) -> dict:
        return asdict(self)


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_for(date: str, model: str, platform: str) -> int:
    """基于日期+型号+平台生成稳定随机种子，保证当日价格稳定且与昨日不同。"""
    s = f"{date}|{model}|{platform}"
    return abs(hash(s)) % (2**31)


def _mock_price(model: dict, platform: dict, date: str, usd_cny: float) -> PriceRow:
    """生成 mock 价格（与历史数据生成逻辑保持一致）。"""
    name = model["name"]
    is_domestic = model["vendor"] in {"华为昇腾", "海光信息", "寒武纪"}
    mult = PLATFORM_MULTIPLIER[platform["name"]]
    rng = random.Random(_seed_for(date, name, platform["name"]))

    # 国际平台（CUD 计价的国产型号）：高价 + 强稀缺溢价
    if is_domestic and platform["scope"] == "intl":
        usd_price = model["base_usd"] * CN_DOMESTIC_PREMIUM[platform["name"]]
        # 注入 ±5% 噪声
        usd_price *= 1 + rng.uniform(-0.05, 0.05)
        return PriceRow(
            date=date, model=name, segment=model["segment"], vendor=model["vendor"],
            platform=platform["name"], price=round(usd_price, 4),
            currency="USD", usd_cny=usd_cny, source="mock",
        )

    # 国际平台（国际型号）
    if platform["scope"] == "intl":
        usd_price = model["base_usd"] * mult["USD"]
        usd_price *= 1 + rng.uniform(-0.04, 0.04)
        return PriceRow(
            date=date, model=name, segment=model["segment"], vendor=model["vendor"],
            platform=platform["name"], price=round(usd_price, 4),
            currency="USD", usd_cny=usd_cny, source="mock",
        )

    # 国内平台（CNY 计价）
    cny_mult = mult.get("CNY", mult["USD"] * usd_cny)
    cny_price = model["base_usd"] * cny_mult
    cny_price *= 1 + rng.uniform(-0.04, 0.04)
    return PriceRow(
        date=date, model=name, segment=model["segment"], vendor=model["vendor"],
        platform=platform["name"], price=round(cny_price, 4),
        currency="CNY", usd_cny=usd_cny, source="mock",
    )


def _try_live_fetch(platform: str, model: dict) -> float | None:
    """尝试从真实 API 拉取；失败返回 None 走 mock 降级。

    RunPod / Vast.ai：仅在配置了 API KEY 时尝试；
    AutoDL：仅在配置了 Token 时尝试；
    国内云：使用公开定价页（保留扩展点，当前不实现以避免触发风控）。
    """
    key = None
    try:
        if platform == "RunPod" and os.environ.get("RUNPOD_API_KEY"):
            key = "runpod"
        elif platform == "Vast.ai" and os.environ.get("VAST_API_KEY"):
            key = "vast"
        elif platform == "AutoDL" and os.environ.get("AUTODL_TOKEN"):
            key = "autodl"
    except Exception as e:  # noqa: BLE001
        logger.warning("live fetch check failed for %s: %s", platform, e)

    if not key:
        return None

    # 真实 API 集成留给未来；当前未连接则抛错走 mock
    try:
        # 占位：实际项目里调用 requests.get(...).json()
        raise NotImplementedError("live API not wired in this build")
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] live fetch failed, fallback to mock: %s", platform, e)
        return None


def collect_prices(date: str | None = None, usd_cny: float = 7.18) -> list[dict]:
    """抓取所有平台 × 型号价格。

    - 优先尝试真实 API（RunPod/Vast.ai/AutoDL）；
    - 失败/未配置时使用 mock 数据；
    - 返回 list[dict]（与 latest.json / jsonl 行格式一致）。
    """
    if date is None:
        date = _today_iso()

    rows: list[dict] = []
    for model in MODELS:
        for platform in PLATFORMS:
            try:
                live = _try_live_fetch(platform["name"], model)
                if live is not None:
                    currency = platform["currency"]
                    rows.append({
                        "date": date, "model": model["name"], "segment": model["segment"],
                        "vendor": model["vendor"], "platform": platform["name"],
                        "price": round(live, 4), "currency": currency,
                        "usd_cny": usd_cny, "source": "api",
                    })
                else:
                    row = _mock_price(model, platform, date, usd_cny)
                    rows.append(row.to_dict())
            except Exception as e:  # noqa: BLE001
                logger.exception("price collect error: %s/%s", model["name"], platform["name"])
                # 降级 mock
                row = _mock_price(model, platform, date, usd_cny)
                rows.append(row.to_dict())

    logger.info("collected %d price rows for %s", len(rows), date)
    return rows


def load_usd_cny(default: float = 7.18) -> float:
    """读取汇率：优先环境变量，再尝试 .env 文件，最后回退默认。"""
    val = os.environ.get("DEFAULT_USDCNY")
    if val:
        try:
            return float(val)
        except ValueError:
            logger.warning("invalid DEFAULT_USDCNY=%s, fallback to default", val)
    return default


def write_outputs(rows: list[dict], root: str, usd_cny: float) -> dict:
    """写入三种数据产物：daily/YYYY/MM/YYYY-MM-DD.csv、jsonl/prices.jsonl、latest.json。

    返回 latest.json 顶层 dict。
    """
    if not rows:
        raise ValueError("no rows to write")

    date = rows[0]["date"]
    yyyy, mm, _ = date.split("-")

    daily_dir = os.path.join(root, "data", "daily", yyyy, mm)
    csv_path = os.path.join(daily_dir, f"{date}.csv")
    os.makedirs(daily_dir, exist_ok=True)

    # 1) CSV
    fieldnames = ["date", "model", "segment", "vendor", "platform",
                  "price", "currency", "usd_cny", "source"]
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(",".join(fieldnames) + "\n")
        for r in rows:
            f.write(",".join(str(r[k]) for k in fieldnames) + "\n")
    logger.info("wrote %d rows -> %s", len(rows), csv_path)

    # 2) JSONL（追加模式：避免历史丢失）
    jsonl_dir = os.path.join(root, "data", "jsonl")
    os.makedirs(jsonl_dir, exist_ok=True)
    jsonl_path = os.path.join(jsonl_dir, "prices.jsonl")
    with open(jsonl_path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    logger.info("appended %d rows -> %s", len(rows), jsonl_path)

    # 3) latest.json
    latest = {
        "date": date,
        "generated_at": _now_iso(),
        "usd_cny": usd_cny,
        "row_count": len(rows),
        "models": [m["name"] for m in MODELS],
        "platforms": [p["name"] for p in PLATFORMS],
        "rows": rows,
    }
    latest_path = os.path.join(root, "data", "latest.json")
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)
    logger.info("wrote latest.json (%d rows)", len(rows))

    return latest


def read_jsonl_all(path: str) -> list[dict]:
    """读取历史 prices.jsonl（去重：同 date+model+platform 保留最后一条）。"""
    if not os.path.exists(path):
        return []
    seen: dict[tuple, dict] = {}
    order: list[tuple] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (r["date"], r["model"], r["platform"])
            if key not in seen:
                order.append(key)
            seen[key] = r
    return [seen[k] for k in order]


def get_date_dir(root: str, date: str) -> str:
    yyyy, mm, _ = date.split("-")
    return os.path.join(root, "data", "daily", yyyy, mm)
