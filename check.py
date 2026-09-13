"""PS5 Pro multi-retailer stock watcher.

Walks the adapter registry, fetches each retailer, diffs the result against
per-retailer state, and fires one batched notification for everything that
just became buyable.

Invariants
----------
* An alert means "the RETAILER ITSELF is selling a PS5 Pro at a sane price".
  Never "some marketplace seller has one at 2 215 EUR".
* A blocked / challenged / unparseable page is `unknown`, never `out_of_stock`.
  Recording a block as out-of-stock would manufacture a phantom
  out_of_stock -> in_stock edge when the site recovers, i.e. a 3am false alarm.
* One retailer breaking must never mask another's signal, so every fetch,
  parse and notification is individually isolated.

Triggered by cron-job.org hitting workflow_dispatch every 5 minutes.
"""
from __future__ import annotations

import os
import random
import sys
import time
import traceback
from typing import List, Tuple

import notify
import state
from retailers import LOAD_ERRORS, enabled_retailers
from retailers.base import (STATUS_IN, STATUS_OUT, STATUS_QUEUED,
                            STATUS_UNKNOWN, FetchError, Result, Retailer,
                            price_ok)

# Re-notify hourly while a retailer stays in stock, so a long-lived listing
# doesn't go silent — but per retailer, so a stale Boulanger listing can't
# suppress a fresh LDLC signal.
KEEPALIVE_SECONDS = 60 * 60
QUEUE_COOLDOWN = 30 * 60
# Three dispatchers (cron-job.org, the schedule: block, manual) can overlap.
MIN_SECONDS_BETWEEN_RUNS = 45
# Look human: don't hit ten French retailers simultaneously on the minute.
STAGGER_MIN, STAGGER_MAX = 1.5, 4.0
START_JITTER_MAX = 20


def run_one(r: Retailer) -> Result:
    """Fetch + parse one retailer. Any failure becomes `unknown`, never OOS."""
    fetch = r.fetch or __import__("retailers.base", fromlist=["x"]).fetch_plain
    try:
        payload = fetch(r)
    except FetchError as e:
        return Result(STATUS_UNKNOWN, f"fetch échoué : {e}")
    except Exception as e:                        # noqa: BLE001
        return Result(STATUS_UNKNOWN, f"fetch échoué : {type(e).__name__}: {e}")
    try:
        res = r.parse(payload)
    except Exception as e:                        # noqa: BLE001
        traceback.print_exc()
        return Result(STATUS_UNKNOWN, f"parse cassé : {type(e).__name__}: {e}")

    # Defence in depth: an adapter bug must not be able to page us for a
    # scalper. The seller gate lives in the adapter; the price gate is here too.
    if res.status == STATUS_IN and not price_ok(res.price, r.max_price):
        return Result(STATUS_OUT,
                      f"prix aberrant {res.price} EUR > {r.max_price} — rejeté",
                      price=res.price, seller=res.seller)
    return res


