"""Auchan — SONY Console PlayStation 5 Pro (pr-C1824137).

Fetch
-----
No anti-bot at all (``server: bobo,local``). ``fetch_plain``.

Why this adapter exists
-----------------------
Auchan is the clearest illustration of why the seller gate is mandatory.
Captured live 2026-09-13, the PS5 Pro buybox is a scalper:

    <span class="offer-selector__marketplace-label">Vendu par</span>
    <a href="/2kings/vm-3732">2KINGS</a>
    <div class="bolder red-txt">2 215,40€</div>
    <meta itemprop="price" content="2215.4">
    <meta itemprop="availability" content="https://schema.org/InStock">

``availability = InStock`` is TRUE right now at 2 215 EUR from a marketplace
seller. This adapter must — and does — return out_of_stock for that page.

Detection (verified live 2026-09-13)
------------------------------------
There is no JSON-LD on Auchan at all; only microdata. The buybox is the region
from the first ``offer-selector`` up to the first ``schema.org/Product``
itemscope (which is where the related-products carousel starts).

The seller is read from the first "Vendu par" construct in that region, which
has exactly two shapes:

  first-party   <div class="offer-selector__seller"><span ...>Vendu par
                <span class="bolder">Auchan</span></span></div>
  marketplace   <div class="offer-selector__seller-container"><span ...>Vendu par
                </span><a href="/2kings/vm-3732">2KINGS</a>

The ``/<slug>/vm-<id>`` vendor-page href is the structural marketplace tell.
NOTE: testing for the string ``offer-selector__marketplace-label`` alone is a
false positive — that span is used in BOTH shapes.

Two further drifts from the original research, both confirmed:

* ``data-seller-type`` is NOT a first-party discriminator. The 2KINGS offer
  carries ``data-seller-type="ONLINE"`` too; the attribute distinguishes
  shipped (ONLINE) from in-store (STORE), not who sells.
* ``itemprop="availability"`` is decorative — every offer on every page tested
  reads InStock, including ones with ``data-stock="0"``. The real quantity
  signal is the buybox ``quantity-selector``'s ``data-stock`` together with
  ``data-offer-type`` (DEFAULT = a real purchasable offer, VIRTUAL = variant
  placeholder with no stock).

Verified matrix:

  ref        product                        buybox seller  type     stock  price
  C1824137   PS5 Pro (target)               2KINGS (mkp)   DEFAULT  3      2215.40
  C1892367   DualSense Icon Blue            Auchan         DEFAULT  91     84.99
  C1862303   DualSense Techno Red           Auchan         DEFAULT  13     84.99
  C1852949   DualSense blanche              Auchan         VIRTUAL  0      74.99
  C1667635   DualSense Edge                 ASD (mkp)      DEFAULT  5      246.70
"""
from __future__ import annotations

import re
from typing import Optional

from .base import (
    DEFAULT_MAX_PRICE,
    STATUS_IN,
    STATUS_OUT,
    STATUS_UNKNOWN,
    Result,
    Retailer,
    availability_to_status,
    looks_blocked,
    parse_price,
    price_ok,
)

URL = "https://www.auchan.fr/sony-console-playstation-5-pro/pr-C1824137"
REF = "C1824137"
MAX_PRICE = DEFAULT_MAX_PRICE

FIRST_PARTY_SELLERS = {"auchan"}

_ATTR_RE = re.compile(r'([a-zA-Z0-9_:-]+)\s*=\s*"([^"]*)"')
_CAROUSEL_START = re.compile(r'itemtype="http://schema\.org/Product"')
# marketplace vendor page: /<slug>/vm-<id>
_VENDOR_LINK = re.compile(r'href="/[^"]*/vm-\d+"[^>]*>\s*([^<]+?)\s*</a>')
_FIRST_PARTY_SPAN = re.compile(r'Vendu par\s*<span class="bolder">\s*([^<]+?)\s*</span>')


