"""Epic Games 商店 API 封装（纯标准库，无第三方依赖）。

常量与请求形状对齐开源项目 claabs/epicgames-freegames-node（2026-11 版本）。
"""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

OAUTH_TOKEN = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token"
OAUTH_DEVICE_AUTH = (
    "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/deviceAuthorization"
)
FREE_GAMES_PROMOTIONS = (
    "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions"
)
GRAPHQL_ENDPOINT = "https://store.epicgames.com/graphql"
# Epic 自家基础设施服务（不走 Cloudflare，比商店 GraphQL 稳定）
CATALOG_SERVICE = "https://catalog-public-service-prod06.ol.epicgames.com"
ENTITLEMENT_SERVICE = "https://entitlement-public-service-prod08.ol.epicgames.com"
STORE_CONTENT = "https://store-content-ipv4.ak.epicgames.com/api/en-US/content"
EULA_AGREEMENTS = (
    "https://eulatracking-public-service-prod-m.ol.epicgames.com/eulatracking/api/public/agreements"
)
STORE_CART_EN = "https://store.epicgames.com/en-US/cart"

# Epic 官方客户端凭据（公开客户端列表，claabs 默认使用的 fortniteNewSwitchGameClient）
CLIENT_ID = "98f7e42c2e3a4f86a74eb43fbb41ed39"
CLIENT_SECRET = "0a2449a2-001a-451e-afec-3e812901c4d7"
_BASIC = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

EULA_KEYS = ["epicgames_privacy_policy_no_table", "egstore"]

# GraphQL persisted query 哈希（与商店页面一致，来源 claabs 源码）
HASH_GET_MAPPING_BY_PAGE_SLUG = "781fd69ec8116125fa8dc245c0838198cdf5283e31647d08dfa27f45ee8b1f30"
HASH_GET_CATALOG_OFFER = "abafd6e0aa80535c43676f533f0283c7f5214a59e9fae6ebfb37bed1b1bb2e9b"
HASH_GET_OFFERS_VALIDATION = "3c9bb0f213f6d0cb6bf056e6b206ba166c8dd59d014618e4d59bff11689f403a"


class OAuthPending(Exception):
    """设备码授权尚未完成（用户未在网页确认）"""


class OAuthError(Exception):
    """OAuth 错误（refresh 失效等），需要重新登录"""


def _http(url: str, *, method: str = "GET", headers=None, data=None, timeout=30):
    """通用 HTTP，返回 (status, body_text)"""
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _form(data: dict) -> bytes:
    return urllib.parse.urlencode(data).encode()


# ================= OAuth =================


def client_credentials_token() -> str:
    status, body = _http(
        OAUTH_TOKEN,
        method="POST",
        headers={"Authorization": f"Basic {_BASIC}"},
        data=_form({"grant_type": "client_credentials"}),
    )
    if status != 200:
        raise OAuthError(f"client_credentials 失败: {status} {body[:200]}")
    return json.loads(body)["access_token"]


def create_device_auth() -> dict:
    """发起设备码授权，返回 device_code/verification_uri_complete 等"""
    cc = client_credentials_token()
    status, body = _http(
        OAUTH_DEVICE_AUTH,
        method="POST",
        headers={"Authorization": f"Bearer {cc}"},
        data=b"",
    )
    if status != 200:
        raise OAuthError(f"设备授权初始化失败: {status} {body[:200]}")
    d = json.loads(body)
    d.setdefault("interval", 10)
    d.setdefault("expires_in", 600)
    return d


def poll_device_token(device_code: str) -> dict:
    """轮询设备码授权；未完成抛 OAuthPending，失败抛 OAuthError"""
    status, body = _http(
        OAUTH_TOKEN,
        method="POST",
        headers={"Authorization": f"Basic {_BASIC}"},
        data=_form({"grant_type": "device_code", "device_code": device_code}),
    )
    if status == 200:
        return json.loads(body)
    try:
        code = json.loads(body).get("errorCode", "")
    except Exception:
        code = ""
    if code == "errors.com.epicgames.account.oauth.authorization_pending":
        raise OAuthPending("等待授权")
    if code == "errors.com.epicgames.account.oauth.slow_down":
        raise OAuthPending("slow_down")
    raise OAuthError(f"设备码授权失败: {status} {body[:200]}")


