"""JSON 解析（纯函数，对齐 claabs/epicgames-freegames-node 的判定逻辑）。"""

import json
from datetime import datetime, timezone

from . import api


def parse_iso(s: str):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_weekly_free_games(promotions_json: str) -> list[dict]:
    """解析 freeGamesPromotions 响应，返回当前时段免费的元素。

    判定：promotions.promotionalOffers[].promotionalOffers[] 中存在
    startDate <= now <= endDate 且 discountPercentage == 0 的促销。
    """
    body = json.loads(promotions_json)
    elements = (
        body.get("data", {})
        .get("Catalog", {})
        .get("searchStore", {})
        .get("elements", [])
        or []
    )
    now = _now()
    result = []
    for e in elements:
        end_date = None
        free_now = False
        for group in (e.get("promotions") or {}).get("promotionalOffers") or []:
            for p in group.get("promotionalOffers") or []:
                start = parse_iso(p.get("startDate", ""))
                end = parse_iso(p.get("endDate", ""))
                discount = (p.get("discountSetting") or {}).get("discountPercentage")
                if discount == 0 and start and end and start <= now <= end:
                    free_now = True
                    end_date = p.get("endDate")
        if not free_now:
            continue
        title = (e.get("title") or "").strip()
        if not title:
            continue
        page_slug = None
        for m in (e.get("catalogNs") or {}).get("mappings") or []:
            if m.get("pageType") == "productHome" and m.get("pageSlug"):
                page_slug = m["pageSlug"]
                break
        result.append(
            {
                "offerId": e.get("id", ""),
                "namespace": e.get("namespace", ""),
                "title": title,
                "productSlug": e.get("productSlug") or e.get("urlSlug") or None,
                "pageSlug": page_slug,
                "offerType": e.get("offerType") or None,
                "endDate": end_date,
            }
        )
    return result


def parse_page_slug_mapping(mapping_json: str):
    """解析 getMappingByPageSlug：返回 ('offer', ns, id) / ('slug', pageSlug) / None"""
    mapping = (
        json.loads(mapping_json)
        .get("data", {})
        .get("StorePageMapping", {})
        .get("mapping")
    )
    if not mapping:
        return None
    offer = (mapping.get("mappings") or {}).get("offer")
    if offer and offer.get("id"):
        return ("offer", offer.get("namespace", ""), offer.get("id"))
    slug = mapping.get("pageSlug")
    return ("slug", slug) if slug else None


def parse_catalog_offer(catalog_offer_json: str, country_code: str):
    """getCatalogOffer：offer 无效（非免费/过期/地区屏蔽）返回 None"""
    offer = (
        json.loads(catalog_offer_json)
        .get("data", {})
        .get("Catalog", {})
        .get("catalogOffer")
    )
    if not offer:
        return None
    price = (offer.get("price") or {}).get("totalPrice") or {}
    if price.get("discountPrice") != 0:
        return None
    expiry = offer.get("expiryDate")
    if expiry:
        exp = parse_iso(expiry)
        if exp and exp <= _now():
            return None
    for c in offer.get("countriesBlacklist") or []:
        if str(c).upper() == country_code.upper():
            return None
    title = (offer.get("title") or "").strip()
    if not title:
        return None
    return {
        "offerId": offer.get("id", ""),
        "namespace": offer.get("namespace", ""),
        "title": title,
        "productSlug": offer.get("productSlug") or None,
        "endDate": expiry or None,
    }


def parse_cms_product_offers(cms_json: str, is_bundle: bool, title: str) -> list[dict]:
    """store-content CMS 产品响应（旧策略），返回 hasOffer 的 offer 列表"""
    root = json.loads(cms_json)
    offers = []
    if is_bundle:
        o = root.get("offer")
        if o and o.get("hasOffer"):
            offers.append(_cms_offer(o, title))
    else:
        for page in root.get("pages") or []:
            o = (page or {}).get("offer") or {}
            if o.get("hasOffer"):
                offers.append(_cms_offer(o, title))
    return offers


def _cms_offer(o: dict, title: str) -> dict:
    return {
        "offerId": o.get("id", ""),
        "namespace": o.get("namespace", ""),
        "title": title,
        "productSlug": None,
        "pageSlug": None,
        "offerType": None,
        "endDate": None,
    }


def _validation(json_text: str):
    return (
        json.loads(json_text)
        .get("data", {})
        .get("Entitlements", {})
        .get("cartOffersValidation")
    )


def parse_can_purchase(validation_json: str) -> bool:
    """getOffersValidation：true = 可购买（未拥有、无冲突、无前置要求）"""
    body = json.loads(validation_json)
    if body.get("errors"):
        return False
    v = _validation(validation_json)
    if not v:
        return False
    return all(
        not v.get(k) for k in
        ("missingPrerequisites", "conflictingOffers", "fullyOwnedOffers", "unablePartiallyUpgradeOffers")
    )


def parse_fully_owned(validation_json: str) -> bool:
    """fullyOwnedOffers 非空 = 已拥有（领取成功）"""
    v = _validation(validation_json)
    return bool(v and v.get("fullyOwnedOffers"))
