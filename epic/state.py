"""共享运行状态：环形日志缓冲 + 面板状态（web 线程与领取线程之间共享）。"""

import threading
import time
from collections import deque
from datetime import datetime

LOCK = threading.RLock()
LOGS = deque(maxlen=500)  # 最近 500 行日志（不含时间戳前缀的原始行已带时间）

STATE = {
    "started_at": time.time(),
    "busy": False,
    "last_run_at": None,
    "next_run_at": None,
    "last_summary": None,  # {claimed: [...], owned: [...], failed: [...]}
    "free_games": None,    # [{title, offer_id, end_date, status}]
    "free_games_at": None,
    "last_error": None,
    # 登录授权流程（面板展示）
    "login_state": "idle",  # idle / starting / waiting / success / failed / expired
    "login_url": None,
    "login_code": None,
    "login_message": "",
    "login_expires_at": None,
}


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    with LOCK:
        LOGS.append(line)
    print(line, flush=True)


def set_free_games(games: list[dict], status_map: dict, end_map: dict | None = None) -> None:
    """记录免费游戏列表（含领取状态），供面板展示"""
    items = []
    for g in games:
        items.append(
            {
                "title": g.get("title", ""),
                "offer_id": g.get("offerId") or g.get("offer_id", ""),
                "end_date": (end_map or {}).get(g.get("title")) or g.get("endDate") or g.get("end_date"),
                "status": status_map.get(g.get("title"), "claimable"),
            }
        )
    with LOCK:
        STATE["free_games"] = items
        STATE["free_games_at"] = time.time()


def set_summary(summary: dict) -> None:
    with LOCK:
        STATE["last_summary"] = summary


def snapshot() -> dict:
    with LOCK:
        return dict(STATE)
