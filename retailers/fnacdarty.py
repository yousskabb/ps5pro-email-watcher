"""Fnac + Darty — one module, two Retailer entries.

Both sites sit behind ONE shared Akamai + DataDome edge: the deny page is
byte-identical on either domain (``<title>FNAC DARTY - Maintenance</title>``).
Plain urllib/requests/curl gets 403 100% of the time, even from a French
residential IP, so both entries use ``fetch_impersonate`` (curl_cffi, profile
``firefox133`` — the only profile verified to work on BOTH domains; every
chrome*/edge* profile is rejected, and safari* works on Fnac but not Darty).

FNAC
----
Fnac A/B-rotates TWO PDP templates on the same URL, so the parser handles both:

* OLD (server-rendered):  ``<p data-automation-id="product-availability">`` inside
  a ``f-buyBox-availability f-spot-web-desktop f-buyBox__availabilityStatus--*``
  block, plus a hidden input in ``form.js-ProductBuy`` carrying the seller.
* NEW (React/Next):       ``<span data-automation-id="pdp-buyBox-webAvailability-status">``.
  The NEW template also embeds its JSON backslash-escaped (``\\"``), so every
  JSON regex runs against an unescaped copy of the document.

Both templates carry the Adobe CEDDL ``digital-data`` payload, whose product
attributes block is the cross-template corroboration signal:
``"availability":"…","availabilityType":"out-of-stock","availabilityID":"198"``.

Two traps, honoured below:

1. **Fnac's JSON-LD lies.** The sold-out PS5 Pro PDP ships
   ``"availability":"http://schema.org/LimitedAvailability"`` and never
   ``OutOfStock`` — and ``availability_to_status()`` maps LimitedAvailability to
   ``in_stock``. So base.py's ``jsonld_product_offer`` / ``availability_to_status``
   are deliberately NOT used for Fnac; they would page us on a dead page.
2. **The numeric availability code is context-dependent.** ``data-availability``
   /``availabilityID`` ``103`` is "marketplace in stock" on a listing page but
   ``discontinued`` on the PS5 Pro bundle PDP (verified on fnac_bundle.html).
   We key on the textual ``availabilityType`` instead, never on the number.

``sellerType="store"`` (sellerName ``ClickAndCollectOnly``) is Fnac itself, not
a scalper — it is the click-and-collect-only state the sold-out PS5 Pro shows in
the NEW template. We therefore accept it as "seller = Fnac" (no marketplace
rejection) but it can NEVER by itself produce ``in_stock``: an alert must mean
"buyable online right now", and that requires a positive *web* availability
signal, which a store-only offer by definition does not have.

DARTY
-----
Richest signal is the TagCommander meta block. ``product_stock`` is emitted
TWICE and the first copy is EMPTY, so we always take the LAST non-empty value:

    <meta data-tagcommander name="product_seller_type" content="darty">
    <meta data-tagcommander name="product_stock" content="">
    <meta data-tagcommander name="product_stock" content="produit indisponible">
    <meta data-tagcommander name="product_btn_AaP" content="0">

An in-stock marketplace item shows ``product_seller_type=marketplace``,
``product_stock=en stock``, ``product_btn_AaP=1`` (verified on darty_mkp.html).

Corroborated by schema.org MICRODATA (not JSON-LD): ``<meta itemprop="seller"
content="DARTY">`` + ``<link itemprop="availability" href=".../OutOfStock">``.
An in-stock item was seen reporting ``OnlineOnly``, NOT ``InStock``, so the
microdata test is *not OutOfStock* rather than equality.

Trap: the visible "PRODUIT INDISPONIBLE" is assembled by CSS and is NOT a
string in the HTML — don't grep for it. The buy box renders
``<div class="buybox-unavailable"><p class="title">Bientôt de retour en stock</p>``.

Never append ``?ofmp=<id>`` to the Darty URL: that switches the buybox to one
specific marketplace offer.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from .base import (STATUS_IN, STATUS_OUT, STATUS_UNKNOWN, Result, Retailer,
                   fetch_impersonate, looks_blocked, parse_price)

# --------------------------------------------------------------------- config

FNAC_URL = "https://www.fnac.com/Console-Sony-PS5-Pro-Blanc-et-Noir/a20901133/w-4"
DARTY_URL = ("https://www.darty.com/nav/achat/console_jeux/univers_ps5/"
             "console_ps5/sony_ps5_pro.html")

# Seller types that ARE the retailer itself (vs. a marketplace/pro seller).
FNAC_FIRST_PARTY = {"fnac", "store"}
DARTY_FIRST_PARTY = {"darty"}

_OUT_WORDS = re.compile(
    r"épuis|indisponible|rupture|bientôt en stock|plus disponible|non disponible",
    re.I)
_IN_WORDS = re.compile(r"en stock|disponible en ligne", re.I)


def _unescape(html: str) -> str:
    """The NEW Fnac PDP embeds its JSON payload backslash-escaped."""
    return html.replace('\\"', '"')


# ----------------------------------------------------------------------- Fnac

def _fnac_seller(html: str, un: str) -> Tuple[Optional[str], Optional[str]]:
    """(sellerType, sellerName) of the offer holding the main buy box.

    OLD template: hidden input inside ``form.js-ProductBuy``. Note the sibling
    attribute ``data-sellertypes`` (plural, a list of ALL seller types that have
    an offer) — matching on ``data-sellertype="`` with the closing quote avoids it.
    NEW template: the CEDDL product block's flat sellerType/sellerName pair.
    """
    m = re.search(
        r'<form[^>]*\bjs-ProductBuy\b[^>]*>.{0,2000}?<input[^>]*\bdata-sellertype="([^"]*)"',
        html, re.S)
    if m:
        stype = m.group(1)
        form = html[m.start():m.end() + 400]
        n = re.search(r'data-sellername="([^"]*)"', form)
        if not n:  # attribute order varies; widen to the same input tag
            n = re.search(r'data-sellername="([^"]*)"', html[m.start():m.start() + 2600])
        return stype.lower() or None, (n.group(1) if n else None)

    m = re.search(r'"sellerType":"([a-zA-Z]+)","sellerName":"([^"]*)"', un)
    if m:
        return m.group(1).lower(), m.group(2)
    return None, None


def _fnac_ceddl(un: str) -> Tuple[Optional[str], Optional[float]]:
    """(availabilityType, priceWithTax) from the CEDDL product attributes block.

    Anchored on the ``"availabilityID"`` that always follows it, because the NEW
    React tree also carries decorative ``"availabilityType":"store"|"web"`` props
    on layout components which must NOT be read as product state.
    """
    m = re.search(r'"availabilityType":"([a-z-]+)","availabilityID"', un)
    if not m:
        return None, None
    tail = un[m.end():m.end() + 2000]
    p = re.search(r'"priceWithTax":\s*([0-9]+(?:\.[0-9]+)?)', tail)
    return m.group(1), (float(p.group(1)) if p else None)


def _fnac_web_availability(html: str, un: str) -> Optional[str]:
    """The *online* availability sentence, or None if the template omits it."""
    # NEW template (React). Present unescaped in the DOM and escaped in the
    # flight payload; searching the unescaped copy catches both.
    m = re.search(
        r'data-automation-id="pdp-buyBox-webAvailability-status"[^>]*>\s*([^<]{2,80})',
        un)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()

    # OLD template: several `product-availability` <p>s (web, then store).
    # Keep the one that talks about "en ligne"; "en magasin" is click & collect.
    for txt in re.findall(
            r'data-automation-id="product-availability"[^>]*>\s*([^<]{2,80})', html):
        t = re.sub(r"\s+", " ", txt).strip()
        if "en ligne" in t.lower():
            return t
    return None


def parse_fnac(html: str) -> Result:
    blocked = looks_blocked(html)
    if blocked:
        return Result(STATUS_UNKNOWN, blocked)

    un = _unescape(html)
    is_new = "pdp-buyBox-webAvailability-status" in un
    is_old = ('data-automation-id="product-offer-fnac"' in html
              or "js-ProductBuy" in html)
    if not (is_new or is_old):
        return Result(STATUS_UNKNOWN, "page Fnac non reconnue (template inconnu ?)")
    tpl = "NEW" if is_new else "OLD"

    stype, sname = _fnac_seller(html, un)
    avail_type, price = _fnac_ceddl(un)
    seller = sname or (stype.upper() if stype else None)

    # Marketplace / pro seller holds the buy box -> Fnac itself is not selling.
    if stype and stype not in FNAC_FIRST_PARTY:
        return Result(STATUS_OUT,
                      "offre principale marketplace (%s) — pas Fnac" % (seller or stype),
                      price=price, seller=seller)

    web = _fnac_web_availability(html, un)
    ceddl_out = avail_type in ("out-of-stock", "sold-out", "discontinued",
                               "preorder", "backorder")
    ceddl_in = avail_type == "in-stock"

    if web:
        if _OUT_WORDS.search(web):
            return Result(STATUS_OUT, "%s (%s)" % (web, tpl), price=price, seller=seller)
        if _IN_WORDS.search(web):
            if stype is None:
                return Result(STATUS_UNKNOWN,
                              "« %s » mais vendeur illisible (%s)" % (web, tpl),
                              price=price, seller=seller)
            if stype == "store":
                return Result(STATUS_OUT,
                              "retrait magasin uniquement — pas de stock en ligne (%s)" % tpl,
                              price=price, seller=seller)
            if ceddl_out:
                return Result(STATUS_UNKNOWN,
                              "signaux contradictoires : « %s » vs availabilityType=%s"
                              % (web, avail_type), price=price, seller=seller)
            return Result(STATUS_IN, "%s — vendu par %s (%s)" % (web, seller or "Fnac", tpl),
                          price=price, seller=seller)
        return Result(STATUS_UNKNOWN, "disponibilité en ligne illisible : « %s »" % web,
                      price=price, seller=seller)

    # No online sentence at all. The OLD template drops it when the item IS
    # buyable (it renders the delivery block + "Ajouter au panier" instead), so
    # fall back to the CEDDL state, corroborated by the buy button.
    has_buy_btn = 'data-automation-id="product-buy-btn"' in html
    if ceddl_in and stype in ("fnac",) and has_buy_btn:
        return Result(STATUS_IN, "En stock en ligne (CEDDL in-stock, %s)" % tpl,
                      price=price, seller=seller)
    if ceddl_out:
        return Result(STATUS_OUT, "availabilityType=%s (%s)" % (avail_type, tpl),
                      price=price, seller=seller)
    # Last resort: the OLD template's semantic state class (not a hashed name).
    if "f-buyBox__availabilityStatus--unavailable" in html and not has_buy_btn:
        return Result(STATUS_OUT, "buyBox indisponible (%s)" % tpl,
                      price=price, seller=seller)
    return Result(STATUS_UNKNOWN,
                  "aucun marqueur de stock Fnac exploitable (%s)" % tpl,
                  price=price, seller=seller)


# ---------------------------------------------------------------------- Darty

def _tc_meta(html: str, name: str) -> Optional[str]:
    """Last NON-EMPTY TagCommander meta with this name.

    product_stock is emitted twice and the first copy is empty — taking the
    first (or a naive "any") value reads a sold-out console as unknown.
    """
    vals = [v.strip() for v in re.findall(
        r'<meta[^>]*\bdata-tagcommander\b[^>]*name="%s"[^>]*content="([^"]*)"'
        % re.escape(name), html, re.I) if v.strip()]
    return vals[-1] if vals else None


def parse_darty(html: str) -> Result:
    blocked = looks_blocked(html)
    if blocked:
        return Result(STATUS_UNKNOWN, blocked)

    if "data-tagcommander" not in html:
        return Result(STATUS_UNKNOWN, "page Darty non reconnue (metas TagCommander absentes)")

    seller_type = (_tc_meta(html, "product_seller_type") or "").lower()
    seller = _tc_meta(html, "product_seller") or None
    stock = (_tc_meta(html, "product_stock") or "").lower()
    aap = _tc_meta(html, "product_btn_AaP")           # Ajouter au Panier
    price = parse_price(_tc_meta(html, "product_unitprice_ttc"))
    if price == 0:                                    # 0 = "pas d'offre", not free
        price = None

    # Microdata (schema.org), NOT JSON-LD. An in-stock item was seen reporting
    # OnlineOnly rather than InStock, so we only test for an explicit OutOfStock.
    m = re.search(r'itemprop="availability"[^>]*href="[^"]*schema\.org/(\w+)"', html)
    micro = m.group(1) if m else None
    m = re.search(r'itemprop="seller"[^>]*content="([^"]*)"', html)
    micro_seller = m.group(1) if m else None
    seller = seller or micro_seller

    if not seller_type and micro_seller:
        seller_type = "darty" if micro_seller.strip().lower() == "darty" else "marketplace"
    if not seller_type:
        return Result(STATUS_UNKNOWN, "vendeur principal illisible", price=price, seller=seller)
    if seller_type not in DARTY_FIRST_PARTY:
        return Result(STATUS_OUT, "offre principale marketplace (%s) — pas Darty"
                      % (seller or seller_type), price=price, seller=seller)

    if micro == "OutOfStock" or "buybox-unavailable" in html or _OUT_WORDS.search(stock):
        return Result(STATUS_OUT, stock or "buybox indisponible", price=price, seller=seller)

    if _IN_WORDS.search(stock) and aap != "0":
        return Result(STATUS_IN, "%s — vendu par Darty" % stock, price=price, seller=seller)
    if aap == "1" and micro != "OutOfStock":
        return Result(STATUS_IN, "ajout au panier actif — vendu par Darty",
                      price=price, seller=seller)
    if stock or aap:
        return Result(STATUS_UNKNOWN,
                      "état ambigu (stock=%r, AaP=%r, micro=%r)" % (stock, aap, micro),
                      price=price, seller=seller)
    return Result(STATUS_UNKNOWN, "aucun marqueur de stock Darty exploitable",
                  price=price, seller=seller)


# -------------------------------------------------------------------- registry

RETAILERS = [
    Retailer(
        key="fnac",
        name="Fnac",
        url=FNAC_URL,
        parse=parse_fnac,
        fetch=fetch_impersonate,          # curl_cffi, profile firefox133
        note="Akamai+DataDome ; deux templates PDP en A/B ; JSON-LD mensonger",
    ),
    Retailer(
        key="darty",
        name="Darty",
        url=DARTY_URL,
        parse=parse_darty,
        fetch=fetch_impersonate,          # même edge que Fnac
        note="ne JAMAIS ajouter ?ofmp= : ça bascule la buybox sur une offre marketplace",
    ),
]


# ------------------------------------------------------------------ validation

if __name__ == "__main__":                                        # pragma: no cover
    import os
    import sys

    HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(HERE))
    FIX = os.environ.get("FIXTURES", "/private/tmp/claude-501/"
                         "-Users-youssefkabbaj-Documents-Extension-PS5-Pro-"
                         "ps5pro-email-watcher/e88af860-a8ea-4515-957a-88c3328377cb/"
                         "scratchpad")

    BLOCKED = ("<!DOCTYPE html><html><head><title>FNAC DARTY - Maintenance</title>"
               "</head><body>Access Denied</body></html>")

    CASES = [
        ("fnac_firefox133.html",      parse_fnac,  STATUS_OUT, "Fnac PS5 Pro template OLD"),
        ("fnac_safari17_0.html",      parse_fnac,  STATUS_OUT, "Fnac PS5 Pro template NEW"),
        ("fnac_instock_OLD.html",     parse_fnac,  STATUS_IN,  "Fnac témoin EN STOCK (OLD)"),
        ("fnac_instock_NEW.html",     parse_fnac,  STATUS_IN,  "Fnac témoin EN STOCK (NEW)"),
        ("fnac_bundle.html",          parse_fnac,  STATUS_OUT, "Fnac pack PS5 Pro (code 103)"),
        ("darty_firefox133.html",     parse_darty, STATUS_OUT, "Darty PS5 Pro"),
        ("darty_safari17_2_ios.html", parse_darty, STATUS_OUT, "Darty PS5 Pro (mobile)"),
        ("darty_mkp.html",            parse_darty, STATUS_OUT, "Darty marketplace EN STOCK -> rejeté"),
    ]

    print("=== hors-ligne (HTML capturés) " + "=" * 40)
    bad = 0
    for fname, fn, want, label in CASES:
        path = os.path.join(FIX, fname)
        if not os.path.exists(path):
            print("  SKIP %-28s fichier absent" % fname)
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            res = fn(fh.read())
        ok = res.status == want
        bad += not ok
        print("  %s %-28s %-12s attendu=%-12s price=%-8s seller=%-14s %s"
              % ("OK " if ok else "ECHEC", fname, res.status, want,
                 res.price, (res.seller or "")[:14], res.note))
        print("      %s" % label)

    for fn, who in ((parse_fnac, "fnac"), (parse_darty, "darty")):
        res = fn(BLOCKED)
        ok = res.status == STATUS_UNKNOWN
        bad += not ok
        print("  %s %-28s %-12s attendu=%-12s %s"
              % ("OK " if ok else "ECHEC", "page 403/maintenance (%s)" % who,
                 res.status, STATUS_UNKNOWN, res.note))
    res = parse_fnac("")
    print("  %s %-28s %-12s %s" % ("OK " if res.status == STATUS_UNKNOWN else "ECHEC",
                                   "réponse vide", res.status, res.note))

    print("=== en direct (curl_cffi firefox133) " + "=" * 33)
    for r in RETAILERS:
        try:
            html = (r.fetch or (lambda x: ""))(r)
            res = r.parse(html)
            print("  %-6s %-12s %8d octets  price=%-8s seller=%-14s %s"
                  % (r.key, res.status, len(html), res.price,
                     (res.seller or "")[:14], res.note))
        except Exception as e:                                    # noqa: BLE001
            print("  %-6s FETCH KO   %s: %s" % (r.key, type(e).__name__, e))

    sys.exit(1 if bad else 0)
