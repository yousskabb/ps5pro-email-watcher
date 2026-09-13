"""Boulanger — Console SONY PS5 Pro 2Tb (ref 1230556).

Fetch
-----
Behind Akamai Bot Manager (``_abck``, ``bm_mi``, ``bm_s``, ``AKA_A2``) but
currently serving full product HTML to plain stdlib urllib, including from a
datacenter IP. ``fetch_plain`` is enough; no impersonation needed.

Detection (verified live 2026-09-13 against four real pages)
-----------------------------------------------------------
Boulanger renders an analytics attribute bundle on the main product's primary
call-to-action button. Which button carries it depends on the stock state:

  * out of stock -> the "créer une alerte" button (``js_create_alert_product_button``)
  * in stock     -> the main "ajouter au panier" button

Either way the bundle is keyed by ``data-analytics_product_sap`` = the 18-digit
zero-padded SKU, so we locate it by SKU rather than by button role and read the
same attributes in both states. Verified matrix:

  ref      page                      json-ld      seller      avail  mkp    grade
  1230556  PS5 Pro 2Tb (target)      OutOfStock   boulanger   false  -      neuf
  1228868  PS5 Slim Digital          InStock      boulanger   true   false  neuf
  1231767  DualSense blanche V3      InStock      boulanger   true   false  neuf
  1199956  DualSense (autre)         InStock      micromania  true   true   neuf

The last row is the whole point of the seller gate: a genuinely in-stock page
whose buybox belongs to a marketplace seller. It must resolve to out_of_stock.

Waiting room
------------
Boulanger has an Akamai virtual waiting room armed. NOTE: the cookie name
``akavpau_vpwaitingroom`` appears on a perfectly normal product page — it is
listed in the didomi consent config's ``protectedCookies`` array. Keying the
queue check on that string alone would pin this adapter to ``queued`` forever.
So the queue is detected on waiting-room *copy* AND the absence of the product
analytics bundle, i.e. a real interstitial that replaced the page.
"""
from __future__ import annotations

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
    jsonld_product_offer,
    looks_blocked,
    parse_price,
    price_ok,
)

URL = "https://www.boulanger.com/ref/1230556"
SKU = "1230556"
SAP = SKU.zfill(18)                      # 000000000001230556
MAX_PRICE = DEFAULT_MAX_PRICE

FIRST_PARTY_SELLERS = {"boulanger"}
NEW_CONDITIONS = {"neuf"}

# Real Akamai/queue interstitial copy. Deliberately does NOT include the
# akavpau_vpwaitingroom cookie name, which is present on normal pages.
_QUEUE_COPY = re.compile(
    r"file d[e'’ ]attente|salle d[e'’ ]attente|waiting\s*room|queue-it"
    r"|votre tour|merci de patienter|vous êtes en file",
    re.I,
)

_ATTR_RE = re.compile(r'([a-zA-Z0-9_:-]+)\s*=\s*"([^"]*)"')


def _tag_around(html: str, pos: int) -> Optional[str]:
    """Return the full text of the HTML tag containing offset `pos`.

    Quote-aware forward scan: Boulanger ships attribute values that contain
    '>' (e.g. data-breadcrumb="Console - Gaming>Console de jeux"), so a naive
    ``html.find('>', pos)`` can truncate the tag mid-way.
    """
    start = html.rfind("<", 0, pos)
    if start < 0:
        return None
    i, n, quote = start, len(html), None
    while i < n:
        ch = html[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == ">":
            return html[start : i + 1]
        i += 1
    return None


def _main_product_attrs(html: str) -> Optional[dict]:
    """Analytics attribute bundle for SKU 1230556, whichever button carries it."""
    for m in re.finditer(r'data-analytics_product_sap="%s"' % re.escape(SAP), html):
        tag = _tag_around(html, m.start())
        if not tag:
            continue
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        # Only the real bundle carries the seller key; skip incidental tags.
        if "data-analytics_product_seller" in attrs:
            return attrs
    return None


def parse(html: str) -> Result:
    attrs = _main_product_attrs(html)

    # --- waiting room before any block verdict: on a drop the queue IS signal.
    if attrs is None and _QUEUE_COPY.search(html or ""):
        return Result(STATUS_QUEUED, "file d'attente Boulanger — drop en cours ?")

    blocked = looks_blocked(html)
    if blocked:
        return Result(STATUS_UNKNOWN, blocked)

    if attrs is None:
        return Result(STATUS_UNKNOWN, "bloc produit %s introuvable (page modifiée ?)" % SKU)

    seller = (attrs.get("data-analytics_product_seller") or "").strip()
    grade = (attrs.get("data-analytics_product_grade")
             or attrs.get("data-analytics_product_condition") or "").strip().lower()
    avail_attr = (attrs.get("data-analytics_product_availability") or "").strip().lower()
    ptype = (attrs.get("data-analytics_product_type") or "").strip().lower()
    is_mkp = (attrs.get("data-is-marketplace") or "").strip().lower()
    is_recond = (attrs.get("data-is-reconditioned") or "").strip().lower()

    price = parse_price(attrs.get("data-analytics_product_unitprice_ati"))

    offer = jsonld_product_offer(html) or {}
    if price is None:
        price = parse_price(offer.get("price"))
    jsonld_status = availability_to_status(offer.get("availability", ""))

    # ---- seller gate first: "in stock" means Boulanger itself is selling it.
    if seller.lower() not in FIRST_PARTY_SELLERS or is_mkp == "true" or ptype == "marketplace":
        return Result(STATUS_OUT, "vendeur tiers (%s) — pas Boulanger" % (seller or "?"),
                      price=price, seller=seller or None)

    if grade not in NEW_CONDITIONS or is_recond == "true":
        return Result(STATUS_OUT, "état « %s » — pas du neuf" % (grade or "?"),
                      price=price, seller=seller)

    # ---- stock: analytics flag and JSON-LD must agree, else we do not guess.
    attr_in = avail_attr == "true"
    attr_out = avail_attr == "false"

    if attr_in and jsonld_status == STATUS_IN:
        if not price_ok(price, MAX_PRICE):
            return Result(STATUS_OUT, "prix anormal %.2f€ — ignoré" % price,
                          price=price, seller=seller)
        return Result(STATUS_IN, "dispo chez Boulanger%s" % (" à %.2f€" % price if price else ""),
                      price=price, seller=seller)

    if attr_out or jsonld_status == STATUS_OUT:
        return Result(STATUS_OUT, "indisponible chez Boulanger", price=price, seller=seller)

    return Result(STATUS_UNKNOWN,
                  "signaux contradictoires (attr=%s, json-ld=%s)" % (avail_attr or "?", jsonld_status),
                  price=price, seller=seller)


RETAILERS: list[Retailer] = [
    Retailer(
        key="boulanger",
        name="Boulanger",
        url=URL,
        parse=parse,
        max_price=MAX_PRICE,
        note="Akamai Bot Manager + waiting room armé ; fetch_plain suffit pour l'instant.",
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
