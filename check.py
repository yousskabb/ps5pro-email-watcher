"""
Fetch the PS5 Pro page on PlayStation Direct France, decide whether the
console is buyable, and email me on transitions to in-stock.

Detection rule (verified 2025-09-07 on 1000050720-FR):
  On the product page there are <button data-product-code="1000050720-FR" ...>
  entries. The buyable ones carry the class "add-to-cart". When the console is
  out of stock, every add-to-cart button ALSO carries the class "hide". As soon
  as at least one add-to-cart button loses that "hide" class, the product is
  buyable and the page also stops showing "Actuellement Indisponible" in the
  buy box.

State is persisted to .state/status.json committed back to the repo by the
workflow, so we only email on the OUT_OF_STOCK -> IN_STOCK edge (and once per
hour while still in stock, as a keep-alive).
"""
from __future__ import annotations
import json
import os
import pathlib
import re
import smtplib
import ssl
import sys
import time
import urllib.request
from email.message import EmailMessage

URL = "https://direct.playstation.com/fr-fr/buy-consoles/playstation5-pro-console-2-tb"
PRODUCT_CODE = "1000050720-FR"
STATE_PATH = pathlib.Path(".state/status.json")
KEEPALIVE_SECONDS = 60 * 60  # re-notify hourly while stock persists

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    # PS Direct serves utf-8; be defensive anyway.
    return raw.decode("utf-8", errors="replace")


def parse_stock(html: str) -> tuple[str, str]:
    """Return (status, note). status ∈ {in_stock, out_of_stock, unknown}."""
    code = re.escape(PRODUCT_CODE)
    buttons = re.findall(
        rf'<button\b[^>]*data-product-code="{code}"[^>]*>',
        html, flags=re.I,
    )
    if not buttons:
        return "unknown", "aucun bouton produit trouvé (page changée ?)"
    add_to_cart = [
        b for b in buttons
        if re.search(r'class="[^"]*\badd-to-cart\b', b, flags=re.I)
    ]
    if not add_to_cart:
        return "unknown", "pas de bouton add-to-cart"
    any_visible = any(
        not re.search(r'class="[^"]*\bhide\b[^"]*"', b, flags=re.I)
        for b in add_to_cart
    )
    if any_visible:
        return "in_stock", "bouton Ajouter au panier actif"
    # Safety check: buy box also shows "Actuellement Indisponible" when hidden.
    if "Actuellement Indisponible" in html:
        return "out_of_stock", "actuellement indisponible"
    return "out_of_stock", "add-to-cart caché (indispo)"


def load_prev() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def send_email(subject: str, body: str) -> None:
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to = os.environ.get("MAIL_TO", user)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)


def main() -> int:
    try:
        html = fetch_html(URL)
    except Exception as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1

    status, note = parse_stock(html)
    now = int(time.time())
    prev = load_prev()
    prev_status = prev.get("status")
    prev_notified_at = int(prev.get("last_notified_at") or 0)

    print(f"prev={prev_status!r} now={status!r} note={note!r}")

    should_email = False
    reason = ""
    if status == "in_stock":
        if prev_status != "in_stock":
            should_email = True
            reason = "transition rupture → EN STOCK"
        elif now - prev_notified_at >= KEEPALIVE_SECONDS:
            should_email = True
            reason = f"toujours en stock ({(now - prev_notified_at)//60} min)"

    new_state = {
        "status": status,
        "note": note,
        "last_check_at": now,
        "last_notified_at": prev_notified_at,
        "url": URL,
    }

    if should_email:
        subject = "🎮 PS5 Pro EN STOCK sur PlayStation Direct FR"
        body = (
            f"{reason}\n\n"
            f"Console PlayStation®5 Pro - 2 To\n"
            f"→ {URL}\n\n"
            f"Signal détecté : {note}\n"
            f"Prix affiché : 899,99 €\n\n"
            f"Fonce sur le lien. Sois connecté à ton compte PSN pour l'ajout au panier."
        )
        try:
            send_email(subject, body)
            new_state["last_notified_at"] = now
            print(f"email sent: {reason}")
        except Exception as e:
            print(f"email failed: {e}", file=sys.stderr)
            # Save the observation anyway so we don't lose the transition.

    save_state(new_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