def main() -> int:
    now = int(time.time())
    doc = state.load()

    last_global = max((int(v.get("last_check_at") or 0)
                       for v in doc.get("retailers", {}).values()), default=0)
    if now - last_global < MIN_SECONDS_BETWEEN_RUNS and not os.environ.get("FORCE"):
        print(f"skip: dernier run il y a {now - last_global}s "
              f"(< {MIN_SECONDS_BETWEEN_RUNS}s) — dispatchers qui se chevauchent")
        return 0

    retailers = enabled_retailers()
    if not retailers:
        print("!! aucun adaptateur chargé", file=sys.stderr)
        return 1
    print(f"{len(retailers)} enseignes actives")

    if not os.environ.get("NO_JITTER"):
        time.sleep(random.uniform(0, START_JITTER_MAX))

    in_stock: List[Tuple[Retailer, Result]] = []
    queued: List[Tuple[Retailer, Result]] = []
    canaries: List[str] = []

    for i, r in enumerate(retailers):
        rec = state.record(doc, r.key)
        now = int(time.time())

        if state.is_quarantined(rec, now):
            left = (int(rec["quarantined_until"]) - now) // 60
            print(f"[{r.key}] en quarantaine encore {left} min — skip")
            continue
        if i:
            time.sleep(random.uniform(STAGGER_MIN, STAGGER_MAX))

        res = run_one(r)
        prev_status = rec.get("status")
        print(f"[{r.key}] {prev_status!r} -> {res.status!r} : {res.note}")

        rec.update({"status": res.status, "note": res.note,
                    "price": res.price, "seller": res.seller,
                    "last_check_at": now})

        if res.status in (STATUS_IN, STATUS_OUT):
            rec["last_ok_at"] = now
            rec["consecutive_unknown"] = 0
            rec["quarantined_until"] = 0
            if res.status == STATUS_IN:
                notified = int(rec.get("last_notified_at") or 0)
                if prev_status != STATUS_IN:
                    in_stock.append((r, res))
                elif now - notified >= KEEPALIVE_SECONDS:
                    in_stock.append((r, res))

        elif res.status == STATUS_QUEUED:
            # On Cultura and Boulanger a waiting room appearing IS the drop.
            rec["last_ok_at"] = now
            rec["consecutive_unknown"] = 0
            if now - int(rec.get("last_notified_at") or 0) >= QUEUE_COOLDOWN:
                queued.append((r, res))

        else:  # STATUS_UNKNOWN
            rec["consecutive_unknown"] = int(rec.get("consecutive_unknown") or 0) + 1
            blind_for = now - int(rec.get("last_ok_at") or now)
            if (rec["consecutive_unknown"] >= state.CANARY_AFTER_UNKNOWNS
                    and now - int(rec.get("last_canary_at") or 0) >= state.CANARY_COOLDOWN):
                if blind_for > state.QUARANTINE_AFTER:
                    canaries.append(f"{r.name} : bloqué depuis {blind_for // 3600} h "
                                    f"— vérifie à la main ({res.note})")
                    rec["quarantined_until"] = now + state.QUARANTINE_FOR
                else:
                    canaries.append(f"{r.name} : {res.note} "
                                    f"({rec['consecutive_unknown']} échecs d'affilée)")
                rec["last_canary_at"] = now

    now = int(time.time())

    if in_stock or queued:
        names = [r.name for r, _ in in_stock] + [f"{r.name} (file)" for r, _ in queued]
        subject = "🎮 PS5 Pro EN STOCK — " + ", ".join(names)
        lines = []
        for r, res in in_stock:
            price = f"{res.price:.2f} €" if res.price else "prix ?"
            lines.append(f"✅ {r.name} — {price}\n   {res.note}\n   {r.url}")
        for r, res in queued:
            lines.append(f"⏳ {r.name} — file d'attente ouverte (= drop en cours)\n"
                         f"   {res.note}\n   {r.url}")
        body = "\n\n".join(lines) + "\n\nFonce. Sois déjà connecté à ton compte."
        first_url = (in_stock or queued)[0][0].url
        if notify.notify(subject, body, url=first_url, urgent=True):
            for r, _ in in_stock + queued:
                state.record(doc, r.key)["last_notified_at"] = now

    if canaries:
        body = ("Le watcher ne voit plus ces enseignes :\n\n  - "
                + "\n  - ".join(canaries)
                + "\n\nCe n'est PAS une rupture de stock : la détection est aveugle.")
        notify.notify("⚠️ PS5 Pro watcher — détection aveugle", body, urgent=False)

    if LOAD_ERRORS:
        print("!! adaptateurs non chargés : " + "; ".join(LOAD_ERRORS), file=sys.stderr)

    state.save(doc)
    print(f"état sauvegardé — {len(in_stock)} en stock, {len(queued)} en file, "
          f"{len(canaries)} canaris")
    return 0


if __name__ == "__main__":
    sys.exit(main())
