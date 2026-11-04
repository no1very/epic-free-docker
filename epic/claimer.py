"""Playwright 领取引擎：会话建立 → 免费检测 → 已拥有过滤 → 结账页自动下单。

移植自安卓版 ClaimEngine/CheckoutJs，会话机制对齐 claabs/epicgames-freegames-node：
EPIC_BEARER_TOKEN cookie 注入 → 访问商店页换取 EPIC_EG1 会话。
"""

import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from . import api, parser

CHECKOUT_JS = """
(function(){
  if (window.__efgInit) return;
  window.__efgInit = true;
  var st = window.__efg = { phase: 'scanning', clicks: 0, detail: '' };
  var lastClick = 0;
  var MAX_CLICKS = 3;

  function clickEl(el){
    try { el.click ? el.click() : el.dispatchEvent(new MouseEvent('click', {bubbles: true})); }
    catch(e) { el.dispatchEvent(new MouseEvent('click', {bubbles: true})); }
  }

  function tickCheckboxes(){
    var cbs = document.querySelectorAll('input[type=checkbox]');
    for (var i = 0; i < cbs.length; i++) {
      if (!cbs[i].checked) { try { clickEl(cbs[i]); } catch(e){} }
    }
  }

  function findButton(){
    var sels = [
      '.pay-view .btn-primary',
      '.payment-payment-methods .btn-primary',
      '[data-component="PN_PlaceOrder"] button',
      '#purchase-app .btn-primary'
    ];
    var i, j, els;
    for (i = 0; i < sels.length; i++) {
      els = document.querySelectorAll(sels[i]);
      for (j = 0; j < els.length; j++) {
        if (els[j].offsetParent !== null) return els[j];
      }
    }
    var texts = ['place order', 'place free order', 'continue', '下单', '下订单', '免费下单', '免费获取', '继续'];
    var btns = document.querySelectorAll('button, [role=button], .btn-primary');
    var k, m, t;
    for (k = 0; k < btns.length; k++) {
      if (btns[k].offsetParent === null) continue;
      t = (btns[k].textContent || '').trim().toLowerCase();
      for (m = 0; m < texts.length; m++) {
        if (t.indexOf(texts[m]) >= 0) return btns[k];
      }
    }
    return null;
  }

  function tick(){
    if (st.phase === 'done' || st.phase === 'timeout') return;
    tickCheckboxes();
    var now = Date.now();
    if (now - lastClick < 5000) return;
    var b = findButton();
    if (!b) { st.phase = 'scanning'; return; }
    st.phase = 'found';
    st.detail = (b.textContent || '').trim();
    var disabled = b.disabled === true || b.getAttribute('aria-disabled') === 'true' ||
                   (b.className || '').indexOf('disabled') >= 0;
    if (disabled) return;
    if (st.clicks >= MAX_CLICKS) { st.phase = 'done'; return; }
    clickEl(b);
    lastClick = now;
    st.clicks++;
    st.phase = 'clicked';
  }

  var iv = setInterval(tick, 1500);
  setTimeout(function(){
    clearInterval(iv);
    if (st.phase !== 'done' && st.phase !== 'timeout') st.phase = 'timeout';
  }, 110000);
  tick();
})();
"""

BEARER_COOKIE_DOMAINS = [".epicgames.com", ".twinmotion.com", ".fortnite.com", ".unrealengine.com"]


def _iso_expired(iso: str) -> bool:
    exp = parser.parse_iso(iso or "")
    return bool(exp and exp <= datetime.now(timezone.utc))


