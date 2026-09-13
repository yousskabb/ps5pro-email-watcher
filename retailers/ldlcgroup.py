"""Groupe LDLC — LDLC.com, Materiel.net, Rue du Commerce.

The three sites are one company on one platform: same SKU
(``AR202410030051`` / LDLC ref ``PB00649574``), same JSON-LD template, same
``data-stock-web`` attribute, and no anti-bot layer at all (plain urllib gets
HTTP 200). So: ONE parse function, THREE registry entries.

Signal (verified live 2026-09-13 on all three URLs)
---------------------------------------------------
Primary — JSON-LD ``offers.availability``::

    "offers": {"@type":"Offer","priceCurrency":"EUR",
      "availability":"https://schema.org/BackOrder","price":"899.95",
      "seller":{"@type":"Organization","name":"LDLC",...}}

Unlike Fnac/Cdiscount this field tracks reality here: a control product
(LDLC PB00386910, Sony DualSense Blanc) served ``schema.org/InStock`` with
``data-stock-web="1"`` the same minute the PS5 Pro served ``BackOrder`` with
``data-stock-web="6"``. ``BackOrder`` maps to out_of_stock in
``availability_to_status``, which is exactly what we want: "En stock dans + de
15 jours (31/12/2026)" is not a buyable console.

Secondary — ``data-stock-web="N"``: 6 = far-future backorder, low = real
stock. The attribute sits in different markup on each site (LDLC
``<div class="modal-stock-web ...">``, Materiel.net ``<button class="...
js__stock_web">``, RDC ``<span class="modal-stock-web ...">`` inside a
``data-stockcode`` wrapper), so it is extracted by attribute alone, never via
the surrounding element.

Traps
-----
* The add-to-cart button is ALWAYS in the HTML on all three sites, unavailable
  or not (``data-label-in-stock="Ajouter au panier"`` is a static data
  attribute). Button presence is never used as a signal.
* Rue du Commerce has a marketplace; LDLC and Materiel.net do not. All three
  are gated on ``offers.seller.name`` anyway — exact strings confirmed live:
  "LDLC", "Materiel.net", "Rue du Commerce".
* RDC search/listing pages are client-rendered; only the canonical product URL
  is pinned here.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

try:                                    # package import (check.py)
    from .base import (
        DEFAULT_MAX_PRICE, STATUS_IN, STATUS_OUT, STATUS_UNKNOWN,
        Result, Retailer, availability_to_status, fetch_plain,
        jsonld_product_offer, looks_blocked, parse_price, price_ok,
    )
except ImportError:                     # direct `python3 retailers/ldlcgroup.py`
    from base import (                  # type: ignore[no-redef]
        DEFAULT_MAX_PRICE, STATUS_IN, STATUS_OUT, STATUS_UNKNOWN,
        Result, Retailer, availability_to_status, fetch_plain,
        jsonld_product_offer, looks_blocked, parse_price, price_ok,
    )

SKU = "AR202410030051"

_STOCK_WEB_RE = re.compile(r'data-stock-web="(\d+)"')

# Observed: 1 and 2 on immediately-shippable products, 6 on "+ de 15 jours".
_STOCK_WEB_BACKORDER = 6        # >= this means far-future / not buyable now
_STOCK_WEB_IMMEDIATE = 2        # <= this means really on the shelf


def _stock_web(html: str) -> Optional[int]:
    """Lowest ``data-stock-web`` value on the page, whatever markup holds it."""
    vals = [int(v) for v in _STOCK_WEB_RE.findall(html)]
    return min(vals) if vals else None


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _offer_seller(offer: dict) -> Optional[str]:
    seller = offer.get("seller")
    if isinstance(seller, dict):
        name = seller.get("name")
        return name if isinstance(name, str) and name.strip() else None
    if isinstance(seller, str) and seller.strip():
        return seller
    return None


def _parse(html: str, expected_seller: str, site: str) -> Result:
    """Shared parser. Anything unrecognised is `unknown`, never out_of_stock."""
    blocked = looks_blocked(html)
    if blocked:
        return Result(STATUS_UNKNOWN, "%s : %s" % (site, blocked))

    offer = jsonld_product_offer(html)
    if not isinstance(offer, dict):
        return Result(STATUS_UNKNOWN,
                      "%s : JSON-LD Product/offers introuvable (page changée ?)" % site)

    price = parse_price(offer.get("price"))
    seller = _offer_seller(offer)
    price_txt = "%.2f EUR" % price if price is not None else "prix inconnu"

    if seller is None:
        return Result(STATUS_UNKNOWN,
                      "%s : offers.seller absent — vendeur non vérifiable" % site,
                      price=price)

    raw_avail = offer.get("availability")
    if not isinstance(raw_avail, str) or not raw_avail:
        return Result(STATUS_UNKNOWN,
                      "%s : offers.availability absent (schéma changé ?)" % site,
                      price=price, seller=seller)

    short_avail = raw_avail.rsplit("/", 1)[-1]
    status = availability_to_status(raw_avail)
    if status == STATUS_UNKNOWN:
        return Result(STATUS_UNKNOWN,
                      "%s : availability=%s non reconnu" % (site, short_avail),
                      price=price, seller=seller)

    # --- seller gate (marketplace / scalper) --------------------------------
    if _norm(seller) != _norm(expected_seller):
        # A real determination, not a parse failure: someone else is selling it,
        # so the retailer itself is not. Never alert on this.
        return Result(STATUS_OUT,
                      "%s : vendeur tiers « %s » (marketplace), pas %s — ignoré"
                      % (site, seller, expected_seller),
                      price=price, seller=seller)

    stock_web = _stock_web(html)
    sw_txt = "" if stock_web is None else ", data-stock-web=%d" % stock_web

    # --- corroboration between the two signals ------------------------------
    if status == STATUS_OUT and stock_web is not None and stock_web <= _STOCK_WEB_IMMEDIATE:
        return Result(STATUS_UNKNOWN,
                      "%s : signaux contradictoires availability=%s mais "
                      "data-stock-web=%d" % (site, short_avail, stock_web),
                      price=price, seller=seller)
    if status == STATUS_IN and stock_web is not None and stock_web >= _STOCK_WEB_BACKORDER:
        return Result(STATUS_UNKNOWN,
                      "%s : signaux contradictoires availability=%s mais "
                      "data-stock-web=%d (dispo lointaine)" % (site, short_avail, stock_web),
                      price=price, seller=seller)

    # --- price sanity (defence in depth) ------------------------------------
    if status == STATUS_IN and not price_ok(price, DEFAULT_MAX_PRICE):
        return Result(STATUS_OUT,
                      "%s : availability=%s mais prix aberrant %s — ignoré"
                      % (site, short_avail, price_txt),
                      price=price, seller=seller)

    return Result(
        status,
        "%s : JSON-LD availability=%s%s, %s, vendeur %s"
        % (site, short_avail, sw_txt, price_txt, seller),
        price=price,
        seller=seller,
    )


def make_parser(expected_seller: str, site: str) -> Callable[[str], Result]:
    """Bind the first-party seller name expected on a given site."""
    def _parser(html: str) -> Result:
        return _parse(html, expected_seller, site)
    _parser.__name__ = "parse_%s" % _norm(site)
    return _parser


parse_ldlc = make_parser("LDLC", "LDLC")
parse_materielnet = make_parser("Materiel.net", "Materiel.net")
parse_rueducommerce = make_parser("Rue du Commerce", "Rue du Commerce")


RETAILERS: list[Retailer] = [
    Retailer(
        key="ldlc",
        name="LDLC",
        url="https://www.ldlc.com/fiche/PB00649574.html",
        fetch=fetch_plain,
        parse=parse_ldlc,
        note="Groupe LDLC, SKU %s — aucun anti-bot" % SKU,
    ),
    Retailer(
        key="materielnet",
        name="Materiel.net",
        url="https://www.materiel.net/produit/202410030051.html",
        fetch=fetch_plain,
        parse=parse_materielnet,
        note="Groupe LDLC, SKU %s — aucun anti-bot" % SKU,
    ),
    Retailer(
        key="rueducommerce",
        name="Rue du Commerce",
        url="https://www.rueducommerce.fr/p/r24107819362.html",
        fetch=fetch_plain,
        parse=parse_rueducommerce,
        note="Groupe LDLC, SKU %s — marketplace présente : gate vendeur obligatoire" % SKU,
    ),
]


if __name__ == "__main__":
    for r in RETAILERS:
        try:
            res = r.parse(fetch_plain(r))
        except Exception as exc:                     # noqa: BLE001
            res = Result(STATUS_UNKNOWN, "fetch KO: %s" % exc)
        print("%-16s %-12s %s | prix=%s vendeur=%s"
              % (r.key, res.status, res.note, res.price, res.seller))

    # Positive control: proves the in_stock branch actually fires. LDLC
    # PB00386910 (Sony DualSense Blanc) is in stock as of 2026-09-13.
    control = Retailer(
        key="ldlc-control",
        name="LDLC (contrôle en stock)",
        url="https://www.ldlc.com/fiche/PB00386910.html",
        fetch=fetch_plain,
        parse=parse_ldlc,
    )
    try:
        res = control.parse(fetch_plain(control))
    except Exception as exc:                         # noqa: BLE001
        res = Result(STATUS_UNKNOWN, "fetch KO: %s" % exc)
    print("%-16s %-12s %s | prix=%s vendeur=%s"
          % (control.key, res.status, res.note, res.price, res.seller))
