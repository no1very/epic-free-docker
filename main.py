"""Epic 白嫖助手 Docker 版 —— 自动领取 Epic 每周免费游戏（含可视化面板）。

命令：
  login   首次登录（设备码授权，输出链接在任意设备浏览器打开完成授权）
  run     常驻运行：按 CLAIM_INTERVAL_HOURS 周期领取（容器默认入口，含 Web 面板）
  once    单次领取（适合外置 cron）
  detect  只检测本周免费游戏，不登录不领取
"""

import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from epic import api, notifier, parser, state, store
from epic.claimer import Claimer

DATA_DIR = os.environ.get("DATA_DIR", "./data")
COUNTRY = os.environ.get("EPIC_COUNTRY", "US").upper()
LOCALE = os.environ.get("EPIC_LOCALE", "en-US")
INTERVAL_HOURS = float(os.environ.get("CLAIM_INTERVAL_HOURS", "6"))
WEB_PORT = int(os.environ.get("WEB_PORT", "8321"))
SCREENSHOT_DIR = os.path.join(DATA_DIR, "logs")

CFG = {
    "DATA_DIR": DATA_DIR,
    "COUNTRY": COUNTRY,
    "LOCALE": LOCALE,
    "INTERVAL_HOURS": INTERVAL_HOURS,
    "NOTIFY_METHOD": os.environ.get("NOTIFY_METHOD", "none"),
}

RUN_LOCK = threading.Lock()


def log(msg: str) -> None:
    state.log(msg)


# ================= login =================

LOGIN_LOCK = threading.Lock()


def run_login_flow(log_fn=None, on_url=None) -> dict:
    """设备码授权登录（阻塞直到成功/失败/过期），同步更新面板状态。

    log_fn: 进度日志输出（CLI 用）；on_url: 链接生成回调（CLI 打印用）。
    面板通过 state 登录状态字段实时展示链接与进度。
    """
    if not LOGIN_LOCK.acquire(blocking=False):
        msg = "已有授权流程进行中"
        if log_fn:
            log_fn(msg)
        return {"state": "busy", "message": msg}
    try:

        def set_state(**kw):
            with state.LOCK:
                state.STATE.update(**kw)

        set_state(login_state="starting", login_url=None, login_code=None,
                  login_message="", login_expires_at=None)
        init = api.create_device_auth()
        url = init["verification_uri_complete"]
        expires_min = int(init.get("expires_in", 600)) // 60
        set_state(login_state="waiting", login_url=url, login_code=init.get("user_code"),
                  login_expires_at=time.time() + int(init.get("expires_in", 600)))
        if on_url:
            on_url(url, init.get("user_code", "-"), expires_min)
        if log_fn:
            log_fn("授权链接已生成，请打开链接完成 Epic 登录")

        interval = max(5, int(init.get("interval", 10)))
        deadline = time.time() + int(init.get("expires_in", 600))
        while time.time() < deadline:
            time.sleep(interval)
            try:
                token = api.poll_device_token(init["device_code"])
            except api.OAuthPending:
                if log_fn:
                    log_fn("等待授权中…")
                continue
            except api.OAuthError as e:
                set_state(login_state="failed", login_message=str(e))
                if log_fn:
                    log_fn(f"授权失败：{e}")
                return {"state": "failed", "message": str(e)}
            store.save(DATA_DIR, token)
            name = token.get("displayName", token.get("account_id", ""))
            set_state(login_state="success", login_message=f"登录成功：{name}")
            if log_fn:
                log_fn(f"登录成功：{name}，凭据已保存到 {store.auth_path(DATA_DIR)}")
            return {"state": "success"}
        set_state(login_state="expired", login_message="授权超时，请重新生成链接")
        if log_fn:
            log_fn("授权超时，请重试")
        return {"state": "expired"}
    except Exception as e:
        with state.LOCK:
            state.STATE.update(login_state="failed", login_message=str(e))
        if log_fn:
            log_fn(f"登录流程异常：{e}")
        return {"state": "failed", "message": str(e)}
    finally:
        LOGIN_LOCK.release()


def cmd_login() -> int:
    def print_url(url: str, code: str, expires_min: int) -> None:
        print()
        print("=" * 62)
        print("请在任意设备浏览器打开以下链接完成 Epic 登录授权：")
        print()
        print(url)
        print()
        print(f"（用户码: {code}，有效期约 {expires_min} 分钟）")
        print("账号密码/两步验证/人机验证均在 Epic 官方页面完成，本程序不接触你的凭据。")
        print("=" * 62)

    result = run_login_flow(log_fn=log, on_url=print_url)
    return 0 if result["state"] == "success" else 1


