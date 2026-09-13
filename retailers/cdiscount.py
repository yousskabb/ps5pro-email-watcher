"""Cdiscount.com — Cloudflare WAF + Baleen, first-party offers only.

Anti-bot layer (verified live 2026-09-13)
-----------------------------------------
Two stacked defences, and the second one is NOT DataDome — it is Baleen, a
French vendor:

1. **Cloudflare WAF.** A bare or non-browser User-Agent gets an instant HTTP
   403 whose body is a French ``<title>Accès bloqué</title>`` page (~13 kB,
   captured as ``bare.html``). ``base.BROWSER_HEADERS`` in full is mandatory —
   it is not cosmetic. Sending only ``User-Agent`` is not enough.

2. **Baleen "challengejs".** With good headers the first GET still returns
   HTTP **200** and a ~14 kB interstitial rather than the product page. That
   interstitial is a *plain cookie echo* — no JS to run, no crypto, no proof
   of work. The body carries::

       var __blnChallengeStore={"cookie":{"sameSite":"None","secure":true,
         "name":"visit_baleen_ACM-655d43","path":"/","value":"bvgED6b7…",
         "maxAge":900},"domain":".cdiscount.com","checkChallengeParams":{
         "request_fate":"challengejs","tracking_id":"","bot_category":
         "unknown","rule_id":""}};

   Parse that JSON, set the named cookie to the given value, POST the
   ``checkChallengeParams`` to ``/.well-known/baleen/challengejs/check``, then
   re-GET. The second GET returns the real ~500 kB page. ``fetch_cdiscount``
   below implements exactly that.

   The cookie's ``maxAge`` is 900 s and the watcher polls every 5 min, so the
   ``requests.Session`` is cached at module level and reused (``_SESSION``)
   for up to ``_SESSION_TTL`` seconds — one challenge solve then serves many
   SKUs. The GitHub Actions workflow starts a fresh process every run, so a
   cold start (no cached session) must also work, and does: it just pays the
   extra round-trip.

Detection — three conditions, ALL required for ``in_stock``
-----------------------------------------------------------
1. **Seller.** Inside ``data-e2e="product-shipping"``:
   ``Vendu<!-- --> par <span…>Cdiscount</span>``  => first-party, accept.
   ``Vendu<!-- --> et <!-- -->expédié<!-- --> par <span…>X</span>``  =>
   marketplace, reject. Note React's comment markers ``<!-- -->`` splitting
   the words: a naive ``"Vendu et expédié par" in html`` is **False** on a
   page that plainly says so. The regexes below tolerate ``<!-- -->`` and
   intervening tags.
2. **Buy control.** ``data-e2e="product-add-to-cart"`` present AND
   ``aria-disabled="false"``.
3. **Price.** JSON-LD ``offers.price`` <= ``max_price``.

Traps this adapter deliberately works around
--------------------------------------------
* **JSON-LD ``availability`` is worthless here.** All 46 products in
  Cdiscount's PS5 category feed report ``schema.org/InStock``, scalpers
  included; ``OutOfStock`` was never observed once. It is used for the PRICE
  and nothing else — ``base.availability_to_status`` is deliberately NOT
  imported.
* **``base.jsonld_product_offer`` does not work on Cdiscount.** Their block is
  ``"@type":"product"`` in lowercase; the helper compares against ``"Product"``
  exactly and returns ``None``. ``base.iter_jsonld`` is reused and the type
  test redone case-insensitively.
* **``base.looks_blocked`` false-positives here.** Every healthy Cdiscount page
  carries Cloudflare's ``/cdn-cgi/challenge-platform/…/main.js`` beacon, which
  matches the helper's ``challenge-platform`` pattern. Verified on all four
  good captures AND on the 410 page. So this adapter runs its own, narrower
  block test (``_blocked``) and never calls ``looks_blocked``.
* **CSS classes are styled-components build hashes** (``sc-bkkiih``,
  ``sc-1ui6w91-0``, ``sc-ctk32h-0``). They change on every deploy. Selection
  is on ``data-e2e="…"`` only.
* **HTTP 410.** ``…/f-1035001-ps3619995009461.html`` — the URL Google still
  surfaces — is delisted and serves "Oups ! Cette page semble introuvable...".
  That is a genuine determination, so it maps to ``out_of_stock``, the one
  documented exception to the "failures are unknown" rule.

Live SKUs, all marketplace-only as of 2026-09-13:
  ps5prov2 1 599,99 EUR (Jeux Video Du Net) · ps5pro 1 299,00 EUR (CDISTRIB2)
  · hbps5profc26 1 699,99 EUR (Jeux Video Du Net).
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Callable, Optional

try:                                    # package import (check.py)
    from .base import (
        BROWSER_HEADERS, DEFAULT_MAX_PRICE, STATUS_IN, STATUS_OUT,
        STATUS_UNKNOWN, FetchError, Result, Retailer, iter_jsonld,
        parse_price, price_ok,
    )
except ImportError:                     # direct `python3 retailers/cdiscount.py`
    from base import (                  # type: ignore[no-redef]
        BROWSER_HEADERS, DEFAULT_MAX_PRICE, STATUS_IN, STATUS_OUT,
        STATUS_UNKNOWN, FetchError, Result, Retailer, iter_jsonld,
        parse_price, price_ok,
    )

# --------------------------------------------------------------- Baleen fetch

BALEEN_CHECK_URL = "https://www.cdiscount.com/.well-known/baleen/challengejs/check"
COOKIE_DOMAIN = ".cdiscount.com"

# Cookie maxAge is 900 s; refresh a little early so a solve never expires
# mid-run and cost us a wasted poll.
_SESSION_TTL = 780.0

_BLN_STORE_RE = re.compile(r"var\s+__blnChallengeStore\s*=\s*(\{.*?\})\s*;", re.S)

_SESSION = None            # type: ignore[var-annotated]  # requests.Session
_SESSION_BORN = 0.0
_SESSION_LOCK = threading.Lock()


def _new_session():
    import requests                     # lazy: a missing wheel = unknown, not a crash
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    return s


def _solve_baleen(session, html: str, timeout: int) -> bool:
    """Set the echoed cookie and ping the check endpoint. True if solved."""
    m = _BLN_STORE_RE.search(html)
    if not m:
        return False
    try:
        store = json.loads(m.group(1))
        cookie = store["cookie"]
        name, value = cookie["name"], cookie["value"]
    except (ValueError, KeyError, TypeError):
        return False

    session.cookies.set(name, value,
                        domain=store.get("domain") or COOKIE_DOMAIN,
                        path=cookie.get("path") or "/")

    # The interstitial's own script pings this before reloading. Not strictly
    # required for the cookie to be honoured, but faithful to the browser and
    # cheap, so it is kept. Its failure must never fail the fetch.
    params = store.get("checkChallengeParams") or {
        "request_fate": "challengejs", "tracking_id": "",
        "bot_category": "unknown", "rule_id": "",
    }
    try:
        session.post("%s?%s=%s" % (BALEEN_CHECK_URL, name, value),
                     data=params, timeout=timeout)
    except Exception:                   # noqa: BLE001 - best effort only
        pass
    return True


def fetch_cdiscount(r: Retailer) -> str:
    """GET a Cdiscount page, solving the Baleen cookie-echo challenge.

    Returns the response BODY even for non-2xx statuses (403 WAF page, 410
    delisting) so ``parse`` can tell a block from a delisting from a product.
    Only genuine transport failures raise ``FetchError``.
    """
    global _SESSION, _SESSION_BORN
    try:
        import requests
    except ImportError as e:
        raise FetchError("requests not installed") from e

    with _SESSION_LOCK:
        if _SESSION is None or (time.time() - _SESSION_BORN) > _SESSION_TTL:
            _SESSION = _new_session()
            _SESSION_BORN = time.time()
        session = _SESSION

    try:
        resp = session.get(r.target, timeout=r.timeout)
        if "__blnChallengeStore" in resp.text:
            if _solve_baleen(session, resp.text, r.timeout):
                resp = session.get(r.target, timeout=r.timeout)
                if "__blnChallengeStore" in resp.text:
                    # Solve was rejected; drop the session so the next SKU in
                    # this run gets a clean one instead of inheriting a burnt
                    # cookie jar.
                    with _SESSION_LOCK:
                        _SESSION, _SESSION_BORN = None, 0.0
        return resp.text
    except requests.RequestException as e:
        with _SESSION_LOCK:
            _SESSION, _SESSION_BORN = None, 0.0
        raise FetchError("%s: %s" % (type(e).__name__, e)) from e
    except Exception as e:              # noqa: BLE001
        raise FetchError("%s: %s" % (type(e).__name__, e)) from e


# ------------------------------------------------------------------ detection

# "Vendu<!-- --> et <!-- -->expédié<!-- --> par <span …>SELLER</span>"
_MARKETPLACE_RE = re.compile(
    r"Vendu(?:<!--\s*-->)?\s*et\s*(?:<!--\s*-->)?\s*exp[ée]di[ée]"
    r"(?:<!--\s*-->)?\s*par\s*(?:<[^>]+>\s*)*([^<]{2,60})", re.I)
# "Vendu<!-- --> par <span …>Cdiscount</span>"
_FIRST_PARTY_RE = re.compile(
    r"Vendu(?:<!--\s*-->)?\s*par\s*(?:<[^>]+>\s*)*Cdiscount", re.I)

_SHIPPING_ANCHOR = 'data-e2e="product-shipping"'
_SHIPPING_WINDOW = 6000     # seller line sits ~1 kB in; 6 kB is ample headroom

_CART_RE = re.compile(
    r'data-e2e="product-add-to-cart"([^>]*)', re.I)
_ARIA_DISABLED_RE = re.compile(r'aria-disabled="(true|false)"', re.I)

# Narrow, Cdiscount-specific block markers. Deliberately NOT looks_blocked():
# see the module docstring — its `challenge-platform` pattern matches every
# healthy Cdiscount page.
_WAF_MARKERS = ("Accès bloqué", "Acc&egrave;s bloqu&eacute;")
_DELISTED_MARKERS = ("Cette page semble introuvable",)


def _blocked(html: str) -> Optional[str]:
    if not html:
        return "réponse vide"
    if "__blnChallengeStore" in html:
        return "défi Baleen non résolu"
    for marker in _WAF_MARKERS:
        if marker in html:
            return "WAF Cloudflare (403 « Accès bloqué »)"
    return None


def _jsonld_price(html: str, sku: str) -> Optional[float]:
    """JSON-LD offers.price. NEVER offers.availability — see docstring."""
    fallback = None
    for obj in iter_jsonld(html):
        if not isinstance(obj, dict):
            continue
        if str(obj.get("@type", "")).lower() != "product":
            continue          # Cdiscount emits lowercase "product"
        offers = obj.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if not isinstance(offers, dict):
            continue
        price = parse_price(offers.get("price"))
        if price is None:
            continue
        if str(obj.get("sku", "")).lower() == sku.lower():
            return price      # exact SKU match wins over "similar products"
        if fallback is None:
            fallback = price
    return fallback


def _parse(html: str, sku: str, max_price: Optional[float]) -> Result:
    blocked = _blocked(html)
    if blocked:
        return Result(STATUS_UNKNOWN, "Cdiscount : %s" % blocked)

    # HTTP 410 delisting. A real determination — the product no longer exists
    # in Cdiscount's catalogue — so out_of_stock, not unknown.
    for marker in _DELISTED_MARKERS:
        if marker in html:
            return Result(STATUS_OUT, "Cdiscount : fiche supprimée (410)")

    has_shipping = _SHIPPING_ANCHOR in html
    has_cart = 'data-e2e="product-add-to-cart"' in html
    if not has_shipping and not has_cart:
        return Result(STATUS_UNKNOWN,
                      "Cdiscount : page non reconnue (%d octets, aucun data-e2e "
                      "produit)" % len(html))

    price = _jsonld_price(html, sku)
    price_txt = "%.2f EUR" % price if price is not None else "prix illisible"

    # --- 1. seller gate, scoped to the shipping panel ----------------------
    idx = html.find(_SHIPPING_ANCHOR)
    scope = html[idx:idx + _SHIPPING_WINDOW] if idx >= 0 else html

    mp = _MARKETPLACE_RE.search(scope)
    if mp:
        seller = re.sub(r"\s+", " ", mp.group(1)).strip()
        if seller.lower() != "cdiscount":
            # Determination, not a failure: a scalper holds the buybox.
            return Result(STATUS_OUT,
                          "Cdiscount : marketplace « %s », %s — ignoré"
                          % (seller, price_txt),
                          price=price, seller=seller)

    if not _FIRST_PARTY_RE.search(scope):
        return Result(STATUS_UNKNOWN,
                      "Cdiscount : vendeur illisible dans product-shipping "
                      "(gabarit changé ?)", price=price)

    # --- 2. buy control ----------------------------------------------------
    btn = _CART_RE.search(html)
    if not btn:
        return Result(STATUS_OUT,
                      "Cdiscount : offre Cdiscount mais aucun bouton panier",
                      price=price, seller="Cdiscount")
    disabled = _ARIA_DISABLED_RE.search(btn.group(1))
    if disabled is None:
        return Result(STATUS_UNKNOWN,
                      "Cdiscount : bouton panier sans aria-disabled "
                      "(gabarit changé ?)", price=price, seller="Cdiscount")
    if disabled.group(1).lower() == "true":
        return Result(STATUS_OUT,
                      "Cdiscount : vendu par Cdiscount mais panier désactivé",
                      price=price, seller="Cdiscount")

    # --- 3. price sanity ---------------------------------------------------
    if price is None:
        return Result(STATUS_UNKNOWN,
                      "Cdiscount : vendu par Cdiscount, panier actif, mais "
                      "prix illisible", seller="Cdiscount")
    if not price_ok(price, max_price):
        return Result(STATUS_OUT,
                      "Cdiscount : vendu par Cdiscount mais prix aberrant %s "
                      "(plafond %.0f EUR) — ignoré" % (price_txt, max_price),
                      price=price, seller="Cdiscount")

    return Result(STATUS_IN,
                  "Cdiscount : vendu par Cdiscount, « Ajouter au panier » "
                  "actif, %s" % price_txt,
                  price=price, seller="Cdiscount")


def make_parser(sku: str, max_price: Optional[float] = DEFAULT_MAX_PRICE
                ) -> Callable[[str], Result]:
    """Bind the SKU and price cap — ``check.py`` calls ``parse(html)`` only."""
    def _parser(html: str) -> Result:
        return _parse(html, sku, max_price)
    _parser.__name__ = "parse_cdiscount_%s" % sku
    return _parser


_BASE = "https://www.cdiscount.com/jeux-pc-video-console/ps5"

# Bundle cap: console + FC26. Lifted by the game's retail price so a genuine
# first-party pack is not filtered out as "absurdly priced".
BUNDLE_MAX_PRICE = DEFAULT_MAX_PRICE + 80.0

RETAILERS: list[Retailer] = [
    Retailer(
        key="cdiscount-ps5prov2",
        name="Cdiscount (PS5 Pro 2 To)",
        url="%s/console-playstation-5-pro/f-1035001-ps5prov2.html" % _BASE,
        fetch=fetch_cdiscount,
        parse=make_parser("ps5prov2"),
        note="Cloudflare + Baleen (cookie-echo) ; marketplace-only au "
             "2026-09-13 (1 599,99 EUR, Jeux Video Du Net)",
    ),
    Retailer(
        key="cdiscount-ps5pro",
        name="Cdiscount (PS5 Pro)",
        url="%s/console-playstation-5-pro/f-1035001-ps5pro.html" % _BASE,
        fetch=fetch_cdiscount,
        parse=make_parser("ps5pro"),
        note="Cloudflare + Baleen (cookie-echo) ; marketplace-only au "
             "2026-09-13 (1 299,00 EUR, CDISTRIB2)",
    ),
    Retailer(
        key="cdiscount-hbps5profc26",
        name="Cdiscount (Pack PS5 Pro + FC26)",
        url="%s/pack-ps5-pro-console-playstation-5-pro-fc26-c/"
            "f-1035001-hbps5profc26.html" % _BASE,
        fetch=fetch_cdiscount,
        # The cap is bound into the closure too: check.py calls parse(html)
        # with no access to Retailer.max_price. Kept on the Retailer as well so
        # logs and any future caller see the same number.
        parse=make_parser("hbps5profc26", BUNDLE_MAX_PRICE),
        max_price=BUNDLE_MAX_PRICE,
        note="Pack FC26 — plafond relevé à %.0f EUR ; marketplace-only au "
             "2026-09-13 (1 699,99 EUR, Jeux Video Du Net)" % BUNDLE_MAX_PRICE,
    ),
]


if __name__ == "__main__":
    import os
    import sys

    SCRATCH = ("/private/tmp/claude-501/-Users-youssefkabbaj-Documents-"
               "Extension-PS5-Pro-ps5pro-email-watcher/"
               "e88af860-a8ea-4515-957a-88c3328377cb/scratchpad")

    def show(label: str, res: Result) -> None:
        print("%-34s %-13s %s | prix=%s vendeur=%s"
              % (label, res.status, res.note, res.price, res.seller))

    def load(name: str) -> str:
        with open(os.path.join(SCRATCH, name), encoding="utf-8",
                  errors="replace") as fh:
            return fh.read()

    print("=== OFFLINE (HTML capturé 2026-09-13) " + "=" * 34)
    fixtures = [
        ("cd_v2.html",              "ps5prov2",       STATUS_OUT),
        ("cdx_ps5pro.html",         "ps5pro",         STATUS_OUT),
        ("cdx_hbps5profc26.html",   "hbps5profc26",   STATUS_OUT),
        # POSITIVE CONTROL: PS5 Standard genuinely sold by Cdiscount, 649 EUR.
        # Proves the in_stock branch actually fires.
        ("cdx_ps5stdchassise.html", "ps5stdchassise", STATUS_IN),
        ("cd_p2.html",              "ps3619995009461", STATUS_OUT),  # HTTP 410
        ("bare.html",               "ps5pro",         STATUS_UNKNOWN),  # WAF 403
    ]
    failures = []
    for name, sku, expected in fixtures:
        cap = BUNDLE_MAX_PRICE if sku == "hbps5profc26" else DEFAULT_MAX_PRICE
        try:
            res = _parse(load(name), sku, cap)
        except FileNotFoundError:
            print("%-34s FIXTURE MANQUANTE" % name)
            continue
        ok = "ok " if res.status == expected else "FAIL"
        if res.status != expected:
            failures.append("%s: attendu %s, obtenu %s" % (name, expected, res.status))
        show("[%s] %s" % (ok, name), res)

    print()
    print("=== LIVE " + "=" * 62)
    for r in RETAILERS:
        try:
            res = r.parse(fetch_cdiscount(r))
        except Exception as exc:                     # noqa: BLE001
            res = Result(STATUS_UNKNOWN, "fetch KO: %s" % exc)
        show(r.key, res)

    if failures:
        print("\n!! ÉCHECS OFFLINE: " + "; ".join(failures))
        sys.exit(1)
    print("\nToutes les assertions offline passent.")
