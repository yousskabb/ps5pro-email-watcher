"""PlayStation Direct FR — JSON commerce API, not the HTML page.

Why the API and not the product page
------------------------------------
The historic detector scraped ``direct.playstation.com`` and keyed on an
``add-to-cart`` button losing its ``hide`` class. That page is ~30 kB of
Akamai-fronted markup that sets ``_abck`` cookies and whose button markup is a
build artifact. The storefront's own SAP Commerce backend answers the same
question over a public, unauthenticated JSON endpoint on a DIFFERENT host
(``api.direct.playstation.com``) which serves no Akamai challenge:

    GET /commercewebservices/ps-direct-fr/users/anonymous/products/productList
        ?fields=BASIC&lang=fr_FR&productCodes=1000050720-FR

Verified live 2026-09-13 (HTTP 200, 7 181 octets)::

    {"products":[{"code":"1000050720-FR","name":"Console PlayStation®5 Pro - 2 To",
      "purchasable":true,
      "stock":{"stockLevelStatus":"outOfStock","isProductLowStock":false},
      "price":{"currencyIso":"EUR","value":899.99},
      "maxOrderQuantity":0,"validProductCode":true}]}

Signal
------
Primary  : ``stock.stockLevelStatus``  outOfStock -> inStock / lowStock
Secondary: ``maxOrderQuantity``        0 -> >= 1

``purchasable`` is decorative — it is ``true`` right now while the console is
sold out — so it is deliberately ignored.

Failure handling: the body is JSON, so a challenge/interstitial page cannot be
decoded and is reported as ``unknown``. ``looks_blocked`` is therefore called
with a small ``min_size`` (its 20 000 default is an HTML heuristic and would
flag every healthy response here).
"""
from __future__ import annotations

import json
from typing import Optional

try:                                    # package import (check.py)
    from .base import (
        DEFAULT_MAX_PRICE, STATUS_IN, STATUS_OUT, STATUS_UNKNOWN,
        Result, Retailer, fetch_plain, looks_blocked, parse_price, price_ok,
    )
except ImportError:                     # direct `python3 retailers/psdirect.py`
    from base import (                  # type: ignore[no-redef]
        DEFAULT_MAX_PRICE, STATUS_IN, STATUS_OUT, STATUS_UNKNOWN,
        Result, Retailer, fetch_plain, looks_blocked, parse_price, price_ok,
    )

PRODUCT_CODE = "1000050720-FR"

HUMAN_URL = ("https://direct.playstation.com/fr-fr/buy-consoles/"
             "playstation5-pro-console-2-tb")

API_URL = (
    "https://api.direct.playstation.com/commercewebservices/ps-direct-fr/"
    "users/anonymous/products/productList"
    "?fields=BASIC&lang=fr_FR&productCodes=%s" % PRODUCT_CODE
)

# stock.stockLevelStatus -> our vocabulary. Anything unlisted is `unknown`:
# a new enum value must wake the canary, never be read as "pas de stock".
_STOCK_LEVEL = {
    "instock": STATUS_IN,
    "lowstock": STATUS_IN,
    "outofstock": STATUS_OUT,
}

# The JSON body is ~7 kB; a blocked/interstitial answer would be far smaller
# and would not decode as JSON anyway.
_MIN_SIZE = 200


def parse(body: str) -> Result:
    """Parse the productList JSON. Never returns out_of_stock on a failure."""
    blocked = looks_blocked(body, min_size=_MIN_SIZE)
    if blocked:
        return Result(STATUS_UNKNOWN, "API PS Direct : %s" % blocked)

    try:
        data = json.loads(body)
    except Exception:                    # noqa: BLE001 — challenge page, HTML, truncation
        return Result(STATUS_UNKNOWN, "réponse API non-JSON (challenge ?)")

    if not isinstance(data, dict):
        return Result(STATUS_UNKNOWN, "JSON API inattendu (pas un objet)")

    products = data.get("products")
    if not isinstance(products, list) or not products:
        return Result(STATUS_UNKNOWN,
                      "aucun produit renvoyé pour %s (code changé ?)" % PRODUCT_CODE)

    product = next(
        (p for p in products
         if isinstance(p, dict) and p.get("code") == PRODUCT_CODE),
        None,
    )
    if product is None:
        product = products[0] if isinstance(products[0], dict) else None
    if product is None:
        return Result(STATUS_UNKNOWN, "entrée produit illisible dans l'API")

    if product.get("validProductCode") is False:
        return Result(STATUS_UNKNOWN,
                      "validProductCode=false pour %s (code changé ?)" % PRODUCT_CODE)

    price: Optional[float] = None
    price_obj = product.get("price")
    if isinstance(price_obj, dict):
        price = parse_price(price_obj.get("value"))

    stock = product.get("stock")
    raw_level = stock.get("stockLevelStatus") if isinstance(stock, dict) else None
    if not isinstance(raw_level, str) or not raw_level:
        return Result(STATUS_UNKNOWN, "stockLevelStatus absent (schéma API changé ?)",
                      price=price, seller="PlayStation Direct")

    status = _STOCK_LEVEL.get(raw_level.strip().lower(), STATUS_UNKNOWN)
    if status == STATUS_UNKNOWN:
        return Result(STATUS_UNKNOWN,
                      "stockLevelStatus=%s inconnu" % raw_level,
                      price=price, seller="PlayStation Direct")

    max_qty = product.get("maxOrderQuantity")
    max_qty = max_qty if isinstance(max_qty, int) else None

    # Corroboration between the two fields.
    if status == STATUS_OUT and max_qty is not None and max_qty >= 1:
        # Contradiction: "sold out" but orderable. Could be the very first
        # second of a drop — surface it, never swallow it as out_of_stock.
        return Result(STATUS_UNKNOWN,
                      "signaux contradictoires : stockLevelStatus=%s mais "
                      "maxOrderQuantity=%d" % (raw_level, max_qty),
                      price=price, seller="PlayStation Direct")

    caveat = ""
    if status == STATUS_IN and max_qty == 0:
        # Positive primary signal wins: missing a real drop is worse than one
        # noisy alert. The disagreement is spelled out in the alert body.
        caveat = " (mais maxOrderQuantity=0 — à vérifier)"

    price_txt = "%.2f EUR" % price if price is not None else "prix inconnu"

    if status == STATUS_IN and not price_ok(price, DEFAULT_MAX_PRICE):
        return Result(STATUS_OUT,
                      "stockLevelStatus=%s mais prix aberrant %s — ignoré"
                      % (raw_level, price_txt),
                      price=price, seller="PlayStation Direct")

    qty_txt = "" if max_qty is None else ", maxOrderQuantity=%d" % max_qty
    return Result(
        status,
        "API stockLevelStatus=%s%s, %s%s" % (raw_level, qty_txt, price_txt, caveat),
        price=price,
        seller="PlayStation Direct",
    )


RETAILERS: list[Retailer] = [
    Retailer(
        key="psdirect",
        name="PlayStation Direct FR",
        url=HUMAN_URL,
        fetch_url=API_URL,
        fetch=fetch_plain,
        parse=parse,
        note="API JSON publique (api.direct.playstation.com) — pas de mur Akamai",
    ),
]


if __name__ == "__main__":
    for r in RETAILERS:
        try:
            res = parse(fetch_plain(r))
        except Exception as exc:                     # noqa: BLE001
            res = Result(STATUS_UNKNOWN, "fetch KO: %s" % exc)
        print("%-22s %-12s %s | prix=%s vendeur=%s"
              % (r.key, res.status, res.note, res.price, res.seller))