def refresh_token(refresh_token_value: str) -> dict:
    """refresh_token 换新 token（会轮转，需整体保存）"""
    status, body = _http(
        OAUTH_TOKEN,
        method="POST",
        headers={"Authorization": f"Basic {_BASIC}"},
        data=_form({"grant_type": "refresh_token", "refresh_token": refresh_token_value}),
    )
    if status == 200:
        return json.loads(body)
    try:
        code = json.loads(body).get("errorCode", "")
    except Exception:
        code = ""
    raise OAuthError(f"刷新 token 失败: {status} {code or body[:120]}")


# ================= EULA =================


def ensure_eulas_accepted(token: dict, log=print) -> None:
    """检查并自动接受商店相关 EULA；204/空响应 = 已接受"""
    access, account_id = token["access_token"], token["account_id"]
    for key in EULA_KEYS:
        status, body = _http(
            f"{EULA_AGREEMENTS}/{key}/account/{account_id}",
            headers={"Authorization": f"Bearer {access}"},
        )
        if status == 204:
            continue  # 已接受
        if status != 200:
            log(f"EULA 查询失败 {key}: {status}")
            continue
        try:
            d = json.loads(body) if body.strip() else None
        except Exception:
            d = None
        if not d or "version" not in d:
            continue  # 已接受
        version, locale = d["version"], d.get("locale", "en")
        log(f"接受 EULA: {key} v{version}")
        _http(
            f"{EULA_AGREEMENTS}/{key}/version/{version}/account/{account_id}/accept"
            f"?locale={urllib.parse.quote(locale)}",
            method="POST",
            headers={"Authorization": f"Bearer {access}"},
            data=b"",
        )


# ================= 免费游戏（公开接口） =================


def fetch_weekly_free_games_json(country="US", locale="en-US") -> str:
    q = urllib.parse.urlencode({"locale": locale, "country": country, "allowCountries": country})
    status, body = _http(f"{FREE_GAMES_PROMOTIONS}?{q}", headers={"User-Agent": DESKTOP_UA})
    if status != 200:
        raise RuntimeError(f"免费游戏列表获取失败: {status}")
    return body


def checkout_url(namespace: str, offer_id: str) -> str:
    return (
        "https://www.epicgames.com/store/purchase?highlightColor=0078f2"
        f"&offers=1-{namespace}-{offer_id}&orderId&purchaseToken&showNavigation=true"
    )


def graphql_get(op: str, sha: str, variables: dict, cookie_header: str | None = None) -> str:
    """直接调用商店 GraphQL（公开查询无需登录；entitlements 类查询需带会话 cookie）。

    直接 HTTP 比页面上下文 fetch 更稳（不依赖页面加载状态/同源策略）。
    非 200 抛 RuntimeError（带状态码与响应片段，便于诊断）。
    """
    params = urllib.parse.urlencode(
        {
            "operationName": op,
            "variables": json.dumps(variables),
            "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": sha}}),
        }
    )
    headers = {"User-Agent": DESKTOP_UA, "Accept": "application/json"}
    if cookie_header:
        headers["Cookie"] = cookie_header
    status, body = _http(f"{GRAPHQL_ENDPOINT}?{params}", headers=headers)
    if status != 200:
        raise RuntimeError(f"GraphQL {op} HTTP {status}: {body[:200]}")
    return body


# ================= 所有权查询（Epic 自家服务，Bearer 认证） =================


def fetch_offer_item_ids(token: dict, namespace: str, offer_id: str) -> list[str]:
    """catalog REST：offer → 关联的 catalog item id 列表"""
    url = f"{CATALOG_SERVICE}/catalog/api/shared/namespace/{namespace}/offers/{offer_id}"
    status, body = _http(
        url,
        headers={"Authorization": f"Bearer {token['access_token']}", "Accept": "application/json"},
    )
    if status != 200:
        raise RuntimeError(f"catalog offer 查询失败: {status} {body[:150]}")
    d = json.loads(body)
    return [i["id"] for i in d.get("items") or [] if i.get("id")]


def fetch_entitlement_names(token: dict) -> set[str]:
    """entitlements REST：账号全部 entitlementName（= catalog item id）集合"""
    names: set[str] = set()
    start, count = 0, 5000
    while True:
        url = (
            f"{ENTITLEMENT_SERVICE}/entitlement/api/account/{token['account_id']}"
            f"/entitlements?start={start}&count={count}"
        )
        status, body = _http(
            url,
            headers={"Authorization": f"Bearer {token['access_token']}", "Accept": "application/json"},
        )
        if status != 200:
            raise RuntimeError(f"entitlements 查询失败: {status} {body[:150]}")
        items = json.loads(body)
        for e in items:
            name = e.get("entitlementName")
            if name:
                names.add(name)
        if len(items) < count:
            break
        start += count
    return names
