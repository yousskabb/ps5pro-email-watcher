"""Shared contract every retailer adapter builds against.

An adapter module exposes a module-level ``RETAILERS: list[Retailer]``. Each
Retailer pairs a fetch strategy with a parse function; ``check.py`` walks the
registry, calls ``fetch`` then ``parse``, and diffs the Result against state.

Design rules that adapters MUST honour
--------------------------------------
1. A blocked / challenged / unrecognised page is ``unknown`` — NEVER
   ``out_of_stock``. Recording a block as out-of-stock manufactures a phantom
   ``out_of_stock -> in_stock`` edge when the site recovers, i.e. a 3am false
   alarm. This is the single most important rule in the codebase.
2. ``in_stock`` means "the RETAILER ITSELF is selling it at a sane price",
   never "some marketplace seller has one". Filter on seller identity first;
   price is defence in depth. Observed scalper listings during research:
   2 215 EUR (Auchan/2KINGS), 2 786 EUR (Carrefour/Shopelya), 1 699 EUR
   (Cdiscount), 1 074 EUR (Darty/ASD) — all reporting "InStock".
3. Never select on hashed CSS class names (``sc-bkkiih``, ``f-xyz123``). They
   are build artifacts and change on every deploy. Use data attributes,
   embedded JSON, microdata or stable French copy.
4. Optional third-party deps (curl_cffi, requests) are imported lazily inside
   the fetch helpers, so a missing wheel degrades one retailer to ``unknown``
   instead of crashing the whole run.
"""
from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

# --------------------------------------------------------------------- consts

STATUS_IN = "in_stock"
STATUS_OUT = "out_of_stock"
STATUS_UNKNOWN = "unknown"
STATUS_QUEUED = "queued"  # waiting-room detected: on Cultura this IS the drop

VALID_STATUSES = {STATUS_IN, STATUS_OUT, STATUS_UNKNOWN, STATUS_QUEUED}

