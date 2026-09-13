"""Carrefour.fr — Cloudflare bypassed by HTTP/1.1, first-party offers only.

Anti-bot layer (verified live 2026-09-13)
-----------------------------------------
Carrefour's Cloudflare returns **HTTP/2 403** with ``cf-mitigated: challenge``
to every client tried, *including* a full browser header set and a real TLS
fingerprint — but **HTTP/1.1 200** with the complete product HTML,
reproducibly. The discriminator is the protocol fingerprint, not the headers.
Forcer HTTP/1.1 suffit depuis une IP residentielle, mais PAS depuis les IP
Azure de GitHub Actions : le 2026-09-13 en prod, les deux fiches ont recu un
challenge Cloudflare. ``fetch_impersonate`` (curl_cffi firefox133) passe dans
les deux cas -- l'empreinte TLS/HTTP2 compte autant que le protocole.
L'ancienne note ``base.fetch_http11`` etait
exactly the right tool and no impersonation library is needed. Captured proof:
``crf_*.hdr`` (HTTP/2 403 challenge) vs ``crf11_*.hdr`` (HTTP/1.1 200).

The challenge page is detected explicitly rather than assumed away:
``cf_chl_opt`` / ``challenge-platform`` / ``cf-mitigated`` in the body =>
``unknown``. ``base.looks_blocked`` covers all three and, unlike on Cdiscount,
does NOT false-positive here — the real Carrefour HTML contains none of those
markers (verified on both captures).

Data source — ``window.__INITIAL_STATE__``
------------------------------------------
The Vuex store embedded in the page is the single best stock signal found
anywhere in this project, because each offer states its own seller identity::

    vuex.analytics.indexedEntities.product.<EAN>.attributes.offers.<EAN> = {
      "0261-150-6":    {"type":"offer","subType":"carrefour","id":"0261-150-6",
                        "attributes":{"availability":{"purchasable":true,
                          "stopped":false,"suspended":false,
                          "hasLowQuantity":false},
                        "price":{"price":899.99},"marketplace":null,
                        "ean":"0711719024040"},"meta":{"cta":"buy"}},
      "A1QD-151-4000": {"subType":"carrefour","attributes":{"availability":{
                          "purchasable":false,…},"price":{"price":899.99},
                        "marketplace":null},"meta":{"cta":null}},
      "MKP_FOOD-2713": {"subType":"marketplace","attributes":{
                        "price":{"price":2650.55},
                        "marketplace":{"seller":"Stock Electronique"}}},
      "MKP_FOOD-4383": {"subType":"marketplace","attributes":{
                        "price":{"price":2786.36},
                        "marketplace":{"seller":"Shopelya"}}}}

First-party gate, ALL required:
  ``subType == "carrefour"`` AND ``marketplace is None`` AND the offer id does
  not start with ``MKP_`` AND ``availability.purchasable`` AND not ``stopped``
  AND not ``suspended`` AND ``price <= max_price``.

The gate filters on **seller identity, not price**: verified that raising the
cap to 3 000 EUR still rejects both scalpers (2 650,55 / 2 786,36), because
they fail ``subType``/``marketplace`` long before price is considered. Keep it
that way — a price-only filter would alert the moment a scalper undercut the
cap.

STORE DEPENDENCY — read this before trusting ``in_stock``
----------------------------------------------------------
Offer ``0261-150-6`` is a **store-specific Drive** offer: its id is shaped
``<storeId>-<serviceId>-<n>`` (4 digits, dash). It reads ``purchasable: true``.
The **national home-delivery** offer ``A1QD-151-4000`` reads
``purchasable: false``. So on this EAN, first-party "available" means
*collectable at some Carrefour Drive*, NOT *deliverable to you*.

Worse, a bare fetch has no store cookie, so Carrefour resolves a default store
**from the caller's IP** — which on a GitHub Actions runner is a datacentre in
who-knows-where, and will differ from the user's own browsing. The Drive an
alert names may therefore be nowhere near the user.

``FIRST_PARTY_MODE`` is the switch:
  * ``"any"`` (default) — accept any first-party offer, Drive included, and say
    so in ``Result.note``. Maximum recall: a Drive appearing is a real restock
    signal even if that particular store is far away.
  * ``"delivery"`` — reject Drive ids (``^\\d{4}-``) and require a national
    delivery offer. Fewer, more actionable alerts; will miss Drive-only drops.

Fallback — JSON-LD
------------------
Weaker and only used when the Vuex store is missing or reshaped. It has **no
``purchasable`` field**, so it cannot distinguish "listed" from "buyable"; it
is treated as suggestive, and a JSON-LD-only in-stock read is reported as
``unknown`` rather than firing an alert on a field that does not track stock.
First-party sellers are named ``Carrefour Drive`` / ``Carrefour Livraison``;
marketplace offers carry ``?s=<sellerId>`` in their offer url.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

try:                                    # package import (check.py)
    from .base import (
        DEFAULT_MAX_PRICE, STATUS_IN, STATUS_OUT, STATUS_UNKNOWN,
        Result, Retailer, fetch_impersonate, iter_jsonld, looks_blocked,
        parse_price, price_ok,
    )
except ImportError:                     # direct `python3 retailers/carrefour.py`
    from base import (                  # type: ignore[no-redef]
        DEFAULT_MAX_PRICE, STATUS_IN, STATUS_OUT, STATUS_UNKNOWN,
        Result, Retailer, fetch_impersonate, iter_jsonld, looks_blocked,
        parse_price, price_ok,
    )

# "any" -> Drive offers count as first-party stock (default, max recall).
# "delivery" -> only national home-delivery offers count.
# "delivery" (default): only the national home-delivery offer counts.
# "any" also accepts a store-specific Drive offer — but which store a bare
# fetch resolves to depends on the CALLER'S IP, so on a GitHub runner that is
# an arbitrary town. As of 2026-09-13 the 2 To page has a purchasable Drive
# offer at 899.99, so "any" would alert on the first run and hourly forever
# for a click-and-collect slot you cannot reach. Flip to "any" if you are
# willing to chase Drive pickups.
FIRST_PARTY_MODE = "delivery"

# Store-specific Drive offer ids look like "0261-150-6"; the national
# home-delivery offer looks like "A1QD-151-4000".
_DRIVE_ID_RE = re.compile(r"^\d{4}-")

_STATE_MARKER = "window.__INITIAL_STATE__="


def _initial_state(html: str) -> Optional[dict]:
    """Extract ``window.__INITIAL_STATE__`` by brace matching.

    A regex to the end of the assignment is not safe here: the payload is
    ~100 kB of nested JSON containing both ``;`` and ``</script>``-adjacent
    strings. Depth counting with string/escape awareness is.
    """
    i = html.find(_STATE_MARKER)
    if i < 0:
        return None
    s = html[i + len(_STATE_MARKER):]
    depth = 0
    in_str = False
    escaped = False
    for k, c in enumerate(s):
        if in_str:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[:k + 1])
                except ValueError:
                    return None
    return None


def _offers(state: dict, ean: str) -> Optional[dict]:
    try:
        return (state["vuex"]["analytics"]["indexedEntities"]
                ["product"][ean]["attributes"]["offers"][ean])
    except (KeyError, TypeError):
        return None


def _is_first_party(offer_id: str, offer: dict) -> bool:
    """Seller identity only. Price is checked separately, never here."""
    attrs = offer.get("attributes") or {}
    return (offer.get("subType") == "carrefour"
            and attrs.get("marketplace") is None
            and not offer_id.startswith("MKP_"))


def _buyable(offer: dict) -> bool:
    av = (offer.get("attributes") or {}).get("availability") or {}
    return bool(av.get("purchasable")
                and not av.get("stopped")
                and not av.get("suspended"))


def _price_of(offer: dict) -> Optional[float]:
    return parse_price(((offer.get("attributes") or {}).get("price") or {})
                       .get("price"))


def _seller_of(offer: dict) -> Optional[str]:
    mkp = (offer.get("attributes") or {}).get("marketplace")
    if isinstance(mkp, dict):
        return mkp.get("seller")
    return None


def _from_state(offers: dict, max_price: Optional[float]) -> Result:
    rejected = []
    # On the out_of_stock path, report the FIRST-PARTY offer's price/seller
    # when one exists. Surfacing a scalper's 2 650 EUR as `Result.price` would
    # poison the state file and any price history built from it.
    fp_price = None
    fp_seen = False
    mkp_price = None
    mkp_seller = None

    for oid, offer in offers.items():
        if not isinstance(offer, dict):
            continue
        price = _price_of(offer)
        price_txt = "%.2f EUR" % price if price is not None else "prix inconnu"

        if not _is_first_party(oid, offer):
            seller = _seller_of(offer) or "vendeur tiers"
            rejected.append("%s marketplace « %s » %s" % (oid, seller, price_txt))
            if mkp_seller is None:
                mkp_price, mkp_seller = price, seller
            continue

        fp_seen = True
        if fp_price is None:
            fp_price = price

        is_drive = bool(_DRIVE_ID_RE.match(oid))
        if FIRST_PARTY_MODE == "delivery" and is_drive:
            rejected.append("%s Drive magasin (mode « delivery »)" % oid)
            continue
        if not _buyable(offer):
            rejected.append("%s Carrefour non disponible" % oid)
            continue
        if not price_ok(price, max_price):
            rejected.append("%s Carrefour mais %s > plafond" % (oid, price_txt))
            continue

        if is_drive:
            note = ("Carrefour : offre 1P Drive %s (retrait magasin), %s — "
                    "ATTENTION magasin dépendant de l'IP du runner, pas "
                    "forcément près de chez toi" % (oid, price_txt))
        else:
            note = ("Carrefour : offre 1P livraison %s, %s" % (oid, price_txt))
        return Result(STATUS_IN, note, price=price, seller="Carrefour")

    if not offers:
        return Result(STATUS_UNKNOWN, "Carrefour : aucune offre dans le store Vuex")
    return Result(STATUS_OUT,
                  "Carrefour : aucune offre 1P achetable — %s"
                  % "; ".join(rejected),
                  price=fp_price if fp_seen else mkp_price,
                  seller="Carrefour" if fp_seen else mkp_seller)


def _jsonld_fallback(html: str, max_price: Optional[float]) -> Optional[Result]:
    """Weaker signal: no ``purchasable`` field, so never returns in_stock."""
    for obj in iter_jsonld(html):
        if not isinstance(obj, dict) or str(obj.get("@type", "")).lower() != "product":
            continue
        agg = obj.get("offers")
        if isinstance(agg, dict):
            sub = agg.get("offers")
            candidates = sub if isinstance(sub, list) else [agg]
        elif isinstance(agg, list):
            candidates = agg
        else:
            continue
        for off in candidates:
            if not isinstance(off, dict):
                continue
            seller = ((off.get("seller") or {}).get("name")
                      if isinstance(off.get("seller"), dict) else None) or ""
            url = str(off.get("url") or "")
            if not seller.lower().startswith("carrefour") or "?s=" in url:
                continue          # marketplace
            if not str(off.get("availability", "")).lower().endswith("instock"):
                continue
            price = parse_price(off.get("price"))
            if not price_ok(price, max_price):
                continue
            # Deliberately NOT in_stock: JSON-LD has no purchasable flag here,
            # so this is a hint to go look, not grounds to fire an alert.
            return Result(STATUS_UNKNOWN,
                          "Carrefour : store Vuex absent ; JSON-LD annonce "
                          "« %s » InStock à %s — à vérifier à la main"
                          % (seller,
                             "%.2f EUR" % price if price else "prix inconnu"),
                          price=price, seller=seller)
        return Result(STATUS_OUT,
                      "Carrefour : store Vuex absent ; JSON-LD sans offre 1P "
                      "en stock")
    return None


def _parse(html: str, ean: str, max_price: Optional[float]) -> Result:
    # looks_blocked is correct on this domain: it catches cf_chl_opt /
    # challenge-platform / cf-mitigated, none of which appear in real
    # Carrefour product HTML (verified on both captures).
    blocked = looks_blocked(html)
    if blocked:
        return Result(STATUS_UNKNOWN, "Carrefour : %s" % blocked)

    state = _initial_state(html)
    if state is not None:
        offers = _offers(state, ean)
        if offers is not None:
            return _from_state(offers, max_price)
        return Result(STATUS_UNKNOWN,
                      "Carrefour : EAN %s absent du store Vuex "
                      "(fiche déplacée ?)" % ean)

    fallback = _jsonld_fallback(html, max_price)
    if fallback is not None:
        return fallback
    return Result(STATUS_UNKNOWN,
                  "Carrefour : ni __INITIAL_STATE__ ni JSON-LD Product "
                  "(%d octets)" % len(html))


def make_parser(ean: str, max_price: Optional[float] = DEFAULT_MAX_PRICE
                ) -> Callable[[str], Result]:
    """Bind EAN and price cap — ``check.py`` calls ``parse(html)`` only."""
    def _parser(html: str) -> Result:
        return _parse(html, ean, max_price)
    _parser.__name__ = "parse_carrefour_%s" % ean
    return _parser


RETAILERS: list[Retailer] = [
    Retailer(
        key="carrefour-ps5pro-2tb",
        name="Carrefour (PS5 Pro 2 To)",
        url="https://www.carrefour.fr/p/console-ps5-pro-2-tb-sony-0711719024040",
        fetch=fetch_impersonate,
        parse=make_parser("0711719024040"),
        note="Cloudflare : HTTP/1.1 obligatoire (403 en HTTP/2). "
             "FIRST_PARTY_MODE=%s" % FIRST_PARTY_MODE,
    ),
    Retailer(
        key="carrefour-ps5pro",
        name="Carrefour (PS5 Pro)",
        url="https://www.carrefour.fr/p/console-ps5-pro-sony-0711719595472",
        fetch=fetch_impersonate,
        parse=make_parser("0711719595472"),
        note="Cloudflare : HTTP/1.1 obligatoire (403 en HTTP/2). "
             "FIRST_PARTY_MODE=%s" % FIRST_PARTY_MODE,
    ),
]


if __name__ == "__main__":
    import os
    import sys

    SCRATCH = ("/private/tmp/claude-501/-Users-youssefkabbaj-Documents-"
               "Extension-PS5-Pro-ps5pro-email-watcher/"
               "e88af860-a8ea-4515-957a-88c3328377cb/scratchpad")

    def show(label: str, res: Result) -> None:
        print("%-40s %-13s %s | prix=%s vendeur=%s"
              % (label, res.status, res.note, res.price, res.seller))

    def load(name: str) -> str:
        with open(os.path.join(SCRATCH, name), encoding="utf-8",
                  errors="replace") as fh:
            return fh.read()

    failures = []

    def check(label, name, ean, expected, cap=DEFAULT_MAX_PRICE, mode=None):
        # Pin the mode per case: these assertions test the seller/price gates,
        # and must not silently flip meaning when the shipped default changes.
        global FIRST_PARTY_MODE
        saved = FIRST_PARTY_MODE
        if mode is not None:
            FIRST_PARTY_MODE = mode
        try:
            res = _parse(load(name), ean, cap)
        except FileNotFoundError:
            print("%-40s FIXTURE MANQUANTE" % name)
            return None
        finally:
            FIRST_PARTY_MODE = saved
        ok = res.status == expected
        if not ok:
            failures.append("%s: attendu %s, obtenu %s"
                            % (label, expected, res.status))
        show("[%s] %s" % ("ok " if ok else "FAIL", label), res)
        return res

    print("=== OFFLINE (HTML capturé 2026-09-13) " + "=" * 30)
    check("HTTP/1.1 2 To 0711719024040 [mode any]",
          "crf11_console-ps5-pro-2-tb-sony-0711719024040.html",
          "0711719024040", STATUS_IN, mode="any")
    check("HTTP/1.1 2 To 0711719024040 [mode delivery = defaut expedie]",
          "crf11_console-ps5-pro-2-tb-sony-0711719024040.html",
          "0711719024040", STATUS_OUT, mode="delivery")
    check("HTTP/1.1 0711719595472",
          "crf11_console-ps5-pro-sony-0711719595472.html",
          "0711719595472", STATUS_OUT)
    check("HTTP/2 403 challenge 2 To",
          "crf_console-ps5-pro-2-tb-sony-0711719024040.html",
          "0711719024040", STATUS_UNKNOWN)
    check("HTTP/2 403 challenge",
          "crf_console-ps5-pro-sony-0711719595472.html",
          "0711719595472", STATUS_UNKNOWN)

    print()
    print("=== ANTI-SCALPER : plafond relevé à 3000 EUR " + "=" * 25)
    # The scalpers sit at 2650.55 and 2786.36. With a 3000 EUR cap they are
    # UNDER the price limit, so only the seller-identity gate can reject them.
    res = check("plafond 3000, 2 To (Drive 1P doit gagner)",
                "crf11_console-ps5-pro-2-tb-sony-0711719024040.html",
                "0711719024040", STATUS_IN, cap=3000.0, mode="any")
    if res is not None and res.seller != "Carrefour":
        failures.append("plafond 3000 : vendeur %r au lieu de Carrefour"
                        % res.seller)
    if res is not None and res.price is not None and res.price > 1000:
        failures.append("plafond 3000 : prix scalper %.2f retenu" % res.price)

    # Same page with 1P offers stripped out: only the two scalpers remain.
    # If the gate were price-based this would come back in_stock at 2650.55.
    raw = load("crf11_console-ps5-pro-2-tb-sony-0711719024040.html")
    offers = _offers(_initial_state(raw), "0711719024040")
    only_scalpers = {k: v for k, v in offers.items() if k.startswith("MKP_")}
    res = _from_state(only_scalpers, 3000.0)
    ok = res.status == STATUS_OUT
    if not ok:
        failures.append("scalpers seuls @3000 : attendu out_of_stock, obtenu %s"
                        % res.status)
    show("[%s] scalpers seuls, plafond 3000" % ("ok " if ok else "FAIL"), res)

    print()
    print("=== FIRST_PARTY_MODE='delivery' (rejette le Drive) " + "=" * 19)
    _mode = FIRST_PARTY_MODE
    FIRST_PARTY_MODE = "delivery"
    check("mode delivery, 2 To",
          "crf11_console-ps5-pro-2-tb-sony-0711719024040.html",
          "0711719024040", STATUS_OUT)
    FIRST_PARTY_MODE = _mode

    print()
    print("=== LIVE (HTTP/1.1) " + "=" * 51)
    for r in RETAILERS:
        try:
            res = r.parse(fetch_impersonate(r))
        except Exception as exc:                     # noqa: BLE001
            res = Result(STATUS_UNKNOWN, "fetch KO: %s" % exc)
        show(r.key, res)

    if failures:
        print("\n!! ÉCHECS OFFLINE: " + "; ".join(failures))
        sys.exit(1)
    print("\nToutes les assertions offline passent.")