class Claimer:
    """一次领取运行占有一个浏览器实例；用完调用 close()。"""

    def __init__(self, country="US", locale="en-US", log=print, screenshot_dir=None):
        self.country = country
        self.locale = locale
        self.log = log
        self.screenshot_dir = screenshot_dir
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None

    # ---------- 生命周期 ----------

    def start(self):
        from playwright.sync_api import sync_playwright  # 延迟导入

        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        self.context = self.browser.new_context(user_agent=api.DESKTOP_UA, locale="en-US")
        self.page = self.context.new_page()

    def close(self):
        for closer in (self.context, self.browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # ---------- 会话 ----------

    def establish_session(self, token: dict) -> bool:
        """注入 EPIC_BEARER_TOKEN，访问购物车页等待 Epic 换发 EPIC_EG1 会话 cookie。"""
        expires = int(parser.parse_iso(token.get("expires_at", "")).timestamp()) if token.get("expires_at") else int(time.time()) + 7200
        cookies = [
            {
                "name": "EPIC_BEARER_TOKEN",
                "value": token["access_token"],
                "domain": d,
                "path": "/",
                "secure": True,
                "httpOnly": False,
                "expires": expires,
            }
            for d in BEARER_COOKIE_DOMAINS
        ]
        self.context.add_cookies(cookies)
        self.log("建立 Epic 商店会话（等待 EPIC_EG1 cookie）…")
        self.page.goto(api.STORE_CART_EN, timeout=60000, wait_until="domcontentloaded")
        deadline = time.time() + 45
        while time.time() < deadline:
            for c in self.context.cookies(api.STORE_CART_EN):
                if c["name"] == "EPIC_EG1":
                    # 等页面 JS 跑完（防 Cloudflare/SPA 状态未稳定就发起页面内请求）
                    time.sleep(3)
                    self.log("会话已建立")
                    return True
            time.sleep(0.8)
        self.log("超时未获得 EPIC_EG1，会话建立失败")
        return False

    # ---------- HTTP：直接请求优先，页面上下文 fetch 兜底 ----------

    def page_fetch(self, url: str, params: dict | None = None) -> str:
        """页面上下文 fetch（同源带 cookie；失败抛错而非静默返回错误页）"""
        if params:
            url = f"{url}?{urlencode(params)}"
        out = self.page.evaluate(
            "async (u) => { const r = await fetch(u, {method: 'GET'});"
            " return {status: r.status, body: await r.text()}; }",
            url,
        )
        if not isinstance(out, dict) or out.get("status", 500) >= 400:
            body = (out or {}).get("body", "") if isinstance(out, dict) else str(out)
            raise RuntimeError(f"页面 fetch HTTP {out.get('status') if isinstance(out, dict) else '?'}: {body[:200]}")
        return out["body"]

    def _cookie_header(self) -> str | None:
        try:
            parts = [
                f"{c['name']}={c['value']}"
                for c in self.context.cookies("https://store.epicgames.com/")
            ]
            return "; ".join(parts) or None
        except Exception:
            return None

    def graphql(self, operation: str, sha: str, variables: dict, need_session: bool = False) -> str:
        """GraphQL 双通道：直接 HTTP（快、稳、可诊断）→ 失败回退页面 fetch。"""
        cookie = self._cookie_header() if need_session else None
        try:
            return api.graphql_get(operation, sha, variables, cookie_header=cookie)
        except Exception as e:
            self.log(f"GraphQL {operation} 直接请求失败，回退页面 fetch：{e}")
            return self.page_fetch(
                "https://store.epicgames.com/graphql",
                {
                    "operationName": operation,
                    "variables": json.dumps(variables),
                    "extensions": json.dumps(
                        {"persistedQuery": {"version": 1, "sha256Hash": sha}}
                    ),
                },
            )

    # ---------- 检测 ----------

    def get_weekly_free_games(self) -> list[dict]:
        # 公开接口，直接 HTTP 即可（无需会话/页面）
        body = api.fetch_weekly_free_games_json(self.country, self.locale)
        return parser.parse_weekly_free_games(body)

    def get_mapping_by_page_slug(self, page_slug: str):
        return self.graphql(
            "getMappingByPageSlug",
            api.HASH_GET_MAPPING_BY_PAGE_SLUG,
            {"pageSlug": page_slug, "locale": self.locale},
        )

    def fetch_cms_product(self, product_slug: str, is_bundle: bool) -> str:
        path = "bundles" if is_bundle else "products"
        return self.page_fetch(f"{api.STORE_CONTENT}/{path}/{product_slug}")

    def get_catalog_offer(self, offer_id: str, namespace: str) -> str:
        return self.graphql(
            "getCatalogOffer",
            api.HASH_GET_CATALOG_OFFER,
            {"locale": self.locale, "country": self.country, "offerId": offer_id, "sandboxId": namespace},
        )

    def get_offers_validation(self, offer_id: str, namespace: str) -> str:
        # 已改用 entitlements API（见 get_purchasable / _verify_owned），此方法仅保留备用
        return self.graphql(
            "getOffersValidation",
            api.HASH_GET_OFFERS_VALIDATION,
            {"offers": [{"offerId": offer_id, "namespace": namespace}]},
            need_session=True,
        )

    def _ensure_token(self, token: dict) -> None:
        self.token = token

    def resolve_offer(self, game: dict) -> list[dict]:
        """把列表元素解析为真实 offer（pageSlug 映射 / 旧 CMS 产品），并经 getCatalogOffer 校验。"""
        page_slug, product_slug = game.get("pageSlug"), game.get("productSlug")
        candidates = [(game["offerId"], game["namespace"])]
        if page_slug and product_slug:
            try:
                m = parser.parse_page_slug_mapping(self.get_mapping_by_page_slug(page_slug))
            except Exception as e:
                self.log(f"{game['title']}：slug 映射查询失败（{e}），退回元素自身 offer")
                m = None
            if m and m[0] == "offer":
                candidates = [(m[2], m[1])]
            elif m and m[0] == "slug":
                try:
                    cms = self.fetch_cms_product(m[1], game.get("offerType") == "BUNDLE")
                    offers = parser.parse_cms_product_offers(cms, game.get("offerType") == "BUNDLE", game["title"])
                    if offers:
                        candidates = [(o["offerId"], o["namespace"]) for o in offers]
                except Exception as e:
                    self.log(f"{game['title']}：CMS 产品查询失败（{e}），退回元素自身 offer")
        validated = []
        for offer_id, ns in candidates:
            try:
                raw = self.get_catalog_offer(offer_id, ns)
            except Exception as e:
                self.log(f"{game['title']}：offer 校验查询失败（{e}）")
                continue
            v = parser.parse_catalog_offer(raw, self.country)
            if v:
                validated.append({**v, "title": game["title"], "endDate": game.get("endDate") or v.get("endDate")})
            else:
                self.log(f"{game['title']}：offer {offer_id[:12]} 未通过免费校验（非免费/过期/地区屏蔽）")
        return validated

    def get_purchasable(self, token: dict, offers: list[dict]) -> tuple[list[dict], list[dict]]:
        """所有权判断：entitlements REST API（Epic 自家服务，Bearer 认证，
        绕开被 Cloudflare 质询的商店 GraphQL 端点）。
        返回 (可领取列表, 已拥有列表)，offer dict 附带 item_ids 供领取后复核。"""
        self.token = token
        try:
            ent_names = api.fetch_entitlement_names(token)
        except Exception as e:
            self.log(f"entitlements 查询失败，无法判断拥有状态：{e}")
            return [], []
        self.log(f"账号 entitlements 共 {len(ent_names)} 条")
        purchasable, owned = [], []
        for o in offers:
            try:
                item_ids = api.fetch_offer_item_ids(token, o["namespace"], o["offerId"])
            except Exception as e:
                self.log(f"{o['title']}：catalog 查询失败（{e}），跳过")
                continue
            o["item_ids"] = item_ids
            if item_ids and set(item_ids) & ent_names:
                owned.append(o)
            else:
                purchasable.append(o)
        return purchasable, owned

    def _verify_owned(self, offer: dict, deadline_s: int = 60) -> bool:
        """领取后复核：entitlements 中出现该 offer 的 item id 即成功"""
        ids = set(offer.get("item_ids") or [])
        if not ids:
            return False
        deadline = time.time() + deadline_s
        while time.time() < deadline:
            time.sleep(5)
            try:
                ent = api.fetch_entitlement_names(self.token)
            except Exception:
                continue
            if ids & ent:
                return True
        return False

    # ---------- 领取 ----------

    def claim_offer(self, offer: dict) -> tuple[bool, str]:
        """加载结账页 → 注入自动点击脚本 → 轮询状态 → API 复核入库。"""
        try:
            self.page.goto(api.checkout_url(offer["namespace"], offer["offerId"]),
                           timeout=60000, wait_until="domcontentloaded")
            self.page.evaluate("window.__efgInit = undefined; void 0;")
            self.page.evaluate(CHECKOUT_JS)

            clicked, detail = False, ""
            deadline = time.time() + 100
            while time.time() < deadline:
                time.sleep(2)
                raw = self.page.evaluate("JSON.stringify(window.__efg || null)")
                if not raw or raw == "null":
                    continue
                st = json.loads(raw)
                detail = st.get("detail") or detail
                if st.get("phase") in ("clicked", "done"):
                    clicked = True
                    break
                if st.get("phase") == "timeout":
                    break
            if not clicked:
                self._screenshot(offer)
                return False, f"自动下单未完成（{detail or '页面未渲染完成'}）"
            self.log(f"{offer['title']}：已点击下单（{detail}），等待入库…")

            if self._verify_owned(offer):
                return True, "ok"
            self._screenshot(offer)
            return False, "已下单但未确认入库，请手动核实"
        except Exception as e:
            self._screenshot(offer)
            return False, f"异常：{e}"

    def _screenshot(self, offer: dict):
        if not self.screenshot_dir:
            return
        try:
            import os

            os.makedirs(self.screenshot_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            safe = "".join(ch for ch in offer["title"] if ch.isalnum())[:30] or "offer"
            self.page.screenshot(path=os.path.join(self.screenshot_dir, f"err-{ts}-{safe}.png"), full_page=True)
        except Exception:
            pass