# ================= claim run =================


def token_ok(token: dict) -> bool:
    """refresh_token 是否仍在有效期（过期则必须重新登录）"""
    exp = parser.parse_iso(token.get("refresh_expires_at", ""))
    return bool(exp and exp > datetime.now(timezone.utc))


def fresh_token(token: dict) -> dict | None:
    """access token 过期前 5 分钟则刷新；刷新失败但旧 access 仍有效则沿用"""
    access_exp = parser.parse_iso(token.get("expires_at", "")) or datetime.min.replace(tzinfo=timezone.utc)
    if access_exp > datetime.now(timezone.utc) + timedelta(minutes=5):
        return token
    try:
        new = api.refresh_token(token["refresh_token"])
        store.save(DATA_DIR, new)
        log("access token 已刷新")
        return new
    except api.OAuthError as e:
        if access_exp <= datetime.now(timezone.utc):
            log(f"token 刷新失败且已过期：{e}")
            return None
        log(f"token 刷新失败（沿用旧 token）：{e}")
        return token


def claim_once() -> bool:
    """执行一次完整领取流程；返回是否有'需要登录'的情况"""
    token = store.load(DATA_DIR)
    if not token or not token_ok(token):
        log("未登录或凭据已失效，请先执行: python main.py login")
        notifier.send("Epic 白嫖助手", "未登录或登录已失效，请执行 login 重新授权")
        return True

    token = fresh_token(token) or token
    api.ensure_eulas_accepted(token, log)

    claimer = Claimer(country=COUNTRY, locale=LOCALE, log=log, screenshot_dir=SCREENSHOT_DIR)
    try:
        claimer.start()
        if not claimer.establish_session(token):
            log("会话建立失败，token 可能已失效，尝试重新登录")
            notifier.send("Epic 白嫖助手", "会话建立失败，可能需要重新登录（docker compose run --rm epic python main.py login）")
            return True

        games = claimer.get_weekly_free_games()
        log(f"本周免费：{'、'.join(g['title'] for g in games) or '无'}")
        status_map = {g["title"]: "claimable" for g in games}

        offers, seen = [], []
        for g in games:
            offers.extend(claimer.resolve_offer(g))
        offers = list({o["offerId"]: o for o in offers}.values())
        log(f"解析出 {len(offers)} 个有效 offer")

        purchasable, owned = claimer.get_purchasable(token, offers)
        for o in owned:
            log(f"{o['title']}：已拥有")
            status_map[o["title"]] = "owned"
        if not purchasable:
            log("没有需要领取的免费游戏")
            save_summary(claimed=[], owned=[o["title"] for o in owned], failed=[])
            state.set_free_games(games, status_map)
            return False

        claimed, failed = [], []
        for o in purchasable:
            log(f"{o['title']}：开始自动下单…")
            ok, msg = claimer.claim_offer(o)
            if ok:
                claimed.append(o["title"])
                status_map[o["title"]] = "claimed"
                log(f"{o['title']}：领取成功 ✓")
            else:
                failed.append((o["title"], msg))
                status_map[o["title"]] = "failed"
                log(f"{o['title']}：领取失败（{msg}）")

        save_summary([t for t in claimed], [o["title"] for o in owned], failed)
        state.set_free_games(games, status_map)
        lines = []
        if claimed:
            lines.append(f"🎉 成功领取 {len(claimed)} 款：" + "、".join(claimed))
        if failed:
            lines.append("❌ 失败 " + "；".join(f"{t}（{m}）" for t, m in failed))
        if not lines:
            lines.append("本次无可领取的免费游戏")
        notifier.send("Epic 白嫖助手", "\n".join(lines))
        return False
    finally:
        claimer.close()


def save_summary(claimed, owned, failed):
    import json

    summary = {
        "at": datetime.now(timezone.utc).isoformat(),
        "claimed": claimed,
        "owned": owned,
        "failed": [{"title": t, "message": m} for t, m in failed],
    }
    state.set_summary(summary)
    path = os.path.join(DATA_DIR, "last_run.json")
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"保存 last_run.json 失败: {e}")


