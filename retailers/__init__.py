"""Adapter registry.

Each adapter module exposes ``RETAILERS: list[Retailer]``. Modules are imported
defensively: a broken or dependency-missing adapter is skipped with a loud log
line instead of taking the whole run down, so one retailer's breakage can never
blind you to the other nine.
"""
from __future__ import annotations

import importlib
from typing import List

from .base import Retailer

# Order matters: fastest / most likely to actually get French allocation first.
ADAPTER_MODULES = (
    "psdirect",     # Sony first-party — JSON API, no anti-bot
    "ldlcgroup",    # LDLC + Materiel.net + Rue du Commerce — no anti-bot
    "boulanger",    # Akamai, currently passing
    "cultura",      # Cloudflare, currently passing; waiting room = drop signal
    "auchan",       # no anti-bot, but buybox is usually a marketplace scalper
    "fnacdarty",    # Akamai + DataDome — needs curl_cffi TLS impersonation
    "cdiscount",    # Cloudflare + Baleen cookie-echo challenge
    "carrefour",    # Cloudflare — needs HTTP/1.1
    "amazon",       # disabled by default; use a free Keepa watch instead
)

LOAD_ERRORS: List[str] = []


def all_retailers() -> List[Retailer]:
    found: List[Retailer] = []
    for name in ADAPTER_MODULES:
        try:
            mod = importlib.import_module(f"{__name__}.{name}")
        except Exception as e:                    # noqa: BLE001
            msg = f"{name}: {type(e).__name__}: {e}"
            LOAD_ERRORS.append(msg)
            print(f"!! adapter import failed — {msg}")
            continue
        entries = getattr(mod, "RETAILERS", None)
        if not entries:
            LOAD_ERRORS.append(f"{name}: no RETAILERS")
            print(f"!! adapter {name} exposes no RETAILERS")
            continue
        found.extend(entries)
    return found


def enabled_retailers() -> List[Retailer]:
    return [r for r in all_retailers() if r.enabled]