UA_CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Cdiscount hard-403s a bare UA; the full set is mandatory, not cosmetic.
BROWSER_HEADERS = {
    "User-Agent": UA_CHROME,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

DEFAULT_MAX_PRICE = 1000.0  # PS5 Pro MSRP 899,99 EUR + bundle headroom


class FetchError(Exception):
    """Transport-level failure. check.py turns this into `unknown`."""


# ---------------------------------------------------------------- data models

@dataclass
class Result:
    """What an adapter returns. `note` is quoted verbatim into the alert."""
    status: str
    note: str
    price: Optional[float] = None
    seller: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError("bad status %r" % (self.status,))


@dataclass(frozen=True)
class Retailer:
    key: str                      # stable state key, e.g. "ldlc"
    name: str                     # display name in the alert, e.g. "LDLC"
    url: str                      # human link — what you tap to buy
    parse: Callable[[str], Result]
    fetch_url: str = ""           # what we actually GET (defaults to `url`)
    fetch: Optional[Callable[["Retailer"], str]] = None   # None -> fetch_plain
    enabled: bool = True
    max_price: Optional[float] = DEFAULT_MAX_PRICE
    timeout: int = 25
    note: str = ""                # free-text caveat surfaced in logs

    @property
    def target(self) -> str:
        return self.fetch_url or self.url


# -------------------------------------------------------------- fetch helpers

def fetch_plain(r: Retailer) -> str:
    """stdlib urllib. Works on: PS Direct API, LDLC group, Auchan, Boulanger."""
    req = urllib.request.Request(r.target, headers=BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=r.timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:                      # noqa: BLE001 - reported as unknown
        raise FetchError("%s: %s" % (type(e).__name__, e)) from e


def fetch_http11(r: Retailer) -> str:
    """requests speaks HTTP/1.1 by default.

    Carrefour's Cloudflare returns 403 over HTTP/2 to any client, including a
    full browser header set, but 200 over HTTP/1.1 — the discriminator is the
    protocol fingerprint, not the headers.
    """
    try:
        import requests
    except ImportError as e:
        raise FetchError("requests not installed") from e
    try:
        resp = requests.get(r.target, headers=BROWSER_HEADERS, timeout=r.timeout)
        return resp.text
    except Exception as e:                      # noqa: BLE001
        raise FetchError("%s: %s" % (type(e).__name__, e)) from e


def fetch_impersonate(r: Retailer, profile: str = "firefox133") -> str:
    """curl_cffi with a real browser's TLS/HTTP2 fingerprint.

    Required by Fnac and Darty (shared Akamai + DataDome edge). firefox133 is
    the only profile verified to work on BOTH domains; every chrome*/edge*
    profile is rejected.
    """
    try:
        from curl_cffi import requests as cffi
    except ImportError as e:
        raise FetchError("curl_cffi not installed") from e
    try:
        resp = cffi.get(r.target, impersonate=profile, timeout=r.timeout,
                        headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"})
        return resp.text
    except Exception as e:                      # noqa: BLE001
        raise FetchError("%s: %s" % (type(e).__name__, e)) from e


# ------------------------------------------------------------ shared detection

_BLOCK_PATTERNS = (
    (r"_Incapsula_Resource|Incapsula incident ID", "Imperva/Incapsula"),
    (r"geo\.captcha-delivery|captcha-delivery\.com|x-datadome", "DataDome"),
    (r"FNAC DARTY - Maintenance", "Akamai (Fnac-Darty edge)"),
    # NB: bare "challenge-platform" is NOT a block signal — Cloudflare
    # injects that beacon script into healthy pages too.
    (r"_cf_chl_opt|cf-mitigated", "Cloudflare challenge"),
    (r"Just a moment", "Cloudflare"),
    (r"__blnChallengeStore", "Baleen challenge unsolved"),
    (r"Acc&egrave;s bloqu&eacute;|Accès bloqué", "Cloudflare WAF 403"),
    (r"Reference #|Access Denied|You don't have permission", "Akamai/edge deny"),
    (r"Queue-it|waiting room|File d'attente", "waiting room"),
    (r"Saisissez les caract|/errors/validateCaptcha", "Amazon captcha"),
)


def looks_blocked(html: str, min_size: int = 20000) -> Optional[str]:
    """Return a human note if this looks like a block/challenge, else None.

    Checked BEFORE parsing in every adapter. Note the size test is separate:
    Amazon soft-blocks with HTTP 200 and a zero-byte body, which a marker
    scan alone would miss.
    """
    if not html:
        return "réponse vide"
    for pattern, vendor in _BLOCK_PATTERNS:
        if re.search(pattern, html, re.I):
            return "page bloquée (%s)" % vendor
    if len(html) < min_size:
        return "réponse trop petite (%d octets) — interstitiel ?" % len(html)
    return None


def iter_jsonld(html: str):
    """Yield each parsed <script type="application/ld+json"> block.

    Several of these sites emit slightly invalid JSON, so each block is
    isolated in its own try/except rather than failing the whole page.
    """
    for m in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.S | re.I,
    ):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:                        # noqa: BLE001
            continue
        yield from (data if isinstance(data, list) else [data])


def jsonld_product_offer(html: str) -> Optional[dict]:
    """First `offers` dict of the first JSON-LD Product block."""
    for obj in iter_jsonld(html):
        # Case-insensitive: Cdiscount emits "@type":"product" (lowercase).
        if not isinstance(obj, dict) or str(obj.get("@type", "")).lower() != "product":
            continue
        offers = obj.get("offers")
        if isinstance(offers, list):
            return offers[0] if offers else None
        if isinstance(offers, dict):
            return offers
    return None


def availability_to_status(value: str) -> str:
    """schema.org availability -> our status vocabulary.

    CAUTION: on some sites this field is decorative. Fnac ships
    `LimitedAvailability` on a sold-out console and Cdiscount reports
    `InStock` for every listing including scalpers. Only trust it where an
    adapter's research proved it tracks reality (LDLC group, Boulanger).
    """
    v = (value or "").rsplit("/", 1)[-1].lower()
    if v in ("instock", "limitedavailability", "onlineonly", "instoreonly"):
        return STATUS_IN
    if v in ("outofstock", "soldout", "discontinued", "backorder", "preorder"):
        return STATUS_OUT
    return STATUS_UNKNOWN


def parse_price(text: str) -> Optional[float]:
    """'1 599 €99' / '899,99€' / '899.99' -> float."""
    if text is None:
        return None
    t = str(text).replace(" ", "").replace("\xa0", "").replace(" ", "")
    m = re.search(r"(\d+(?:[.,]\d+)?)", t.replace(",", "."))
    return float(m.group(1)) if m else None


def price_ok(price: Optional[float], max_price: Optional[float]) -> bool:
    """Missing price never blocks an alert; an absurd one always does."""
    if price is None or max_price is None:
        return True
    return price <= max_price