def run_claim_guarded() -> dict:
    """带锁的单次领取（周期任务与面板手动触发共用）；busy 时直接拒绝"""
    if not RUN_LOCK.acquire(blocking=False):
        return {"started": False, "need_login": False, "message": "已有任务在运行，请稍候"}
    try:
        with state.LOCK:
            state.STATE["busy"] = True
        need_login = claim_once()
        with state.LOCK:
            state.STATE["last_run_at"] = time.time()
            state.STATE["busy"] = False
        return {"started": True, "need_login": need_login, "message": "完成"}
    except Exception as e:
        with state.LOCK:
            state.STATE["busy"] = False
            state.STATE["last_error"] = str(e)
        log(f"运行异常：{e}")
        traceback.print_exc()
        return {"started": True, "need_login": False, "message": f"运行异常：{e}"}
    finally:
        RUN_LOCK.release()


def cmd_run() -> int:
    # 启动可视化面板（WEB_PORT=0 关闭）
    if WEB_PORT > 0:
        from epic import web

        threading.Thread(target=web.serve_forever, args=(CFG, WEB_PORT), daemon=True).start()

    log(f"启动：每 {INTERVAL_HOURS:g} 小时检测领取一次（region={COUNTRY}）")
    while True:
        result = run_claim_guarded()
        if result["started"] and result["need_login"]:
            # 需要重新登录：按周期重试（登录后自动恢复）
            log("将在下个周期自动重试；完成登录后无需重启容器")
        next_at = time.time() + max(1, INTERVAL_HOURS) * 3600
        with state.LOCK:
            state.STATE["next_run_at"] = next_at
        time.sleep(max(1, INTERVAL_HOURS) * 3600)


# ================= detect =================


def resolve_offers_offline(games: list[dict], log=print) -> list[dict]:
    """离线解析真实 offer（映射 + getCatalogOffer 均为公开接口，无需登录）。
    用于 detect 命令验证解析链路；与 claimer.resolve_offer 逻辑一致。"""
    offers = []
    for g in games:
        candidates = [(g["offerId"], g["namespace"])]
        if g.get("pageSlug") and g.get("productSlug"):
            try:
                m = parser.parse_page_slug_mapping(
                    api.graphql_get(
                        "getMappingByPageSlug",
                        api.HASH_GET_MAPPING_BY_PAGE_SLUG,
                        {"pageSlug": g["pageSlug"], "locale": LOCALE},
                    )
                )
                if m and m[0] == "offer":
                    candidates = [(m[2], m[1])]
            except Exception as e:
                log(f"  ! {g['title']} 映射查询失败: {e}")
        for oid, ns in candidates:
            try:
                raw = api.graphql_get(
                    "getCatalogOffer",
                    api.HASH_GET_CATALOG_OFFER,
                    {"locale": LOCALE, "country": COUNTRY, "offerId": oid, "sandboxId": ns},
                )
            except Exception as e:
                log(f"  ! {g['title']} offer 校验查询失败: {e}")
                continue
            v = parser.parse_catalog_offer(raw, COUNTRY)
            if v:
                offers.append({**v, "title": g["title"], "endDate": g.get("endDate") or v.get("endDate")})
            else:
                log(f"  ! {g['title']} offer {oid[:12]} 未通过免费校验（非免费/过期/地区屏蔽）")
    return list({o["offerId"]: o for o in offers}.values())


def cmd_detect() -> int:
    body = api.fetch_weekly_free_games_json(COUNTRY, LOCALE)
    games = parser.parse_weekly_free_games(body)
    state.set_free_games(games, {g["title"]: "claimable" for g in games})
    if not games:
        print("当前没有可领取的免费游戏")
        return 0
    print(f"当前免费游戏（{COUNTRY} 区）:")
    for g in games:
        end = (parser.parse_iso(g["endDate"]) or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
        print(f"  - {g['title']}  [截止 {end}]  offer={g['offerId'][:12]} ns={g['namespace'][:12]}")

    # 解析真实 offer（验证完整解析链路，无需登录）
    offers = resolve_offers_offline(games)
    print(f"\n解析出 {len(offers)} 个可领取 offer:")
    for o in offers:
        print(f"  ✓ {o['title']}  offer={o['offerId'][:12]} ns={o['namespace'][:12]}")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    os.makedirs(DATA_DIR, exist_ok=True)
    if cmd == "login":
        return cmd_login()
    if cmd == "run":
        return cmd_run()
    if cmd == "once":
        result = run_claim_guarded()
        return 0 if result["started"] and not result["need_login"] else 1
    if cmd == "detect":
        return cmd_detect()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
