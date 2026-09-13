"""Per-retailer persisted state (.state/status.json), committed back by the workflow.

Schema v2 keys state by retailer so one site breaking can never mask another's
signal. v1 was a single flat document describing PS Direct only; `load()`
migrates it in place on first run so the existing out_of_stock baseline is
preserved and we don't fire a spurious "restock" on the first v2 run.
"""
from __future__ import annotations

import json
import pathlib
import time
from typing import Any, Dict

STATE_PATH = pathlib.Path(".state/status.json")
SCHEMA_VERSION = 2

# A retailer that has been unknown this many consecutive runs is presumed
# blocked rather than merely flaky (~15 min at a 5 min cadence).
CANARY_AFTER_UNKNOWNS = 3
CANARY_COOLDOWN = 6 * 3600
# After this long with no usable read at all, stop hammering a wall.
QUARANTINE_AFTER = 18 * 3600
QUARANTINE_FOR = 30 * 60


def _blank(key: str) -> Dict[str, Any]:
    return {
        "key": key,
        "status": None,
        "note": "",
        "price": None,
        "seller": None,
        "last_check_at": 0,
        "last_ok_at": 0,          # last run that produced in_stock/out_of_stock
        "last_notified_at": 0,
        "last_canary_at": 0,
        "consecutive_unknown": 0,
        "quarantined_until": 0,
    }


def _migrate_v1(old: Dict[str, Any]) -> Dict[str, Any]:
    """v1 was PS Direct only, flat. Fold it into retailers.psdirect."""
    rec = _blank("psdirect")
    rec.update({
        "status": old.get("status"),
        "note": old.get("note", ""),
        "last_check_at": int(old.get("last_check_at") or 0),
        "last_notified_at": int(old.get("last_notified_at") or 0),
        "last_canary_at": int(old.get("last_canary_at") or 0),
    })
    if rec["status"] in ("in_stock", "out_of_stock"):
        rec["last_ok_at"] = rec["last_check_at"]
    return {"version": SCHEMA_VERSION, "retailers": {"psdirect": rec},
            "migrated_from_v1_at": int(time.time())}


def load() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"version": SCHEMA_VERSION, "retailers": {}}
    try:
        doc = json.loads(STATE_PATH.read_text())
    except Exception:                             # noqa: BLE001 - corrupt file
        return {"version": SCHEMA_VERSION, "retailers": {}}
    if doc.get("version") != SCHEMA_VERSION:
        return _migrate_v1(doc)
    doc.setdefault("retailers", {})
    return doc


def save(doc: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(doc, indent=2, ensure_ascii=False,
                                     sort_keys=True) + "\n")


def record(doc: Dict[str, Any], key: str) -> Dict[str, Any]:
    return doc.setdefault("retailers", {}).setdefault(key, _blank(key))


def is_quarantined(rec: Dict[str, Any], now: int) -> bool:
    return now < int(rec.get("quarantined_until") or 0)
