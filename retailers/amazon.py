"""Amazon.fr — PS5 Pro ASINs B0FRYBZR91 and B0DHSV68YZ.

BOTH ENTRIES SHIP ``enabled=False``. Reasons, all verified during research:

* ~2 MB of HTML per poll per ASIN, every 5 minutes — by far the heaviest
  retailer in the registry, for the weakest signal.
* There is no light endpoint left: ``/gp/aod/ajax/`` (the old offer-listing
  fragment) now returns 404, and PA-API 5.0 answers 403 AccessDeniedException
  because the Product Advertising API is deprecated for new/dormant callers.
* Amazon soft-blocks scrapers with **HTTP 200 + a zero-byte or tiny body**, so a
  naive parser silently flips to "out of stock" on every block — exactly the
  phantom-restock failure base.py rule 1 exists to prevent.
* Polling product pages violates Amazon's Conditions of Use.

Fetch strategy: plain ``fetch_plain`` (base.py's BROWSER_HEADERS), which is how
the 2 MB evidence pages were captured and which still returns the full PDP live
(verified 2026-09-13). Do NOT route these entries through curl_cffi: fetched
with the firefox133 impersonation profile, Amazon.fr answers HTTP 200 with a
REDUCED ~317 KB page — real product title, one ``add-to-cart-button``, but ZERO
``data-csa-c-asin`` attributes, i.e. no buy-box widgets at all. Seller identity
is unreadable there, so the adapter reports ``unknown`` (correct, but blind).

Recommended route instead: a free Keepa watch on the **"Amazon" price series**
(not "New" / "Buy Box"), which fires only on a first-party offer appearing.
The adapter is here anyway so it can be switched on by flipping ``enabled``.

Detection — in_stock requires ALL of:

1. ``id="add-to-cart-button"`` AND ``id="submit.add-to-cart"`` present;
2. no *rendered* ``unqualifiedBuyBox_feature_div`` bound to this ASIN (both PS5
   Pro ASINs currently render one, with empty ``offerListingID``/``merchantID``
   = no first-party offer, marketplace only). NB the string
   ``unqualifiedBuyBox_feature_div`` ALSO appears in the page's feature manifest
   (``{"dtu":"unqualifiedBuyBox_feature_div",...}``) on genuinely in-stock pages,
   so the test must be on ``id="…"`` + ``data-csa-c-asin``, never on the bare string;
3. seller is Amazon — read from ``offer-display-feature-name="desktop-merchant-info"``
   inside the ASIN's ``merchantInfoFeature_feature_div``, or from the hidden
   ``merchantID`` input (``A1X6FK5RDHNB96`` = Amazon.fr). ``#merchant-info`` is
   empty on modern Amazon.fr and is obsolete — it is not used;
4. a price read from ``corePrice_feature_div`` scoped to the ASIN.

Traps honoured:

* Sponsored tiles and "frequently bought together" embed OTHER ASINs' sellers
  and availability inline (e.g. a ``"merchant":{"merchantName":"KIWIHOME Direct- FR"},
  "availability":{"status":"IN_STOCK"}`` block that belongs to a different ASIN —
  the KIWIHOME ad is present in the captured B0FRYBZR91 page). Every extraction
  below is anchored on ``data-csa-c-asin="<ASIN>"``.
* ``Actuellement indisponible`` appears TWICE on a page that is genuinely IN
  STOCK: it is an i18n string table (``"currentlyUnavailableMessage"``,
  ``"currentlyUnavailablePopOverStringValue"``). Never bare-grep it; we only read
  it inside the ASIN-scoped ``#availability`` block.
* HTTP 200 + tiny body is a SILENT soft block => ``unknown``, never
  ``out_of_stock`` (``MIN_PAGE`` below).
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from .base import (STATUS_IN, STATUS_OUT, STATUS_UNKNOWN, Result, Retailer,
                   looks_blocked, parse_price)

ASINS = ("B0FRYBZR91", "B0DHSV68YZ")
AMAZON_FR_MERCHANT_ID = "A1X6FK5RDHNB96"
FIRST_PARTY_NAMES = {"amazon", "amazon.fr", "amazon eu sarl"}

# A real Amazon.fr PDP is ~2 MB. Anything under this is an interstitial, a soft
# block, or a truncated read — all of which must read as `unknown`.
MIN_PAGE = 200_000


def _text(fragment: str) -> str:
    s = re.sub(r"<script.*?</script>", " ", fragment, flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", s)).strip()


def _asin_block(html: str, feature_id: str, asin: str, size: int = 30_000) -> Optional[str]:
    """Slice of the feature div with this id AND bound to this ASIN.

    The id and the ASIN must live in the SAME tag, which is what keeps a
    sponsored carousel's identically-named widget from being read.
    """
    for m in re.finditer(r'id="%s"([^>]*)>' % re.escape(feature_id), html):
        if 'data-csa-c-asin="%s"' % asin in m.group(1):
            return html[m.end():m.end() + size]
    return None


def _seller(html: str, asin: str) -> Optional[str]:
    seg = _asin_block(html, "merchantInfoFeature_feature_div", asin, 6_000)
    if seg:
        m = re.search(
            r'class="offer-display-feature-text[^"]*"[^>]*'
            r'offer-display-feature-name="desktop-merchant-info"[^>]*>(.{0,600}?)</div>\s*</div>',
            seg, re.S)
        if not m:
            m = re.search(r'offer-display-feature-text-message"[^>]*>\s*([^<]{1,60})', seg)
        if m:
            name = _text(m.group(1))
            if name:
                return name
    # Fallback: the hidden buybox form. Empty value => no featured offer.
    m = re.search(r'id="merchantID"[^>]*value="([^"]*)"', html) or \
        re.search(r'name="merchantID"[^>]*value="([^"]*)"', html)
    if m and m.group(1):
        return "Amazon" if m.group(1) == AMAZON_FR_MERCHANT_ID else "merchant:" + m.group(1)
    return None


def _price(html: str, asin: str) -> Optional[float]:
    seg = _asin_block(html, "corePrice_feature_div", asin, 4_000)
    if not seg:
        return None
    m = re.search(r'class="a-offscreen">\s*([0-9][0-9\s  .,]*)\s*€', seg)
    return parse_price(m.group(1)) if m else None


def make_parser(asin: str) -> Callable[[str], Result]:
    """One parse function per ASIN (Retailer.parse takes only the HTML)."""

    def parse(html: str) -> Result:
        blocked = looks_blocked(html, min_size=MIN_PAGE)
        if blocked:
            return Result(STATUS_UNKNOWN, blocked)
        if 'data-csa-c-asin="%s"' % asin not in html:
            return Result(STATUS_UNKNOWN,
                          "page sans bloc lié à l'ASIN %s (redirection ?)" % asin)

        price = _price(html, asin)
        seller = _seller(html, asin)

        # 1/2. No featured offer at all -> marketplace-only, Amazon isn't selling.
        if _asin_block(html, "unqualifiedBuyBox_feature_div", asin, 200) is not None:
            return Result(STATUS_OUT, "aucune offre en vedette (marketplace seulement)",
                          price=price, seller=seller)
        if not ('id="add-to-cart-button"' in html and 'id="submit.add-to-cart"' in html):
            return Result(STATUS_OUT, "pas de bouton d'ajout au panier",
                          price=price, seller=seller)

        # ASIN-scoped availability sentence (never a bare grep — the i18n table
        # ships "Actuellement indisponible" on in-stock pages too).
        seg = _asin_block(html, "availability", asin, 1_200)
        avail = _text(seg)[:120] if seg else ""
        if re.search(r"actuellement indisponible|temporairement en rupture", avail, re.I):
            return Result(STATUS_OUT, avail or "indisponible", price=price, seller=seller)

        # 3. Seller gate.
        if not seller:
            return Result(STATUS_UNKNOWN, "vendeur illisible dans la buybox", price=price)
        if seller.strip().lower() not in FIRST_PARTY_NAMES:
            return Result(STATUS_OUT, "buybox tenue par %s — pas Amazon" % seller,
                          price=price, seller=seller)

        # 4. Price. base.py: a missing price never blocks an alert (check.py
        # re-applies price_ok as defence in depth), so an unreadable price is
        # reported in the note rather than turned into a miss.
        if price is None:
            return Result(STATUS_IN, "vendu et expédié par Amazon (prix illisible)",
                          seller=seller)
        return Result(STATUS_IN, "vendu et expédié par Amazon — %.2f EUR" % price,
                      price=price, seller=seller)

    parse.__name__ = "parse_amazon_%s" % asin
    return parse


# -------------------------------------------------------------------- registry
# enabled=False: ~2 Mo par sondage, pas d'endpoint léger (/gp/aod/ajax/ = 404),
# soft-block en HTTP 200 + corps vide, contraire aux CGU Amazon, et PA-API 5.0
# dépréciée (403 AccessDeniedException). Alternative recommandée : une alerte
# Keepa gratuite sur la série de prix « Amazon ». Passer enabled=True pour
# activer malgré tout.
RETAILERS = [
    Retailer(
        key="amazon_%s" % asin.lower(),
        name="Amazon.fr (%s)" % asin,
        url="https://www.amazon.fr/dp/%s" % asin,
        parse=make_parser(asin),
        enabled=False,
        note="désactivé : 2 Mo/sondage, CGU Amazon, soft-block HTTP 200 ; "
             "préférer une alerte Keepa sur la série « Amazon »",
    )
    for asin in ASINS
]


# ------------------------------------------------------------------ validation

if __name__ == "__main__":                                        # pragma: no cover
    import os
    import sys

    HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(HERE))
    from retailers.base import BROWSER_HEADERS, fetch_plain       # noqa: E402

    def live(r: Retailer) -> str:
        """fetch_plain, with a curl fallback for Macs whose Python has no CA
        bundle (SSLCertVerificationError) — same headers either way."""
        try:
            return fetch_plain(r)
        except Exception:                                         # noqa: BLE001
            import subprocess
            cmd = ["curl", "-sS", "--compressed", "-L", "--max-time", "40"]
            for k, v in BROWSER_HEADERS.items():
                cmd += ["-H", "%s: %s" % (k, v)]
            return subprocess.run(cmd + [r.target], capture_output=True,
                                  text=True, timeout=60).stdout


    FIX = os.environ.get("FIXTURES", "/private/tmp/claude-501/"
                         "-Users-youssefkabbaj-Documents-Extension-PS5-Pro-"
                         "ps5pro-email-watcher/e88af860-a8ea-4515-957a-88c3328377cb/"
                         "scratchpad")

    CASES = [
        ("amz_B0FRYBZR91.html", "B0FRYBZR91", STATUS_OUT, "PS5 Pro ASIN 1"),
        ("amz_B0DHSV68YZ.html", "B0DHSV68YZ", STATUS_OUT, "PS5 Pro ASIN 2"),
        # Témoin positif : ASIN vendu par Amazon (B0FX2VBNMF), capture amz_POSCTRL.
        ("amz_POSCTRL.html",    "B0FX2VBNMF", STATUS_IN,  "témoin vendu par Amazon"),
    ]

    print("=== hors-ligne (HTML capturés) " + "=" * 40)
    bad = 0
    for fname, asin, want, label in CASES:
        path = os.path.join(FIX, fname)
        if not os.path.exists(path):
            print("  SKIP %-22s fichier absent" % fname)
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            res = make_parser(asin)(fh.read())
        ok = res.status == want
        bad += not ok
        print("  %s %-22s %-12s attendu=%-12s price=%-8s seller=%-10s %s"
              % ("OK " if ok else "ECHEC", fname, res.status, want, res.price,
                 (res.seller or "")[:10], res.note))
        print("      %s" % label)

    # Soft-block / captcha / vide -> unknown, jamais out_of_stock.
    p = make_parser("B0FRYBZR91")
    for label, payload in (
        ("corps vide (HTTP 200)", ""),
        ("corps tronqué 1 Ko", "<html>" + "x" * 1000 + "</html>"),
        ("captcha Amazon", "<html>Saisissez les caractères" + "x" * 300_000 + "</html>"),
        ("page 403 générique", "<html>Access Denied" + "x" * 300_000 + "</html>"),
        ("HTML volumineux sans ASIN", "<html>" + "x" * 300_000 + "</html>"),
    ):
        res = p(payload)
        ok = res.status == STATUS_UNKNOWN
        bad += not ok
        print("  %s %-22s %-12s attendu=%-12s %s"
              % ("OK " if ok else "ECHEC", label, res.status, STATUS_UNKNOWN, res.note))

    print("=== en direct " + "=" * 56)
    if os.environ.get("LIVE_AMAZON"):
        for r in RETAILERS:
            try:
                html = live(r)
                res = r.parse(html)
                print("  %-24s %-12s %9d octets  price=%-8s %s"
                      % (r.key, res.status, len(html), res.price, res.note))
            except Exception as e:                                # noqa: BLE001
                print("  %-24s FETCH KO   %s: %s" % (r.key, type(e).__name__, e))
    else:
        print("  ignoré (enabled=False, CGU Amazon) — LIVE_AMAZON=1 pour forcer")

    sys.exit(1 if bad else 0)
