"""Match invoices received in KSeF to purchase invoices already booked in accounting.

The key is the seller's tax id (NIP) plus the invoice number, both normalised.
"""

from __future__ import annotations

import re


def normalize_number(value: str | None) -> str:
    if not value:
        return ""
    s = re.sub(r"\s+", "", value)
    # A backslash between digits is a segment separator typed the wrong way round
    # ("12\2026" means "12/2026"). Anywhere else it is noise and is dropped.
    s = re.sub(r"(?<=\d)\\(?=\d)", "/", s)
    s = s.replace("\\", "")
    return s.upper()


def normalize_nip(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\D", "", value)


def mark_booked(ksef_invoices: list[dict], merit_purchases: list[dict]) -> list[dict]:
    index = {
        (normalize_nip(p.get("vendor_nip")), normalize_number(p.get("number"))): p.get("id")
        for p in merit_purchases
    }
    out = []
    for inv in ksef_invoices:
        key = (normalize_nip(inv.get("seller_nip")), normalize_number(inv.get("invoice_number")))
        merit_id = index.get(key)
        out.append({**inv, "booked_in_merit": merit_id is not None, "merit_id": merit_id})
    return out