def _buybox(html: str) -> Optional[str]:
    """The main offer region: first offer-selector .. start of the carousel."""
    start = html.find("offer-selector")
    if start < 0:
        return None
    tail = html[start:]
    m = _CAROUSEL_START.search(tail)
    end = m.start() if m else min(len(tail), 40000)
    return tail[:end]


def _tag_around(html: str, pos: int) -> Optional[str]:
    """Full text of the tag containing offset `pos` (quote-aware forward scan)."""
    begin = html.rfind("<", 0, pos)
    if begin < 0:
        return None
    i, n, quote = begin, len(html), None
    while i < n:
        ch = html[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == ">":
            return html[begin : i + 1]
        i += 1
    return None


def _buybox_seller(region: str) -> tuple[Optional[str], bool]:
    """(seller name, is_marketplace) from the first 'Vendu par' in the buybox."""
    at = region.find("Vendu par")
    if at < 0:
        return None, False
    window = region[at : at + 900]
    vendor = _VENDOR_LINK.search(window)
    if vendor:
        return vendor.group(1).strip(), True
    own = _FIRST_PARTY_SPAN.search(window)
    if own:
        return own.group(1).strip(), False
    return None, False


def _buybox_quantity(region: str) -> tuple[Optional[int], Optional[str]]:
    """(stock, offer_type) from the buybox quantity-selector."""
    for m in re.finditer(r'class="quantity-selector[^"]*"', region):
        tag = _tag_around(region, m.start())
        if not tag:
            continue
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        raw = attrs.get("data-stock")
        if raw is None:
            continue
        try:
            stock = int(raw)
        except ValueError:
            continue
        return stock, (attrs.get("data-offer-type") or "").strip().upper() or None
    return None, None


def parse(html: str) -> Result:
    blocked = looks_blocked(html)
    if blocked:
        return Result(STATUS_UNKNOWN, blocked)

    region = _buybox(html)
    if not region:
        return Result(STATUS_UNKNOWN, "buybox introuvable (page modifiée ?)")

    seller, is_mkp = _buybox_seller(region)
    price_m = re.search(r'<meta itemprop="price" content="([^"]*)"', region)
    price = parse_price(price_m.group(1)) if price_m else None
    avail_m = re.search(r'<meta itemprop="availability" content="([^"]*)"', region)
    micro_status = availability_to_status(avail_m.group(1)) if avail_m else STATUS_UNKNOWN
    stock, offer_type = _buybox_quantity(region)

    if seller is None:
        return Result(STATUS_UNKNOWN, "vendeur illisible dans la buybox",
                      price=price, seller=None)

    # ---- seller gate first. This is the branch the 2KINGS page takes.
    if is_mkp or seller.lower() not in FIRST_PARTY_SELLERS:
        return Result(STATUS_OUT, "vendeur tiers (%s) — pas Auchan" % seller,
                      price=price, seller=seller)

    if stock is None or micro_status == STATUS_UNKNOWN:
        return Result(STATUS_UNKNOWN,
                      "stock illisible (stock=%s, dispo=%s)" % (stock, micro_status),
                      price=price, seller=seller)

    # itemprop availability is decorative on Auchan: quantity is the real gate.
    if micro_status == STATUS_IN and stock > 0 and offer_type == "DEFAULT":
        if not price_ok(price, MAX_PRICE):
            return Result(STATUS_OUT, "prix anormal %.2f€ — ignoré" % price,
                          price=price, seller=seller)
        return Result(STATUS_IN,
                      "dispo chez Auchan%s (stock %d)"
                      % (" à %.2f€" % price if price else "", stock),
                      price=price, seller=seller)

    return Result(STATUS_OUT, "indisponible chez Auchan (stock %s)" % (stock,),
                  price=price, seller=seller)


RETAILERS: list[Retailer] = [
    Retailer(
        key="auchan",
        name="Auchan",
        url=URL,
        parse=parse,
        max_price=MAX_PRICE,
        note="Aucun anti-bot ; buybox souvent tenue par un revendeur marketplace.",
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
