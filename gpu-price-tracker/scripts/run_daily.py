"""GPU 算力价格每日跟踪主入口。

执行流程：
1) 抓取所有平台价格（8 平台 × 14 型号）；
2) 写入 data/daily/YYYY/MM/YYYY-MM-DD.csv 与 data/jsonl/prices.jsonl；
3) 更新 data/latest.json；
4) 生成 reports/GPU价格趋势_YYYY.MM.DD.html；
5) git add data/ reports/ 并提交；
6) git push 到 origin/master（凭据缺失则记录失败但不影响本地数据）。
异常情况：所有错误都写到 logs/。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

# 允许直接 `python3 scripts/run_daily.py` 运行时找到同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data_source import collect_prices, load_usd_cny, read_jsonl_all, write_outputs
from generate_report import render_report

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
LOGS_DIR = os.path.join(ROOT, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)


def _find_git_root(start: str) -> str | None:
    """向上查找包含 .git 的目录；找不到返回 None。"""
    cur = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _setup_logging() -> logging.Logger:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = os.path.join(LOGS_DIR, f"run_{today}.log")
    logger = logging.getLogger("gpu_price_daily")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def _run(cmd: list[str], cwd: str, log: logging.Logger,
         extra_env: dict | None = None) -> tuple[int, str, str]:
    """在子进程中执行命令；返回 (returncode, stdout, stderr)。"""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=120, env=env)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as e:  # noqa: BLE001
        log.exception("command failed: %s", " ".join(cmd))
        return 1, "", str(e)


def git_commit_and_push(log: logging.Logger) -> None:
    """git add data/ reports/ && commit && push。失败不中断主流程。"""
    git_root = _find_git_root(ROOT)
    if not git_root:
        log.warning("no .git found walking up from %s, skip commit/push", ROOT)
        return

    # 相对于 git root 的路径（gpus 数据都在 gpu-price-tracker/ 子目录下）
    rel_data = os.path.relpath(os.path.join(ROOT, "data"), git_root)
    rel_reports = os.path.relpath(os.path.join(ROOT, "reports"), git_root)

    cmds = [
        ["git", "add", rel_data, rel_reports],
        ["git", "status", "--short"],
    ]
    for cmd in cmds:
        rc, out, err = _run(cmd, git_root, log)
        if rc != 0:
            log.error("git %s failed: rc=%s err=%s", " ".join(cmd), rc, err)
            return
        if cmd[1] == "status":
            log.info("git status: %s", out.strip() or "(no changes)")

    # 没有变更则跳过 commit
    rc, out, _ = _run(["git", "status", "--porcelain"], git_root, log)
    if rc != 0 or not out.strip():
        log.info("no changes to commit")
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    msg = f"chore(gpu-tracker): 每日数据 {today}"
    # 通过环境变量注入作者身份，避免修改全局 git config
    author_env = {
        "GIT_AUTHOR_NAME": "gpu-tracker-bot",
        "GIT_AUTHOR_EMAIL": "gpu-tracker@local",
        "GIT_COMMITTER_NAME": "gpu-tracker-bot",
        "GIT_COMMITTER_EMAIL": "gpu-tracker@local",
    }
    rc, out, err = _run(["git", "commit", "-m", msg], git_root, log, extra_env=author_env)
    if rc != 0:
        log.error("git commit failed: rc=%s out=%s err=%s", rc, out, err)
        return
    log.info("git commit ok: %s", out.strip())

    # 探测当前分支（避免硬编码 master 在某些环境出错）
    rc, out, err = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], git_root, log)
    branch = out.strip() if rc == 0 else "master"
    rc, out, err = _run(["git", "push", "origin", branch], git_root, log)
    if rc != 0:
        log.warning("git push failed (credentials missing or network?): rc=%s err=%s", rc, err)
    else:
        log.info("git push ok: %s", out.strip())


def main() -> int:
    log = _setup_logging()
    log.info("==== gpu-price-tracker daily run start ====")
    log.info("ROOT=%s", ROOT)

    try:
        usd_cny = load_usd_cny()
        log.info("usd_cny=%s", usd_cny)

        # 1) 抓取
        rows = collect_prices(usd_cny=usd_cny)
        if not rows:
            log.error("no rows collected, abort")
            return 2
        log.info("collected %d rows", len(rows))

        # 2/3) 写入产物
        latest = write_outputs(rows, ROOT, usd_cny)
        date = latest["date"]

        # 4) 报告
        history_path = os.path.join(ROOT, "data", "jsonl", "prices.jsonl")
        history = read_jsonl_all(history_path)
        report_path = os.path.join(ROOT, "reports", f"GPU价格趋势_{date.replace('-', '.')}.html")
        render_report(history, date, usd_cny, report_path)

        # 5/6) git
        git_commit_and_push(log)

        log.info("==== done ====")
        return 0
    except Exception as e:  # noqa: BLE001
        log.exception("daily run failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
