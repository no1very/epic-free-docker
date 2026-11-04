"""解析逻辑自测（无依赖，python test_parser.py 直接运行）。"""

import json
import sys
from datetime import datetime, timezone

from epic import parser

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)


class DateTimeMonkey:
    """把 parser._now 固定为 NOW 以便测试"""

    def __enter__(self):
        self._orig = parser._now
        parser._now = lambda: NOW
        return self

    def __exit__(self, *a):
        parser._now = self._orig


FIXTURE_PROMOTIONS = {
    "data": {
        "Catalog": {
            "searchStore": {
                "elements": [
                    {
                        "title": "Test Game",
                        "id": "abc123",
                        "namespace": "ns1",
                        "offerType": "OTHER",
                        "productSlug": "test-game",
                        "urlSlug": "test-game-home",
                        "catalogNs": {"mappings": [{"pageType": "productHome", "pageSlug": "test-game"}]},
                        "promotions": {
                            "promotionalOffers": [
                                {
                                    "promotionalOffers": [
                                        {
                                            "startDate": "2026-11-01T16:00:00.000Z",
                                            "endDate": "2026-11-08T16:00:00.000Z",
                                            "discountSetting": {"discountType": "PERCENTAGE", "discountPercentage": 0},
                                        }
                                    ]
                                }
                            ],
                            "upcomingPromotionalOffers": [],
                        },
                    },
                    {
                        "title": "Paid Game",
                        "id": "xyz",
                        "namespace": "ns2",
                        "promotions": {
                            "promotionalOffers": [
                                {
                                    "promotionalOffers": [
                                        {
                                            "startDate": "2026-11-01T16:00:00.000Z",
                                            "endDate": "2026-11-08T16:00:00.000Z",
                                            "discountSetting": {"discountType": "PERCENTAGE", "discountPercentage": 50},
                                        }
                                    ]
                                }
                            ]
                        },
                    },
                    {
                        "title": "Expired Free",
                        "id": "old",
                        "namespace": "ns3",
                        "promotions": {
                            "promotionalOffers": [
                                {
                                    "promotionalOffers": [
                                        {
                                            "startDate": "2026-10-01T16:00:00.000Z",
                                            "endDate": "2026-10-08T16:00:00.000Z",
                                            "discountSetting": {"discountType": "PERCENTAGE", "discountPercentage": 0},
                                        }
                                    ]
                                }
                            ]
                        },
                    },
                ]
            }
        }
    }
}


def test_weekly():
    with DateTimeMonkey():
        games = parser.parse_weekly_free_games(json.dumps(FIXTURE_PROMOTIONS))
        assert len(games) == 1, games
        g = games[0]
        assert g["title"] == "Test Game" and g["offerId"] == "abc123" and g["namespace"] == "ns1"
        assert g["pageSlug"] == "test-game"
        assert g["endDate"] == "2026-11-08T16:00:00.000Z"
        assert parser.parse_weekly_free_games(json.dumps({"data": {"Catalog": {"searchStore": {"elements": []}}}})) == []
        assert parser.parse_weekly_free_games("{}") == []
    print("test_weekly OK")


def test_validation():
    can_buy = json.dumps(
        {
            "data": {
                "Entitlements": {
                    "cartOffersValidation": {
                        "missingPrerequisites": [],
                        "conflictingOffers": [],
                        "fullyOwnedOffers": [],
                        "unablePartiallyUpgradeOffers": [],
                    }
                }
            }
        }
    )
    assert parser.parse_can_purchase(can_buy) is True
    assert parser.parse_fully_owned(can_buy) is False

    owned = json.dumps(
        {
            "data": {
                "Entitlements": {
                    "cartOffersValidation": {
                        "missingPrerequisites": [],
                        "conflictingOffers": [],
                        "fullyOwnedOffers": [{"offerId": "abc123", "namespace": "ns1"}],
                        "unablePartiallyUpgradeOffers": [],
                    }
                }
            }
        }
    )
    assert parser.parse_can_purchase(owned) is False
    assert parser.parse_fully_owned(owned) is True

    assert parser.parse_can_purchase(json.dumps({"errors": [{"message": "x"}], "data": None})) is False
    print("test_validation OK")


def test_catalog_offer():
    with DateTimeMonkey():
        ok = json.dumps(
            {
                "data": {
                    "Catalog": {
                        "catalogOffer": {
                            "title": "Test Game",
                            "id": "abc123",
                            "namespace": "ns1",
                            "productSlug": "test-game",
                            "price": {"totalPrice": {"discountPrice": 0}},
                            "expiryDate": "2026-11-08T16:00:00.000Z",
                        }
                    }
                }
            }
        )
        offer = parser.parse_catalog_offer(ok, "US")
        assert offer and offer["offerId"] == "abc123"

        paid = json.loads(ok)
        paid["data"]["Catalog"]["catalogOffer"]["price"]["totalPrice"]["discountPrice"] = 2999
        assert parser.parse_catalog_offer(json.dumps(paid), "US") is None

        blocked = json.loads(ok)
        blocked["data"]["Catalog"]["catalogOffer"]["countriesBlacklist"] = ["CN"]
        assert parser.parse_catalog_offer(json.dumps(blocked), "CN") is None
        assert parser.parse_catalog_offer(json.dumps(blocked), "US") is not None
    print("test_catalog_offer OK")


def test_mapping_and_cms():
    offer_map = json.dumps(
        {
            "data": {
                "StorePageMapping": {
                    "mapping": {"pageSlug": "bloons-td-6", "mappings": {"offer": {"id": "offer1", "namespace": "nsX"}}}
                }
            }
        }
    )
    assert parser.parse_page_slug_mapping(offer_map) == ("offer", "nsX", "offer1")

    slug_map = json.dumps({"data": {"StorePageMapping": {"mapping": {"pageSlug": "old-slug", "mappings": {}}}}})
    assert parser.parse_page_slug_mapping(slug_map) == ("slug", "old-slug")
    assert parser.parse_page_slug_mapping(json.dumps({"data": {}})) is None

    product = json.dumps(
        {
            "pages": [
                {"offer": {"id": "o1", "namespace": "n1", "hasOffer": True}},
                {"offer": {"id": "o2", "namespace": "n2", "hasOffer": False}},
            ]
        }
    )
    offers = parser.parse_cms_product_offers(product, is_bundle=False, title="Game")
    assert len(offers) == 1 and offers[0]["offerId"] == "o1"

    bundle = json.dumps({"offer": {"id": "b1", "namespace": "nb", "hasOffer": True}})
    offers = parser.parse_cms_product_offers(bundle, is_bundle=True, title="Bundle")
    assert len(offers) == 1 and offers[0]["offerId"] == "b1"
    print("test_mapping_and_cms OK")


def test_iso():
    assert parser.parse_iso("2026-11-04T14:00:00.000Z") is not None
    assert parser.parse_iso("bad") is None
    print("test_iso OK")


if __name__ == "__main__":
    test_weekly()
    test_validation()
    test_catalog_offer()
    test_mapping_and_cms()
    test_iso()
    print("\n全部自测通过 ✓")
    sys.exit(0)
