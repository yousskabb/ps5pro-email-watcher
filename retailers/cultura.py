"""Cultura — Console Sony PlayStation 5 Pro 2Tb (sku 12685100).

Fetch
-----
Behind Cloudflare (``cf-ray: ...-CDG``) but passing plain stdlib urllib today,
mais UNIQUEMENT depuis une IP residentielle : depuis les IP Azure de
GitHub Actions, urllib recoit un HTTP 403 (verifie en prod le 2026-09-13).
On passe donc par ``fetch_impersonate`` (curl_cffi firefox133). Search paths are brittle (``/search?q=`` 404s,
``/catalogsearch/result/?q=`` 403-redirects), so the product URL is pinned.

Detection (verified live 2026-09-13)
------------------------------------
Two independent server-rendered payloads carry the truth:

1. ``<div id="new-react-product-details" data-graphql-response="...">`` — HTML
   entity encoded JSON. ``stock_item_extra.front_availability`` is the
   authoritative online flag ("unavailable" today).
2. The TagCommander ``tc_vars.list_products`` blob — ``product_stock_site``
   ("INDISPONIBLE EN LIGNE"), ``product_stock_shop`` ("unavailable"),
   ``product_seller`` ("Cultura") and ``product_unitprice_ati`` ("899.99").

Two documented traps, both confirmed against the live page:

* ``stock_item_extra.offer[]`` lists entries reading
  ``{"front_availability": "available", "seller_code": "CBC", "qty": 10000.0}``
  for codes CBC/CCT/CHB/CLE/CTR. These are store/warehouse channel codes with a
  sentinel quantity, NOT purchasable online offers — they say "available" while
  the product is out of stock online. They are deliberately ignored.
* ``product_seller`` reads "Cultura" even on the target page where the only
  live offers are marketplace ones, so it is necessary but not sufficient.
  ``product_offer_mkp`` is what actually distinguishes them — verified live:

      PS5 Pro 2Tb (target)  "marketplace"            -> no first-party offer
      Astro Bot (in stock)  "cultura et marketplace" -> Cultura sells it too

  so the first-party gate requires "cultura" to appear in ``product_offer_mkp``.
* ``product_stock_shop`` is *physical store* stock and reads "unavailable" in
  BOTH states under an anonymous session (no store selected). It says nothing
  about online availability and is therefore informational only — treating it
  as an out-of-stock signal wrongly pins in-stock products to out_of_stock.

The page's Product JSON-LD block exists but is invalid JSON (raw newlines inside
the description string), so ``iter_jsonld`` / ``jsonld_product_offer`` skip it
and return None. It is re-read leniently here as corroboration only.

Waiting room
------------
Cultura runs a Queue-it style waiting room for hot PS5 SKUs. ``looks_blocked``
already matches "File d'attente" / "Queue-it" / "waiting room" and would report
that as *blocked*, but on Cultura the queue appearing IS the drop signal — so
the queue is checked FIRST and returns ``STATUS_QUEUED``. The check additionally
requires the product payload to be absent, so that an incidental mention of the
phrase inside a normal page cannot flip us to queued.
"""
from __future__ import annotations

import html as _html
import json
import re
from typing import Optional

from .base import (
    DEFAULT_MAX_PRICE,
    STATUS_IN,
    STATUS_OUT,
    STATUS_QUEUED,
    STATUS_UNKNOWN,
    Result,
    Retailer,
    availability_to_status,
    fetch_impersonate,
    looks_blocked,
    parse_price,
    price_ok,
)

URL = "https://www.cultura.com/p-console-sony-playstation-5-pro-2tb-12685100.html"
SKU = "12685100"
MAX_PRICE = DEFAULT_MAX_PRICE

FIRST_PARTY_SELLERS = {"cultura"}

_QUEUE_COPY = re.compile(
    r"file d[e'’ ]attente|salle d[e'’ ]attente|waiting\s*room|queue-it|queue-it\.net"
    r"|votre tour|merci de patienter",
    re.I,
)

_UNAVAILABLE_SITE = re.compile(r"indisponible", re.I)


def _json_at(text: str, idx: int):
    """Decode the JSON value starting at `idx` (balanced, quote-aware)."""
    try:
        value, _ = json.JSONDecoder().raw_decode(text, idx)
        return value
    except Exception:                                # noqa: BLE001
        return None


def _graphql_payload(html: str) -> Optional[dict]:
    """The entity-encoded product blob on #new-react-product-details."""
    m = re.search(
        r'id="new-react-product-details"[^>]*\sdata-graphql-response="([^"]*)"', html)
    if not m:
        return None
    try:
        data = json.loads(_html.unescape(m.group(1)))
    except Exception:                                # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _tc_product(html: str) -> Optional[dict]:
    """The tc_vars.list_products entry for our SKU."""
    for m in re.finditer(r'"list_products"\s*:\s*\[', html):
        arr = _json_at(html, html.index("[", m.start()))
        if not isinstance(arr, list):
            continue
        for item in arr:
            if isinstance(item, dict) and str(item.get("product_id", "")) == SKU:
                return item
    return None


