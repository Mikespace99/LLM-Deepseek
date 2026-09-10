"""
LIVELLO 1 di risoluzione: riconoscimento universale contro il contesto
già mostrato all'utente.

Principio: se l'utente descrive (per numero, orario o giorno) uno slot
che coincide con uno di quelli già proposti, è una selezione - punto.
Non ha importanza come AI#1 ha classificato l'intent (CONFIRM, BOOK,
o altro): il segnale nei dati vince, perché è più affidabile della
sola classificazione testuale.

Questo modulo sostituisce la necessità di prevedere ogni possibile
intent con cui l'utente potrebbe esprimere una selezione ("lunedì alle
10", "quello delle 10", "va bene per lunedì prossimo"...): invece di
insegnare ad AI#1 a riconoscere N varianti, qui basta confrontare i
dati con ciò che è già a schermo.
"""

from __future__ import annotations

from app.context.models import OfferedSlot
from app.utils.it_dates import ITALIAN_WEEKDAYS


def match_offered_slot(entities: dict, offered_slots: list[OfferedSlot]) -> OfferedSlot | None:
    if not offered_slots:
        return None

    slot_number = entities.get("slot_number")
    if slot_number is not None:
        return next((o for o in offered_slots if o.option == int(slot_number)), None)

    weekday = (entities.get("weekday") or "").strip().lower()
    exact_time = entities.get("exact_time")
    exact_date = entities.get("date_from")

    if not (weekday or exact_time or exact_date):
        return None  # nessun segnale utile: non proviamo a indovinare

    candidates = list(offered_slots)

    if weekday:
        candidates = [
            o for o in candidates
            if ITALIAN_WEEKDAYS[o.slot.date.isoweekday() % 7] == weekday
        ]
    if exact_date:
        candidates = [o for o in candidates if o.slot.date.isoformat() == exact_date]
    if exact_time:
        candidates = [o for o in candidates if o.slot.time.strftime("%H:%M") == exact_time]

    # Corrispondenza univoca -> è quella. Se ambigua, meglio chiedere
    # piuttosto che indovinare.
    return candidates[0] if len(candidates) == 1 else None
