"""游戏库构建：entitlements → catalog bulk 解析 → 过滤游戏 → 去重 → 持久化。"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import api
from .zh_names import ZH_NAMES


def library_path(data_dir: str) -> str:
    return os.path.join(data_dir, "library.json")


def load_library(data_dir: str) -> dict | None:
    try:
        with open(library_path(data_dir), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_library(data_dir: str, lib: dict) -> None:
    os.makedirs(data_dir, exist_ok=True)
    with open(library_path(data_dir), "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, indent=2)


#: 明确不属于游戏库展示的类目（道具、引擎/工具、插件等）
_NON_GAME_CATEGORIES = ("testing", "engines", "applications", "plugins", "mods", "assets")


def _is_game(categories: list[str]) -> bool:
    """按类目判断是否游戏类内容（基础游戏/捆绑包/软件；排除货币、引擎资产等）。

    注意：Epic catalog 返回的 categories.path 顶层并不总是 games/、bundles/，
    真游戏也可能只有 audience/、public/（例如 Shenmue III）。所以这里以
    “命中游戏类目 或 不命中任何明确非游戏类目” 判定，避免整库被判为空。
    """
    if not categories:
        return False
    if any(c.startswith(("games/", "bundles/", "software/")) for c in categories):
        return True
    return not any((c.split("/")[0] or "").lower() in _NON_GAME_CATEGORIES for c in categories)


def _pick_offer_title(offers: list) -> str:
    """从 offer 列表里挑第一个有标题的"""
    for o in offers:
        if not isinstance(o, dict):
            continue
        title = (o.get("title") or "").strip()
        if title:
            return title
    return ""


def _strip_audience_suffix(title: str) -> str:
    """把 SerpentineGeneralAudience 这类内部代号收拾成可读名字"""
    for suffix in ("GeneralAudience", "General Audience", "Audience"):
        if title.endswith(suffix):
            return title[: -len(suffix)].strip()
    return title


def _resolve_namespace(token, ns, ids, kinds, country, locale) -> list[dict]:
    """把一个 namespace 解析成游戏条目。

    有可玩授权（非 AUDIENCE）时按 catalog item 的类目判断；
    纯受众授权时 catalog item 只给内部代号，改用 offer 标题反查真实游戏名。
    """
    entries: list[dict] = []
    if any(k != "AUDIENCE" for k in kinds):
        try:
            items = api.fetch_bulk_items(token, ns, ids, country, locale)
        except Exception:
            items = {}
        for iid, item in items.items():
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or "").strip()
            categories = [c.get("path", "") for c in item.get("categories") or []]
            if not title or title.lower().endswith("audience") or not _is_game(categories):
                continue
            app_id = next(
                (r.get("appId") for r in item.get("releaseInfo") or [] if r.get("appId")),
                None,
            )
            entries.append(
                {
                    "title": title,
                    "item_id": iid,
                    "namespace": ns,
                    "app_id": app_id,
                    "base": any("edition/base" in c for c in categories),
                }
            )
        if entries:
            return entries

    title = _pick_offer_title(api.fetch_namespace_offers(token, ns, country, locale))
    if title:
        entries.append(
            {
                "title": _strip_audience_suffix(title),
                "item_id": ids[0] if ids else None,
                "namespace": ns,
                "app_id": None,
                "base": False,
            }
        )
    return entries


def build_library(token: dict, country: str, locale: str, log=print) -> dict:
    """构建游戏库并返回 {updated_at, count, games: [...]}。

    - entitlements 全量拉取，按 namespace 分组
    - 可玩授权走 catalog bulk/items；受众授权（AUDIENCE）用 offer 标题反查真实游戏名
    - 并发解析（250 个 namespace 串行要一两分钟），标题去重优先基础版/带 appId
    """
    ents = api.fetch_entitlements(token)
    active = [e for e in ents if e.get("status") == "ACTIVE"]
    log(f"entitlements 共 {len(ents)} 条（ACTIVE {len(active)} 条）")

    groups: dict[str, dict] = {}
    for e in active:
        ns = e.get("namespace")
        if not ns:
            continue
        g = groups.setdefault(ns, {"ids": [], "kinds": set()})
        g["kinds"].add((e.get("entitlementType") or "").upper())
        iid = e.get("catalogItemId") or e.get("entitlementName")
        if iid and iid not in g["ids"]:
            g["ids"].append(iid)

    log(f"共 {len(groups)} 个 namespace，开始解析游戏名…")

    def work(item):
        ns, g = item
        try:
            return _resolve_namespace(token, ns, g["ids"], g["kinds"], country, locale)
        except Exception:
            return []

    resolved: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for batch in pool.map(work, list(groups.items())):
            resolved.extend(batch)

    games: dict[str, dict] = {}
    for g in resolved:
        title = g["title"]
        prev = games.get(title)
        if prev is None:
            games[title] = g
        elif (g["base"] and not prev["base"]) or (
            g["base"] == prev["base"] and g["app_id"] and not prev["app_id"]
        ):
            games[title] = g

    result = sorted(games.values(), key=lambda g: g["title"].lower())
    # 附带中文译名（内置通行译名表，查不到的保留英文原名）
    for g in result:
        g["title_zh"] = ZH_NAMES.get(g["title"], "")
    log(f"游戏库解析完成：{len(result)} 款（来自 {len(groups)} 个 namespace）")
    lib = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(result),
        "games": result,
    }
    return lib