def _jsonld_availability(html: str) -> str:
    """Cultura's Product JSON-LD is malformed; re-read it leniently."""
    for m in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S | re.I
    ):
        block = m.group(1).strip()
        if '"Product"' not in block:
            continue
        try:
            data = json.loads(block, strict=False)   # tolerate raw control chars
        except Exception:                            # noqa: BLE001
            hit = re.search(r'"availability"\s*:\s*"([^"]+)"', block)
            return availability_to_status(hit.group(1)) if hit else STATUS_UNKNOWN
        offers = data.get("offers") if isinstance(data, dict) else None
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if isinstance(offers, dict):
            return availability_to_status(offers.get("availability", ""))
    return STATUS_UNKNOWN


def parse(html: str) -> Result:
    payload = _graphql_payload(html)
    tc = _tc_product(html)

    # ---- queue FIRST: looks_blocked() would call this "blocked", but on
    # Cultura a waiting room on this SKU is the drop starting.
    if payload is None and tc is None and _QUEUE_COPY.search(html or ""):
        return Result(STATUS_QUEUED, "file d'attente Cultura — drop en cours")

    blocked = looks_blocked(html)
    if blocked:
        return Result(STATUS_UNKNOWN, blocked)

    if payload is None and tc is None:
        return Result(STATUS_UNKNOWN, "données produit %s absentes (page modifiée ?)" % SKU)

    if payload is not None and str(payload.get("sku", "")) not in ("", SKU):
        return Result(STATUS_UNKNOWN, "sku inattendu (%s)" % payload.get("sku"))

    tc = tc or {}
    seller = (tc.get("product_seller") or "").strip()
    price = parse_price(tc.get("product_unitprice_ati"))
    mkp = (tc.get("product_offer_mkp") or "").strip()

    stock_site = (tc.get("product_stock_site") or "").strip()
    stock_shop = (tc.get("product_stock_shop") or "").strip().lower()

    front = None
    if payload is not None:
        extra = payload.get("stock_item_extra")
        if isinstance(extra, dict):
            front = (extra.get("front_availability") or "").strip().lower()

    jsonld_status = _jsonld_availability(html)

    # ---- first-party gate: seller name AND an actual Cultura-sold offer.
    if seller and seller.lower() not in FIRST_PARTY_SELLERS:
        return Result(STATUS_OUT, "vendeur tiers (%s) — pas Cultura" % seller,
                      price=price, seller=seller)
    # NB: stock_shop (magasin physique) is deliberately excluded — it reads
    # "unavailable" in both states for an anonymous session.
    signals_out = (
        front == "unavailable"
        or (stock_site and _UNAVAILABLE_SITE.search(stock_site))
        or jsonld_status == STATUS_OUT
    )
    signals_in = (
        front == "available"
        and jsonld_status == STATUS_IN
        and not (stock_site and _UNAVAILABLE_SITE.search(stock_site))
    )

    if signals_in and not signals_out:
        # A "marketplace"-only offer mix means the stock belongs to a reseller.
        if mkp and "cultura" not in mkp.lower():
            return Result(STATUS_OUT, "stock marketplace uniquement — pas Cultura",
                          price=price, seller=seller or None)
        if not seller:
            return Result(STATUS_UNKNOWN, "vendeur illisible malgré stock annoncé",
                          price=price, seller=None)
        if not price_ok(price, MAX_PRICE):
            return Result(STATUS_OUT, "prix anormal %.2f€ — ignoré" % price,
                          price=price, seller=seller)
        return Result(STATUS_IN, "dispo chez Cultura%s" % (" à %.2f€" % price if price else ""),
                      price=price, seller=seller or "Cultura")

    if signals_out:
        detail = stock_site or front or stock_shop or "indisponible"
        return Result(STATUS_OUT, "indisponible chez Cultura (%s)" % detail.lower(),
                      price=price, seller=seller or None)

    return Result(STATUS_UNKNOWN,
                  "signaux illisibles (front=%s, site=%s, json-ld=%s, mkp=%s)"
                  % (front or "?", stock_site or "?", jsonld_status, mkp or "?"),
                  price=price, seller=seller or None)


RETAILERS: list[Retailer] = [
    Retailer(
        key="cultura",
        name="Cultura",
        url=URL,
        parse=parse,
        fetch=fetch_impersonate,
        max_price=MAX_PRICE,
        note="Cloudflare : curl_cffi firefox133 obligatoire depuis un runner "
             "(403 en clair sur IP Azure) ; waiting room Queue-it => queued (= le drop).",
    ),
]


if __name__ == "__main__":
    from .base import fetch_plain

    for r in RETAILERS:
        try:
            page = fetch_plain(r)
        except Exception as exc:                     # noqa: BLE001
            print(r.key, (STATUS_UNKNOWN, "fetch: %s" % exc))
            continue
        res = parse(page)
        print("%s -> (%s, %r) price=%s seller=%s [%d octets]"
              % (r.key, res.status, res.note, res.price, res.seller, len(page)))
