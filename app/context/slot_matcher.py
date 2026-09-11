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

import re

from app.context.models import OfferedSlot
from app.utils.it_dates import ITALIAN_WEEKDAYS

# Un numero isolato a inizio messaggio ("1", "3 ore 10", "2 va bene") è quasi
# sempre la scelta di un'opzione numerata: lo riconosciamo qui in modo
# deterministico, senza dipendere dal fatto che AI#1 l'abbia estratto
# correttamente come slot_number. Solo quando ci sono slot proposti e il
# numero è tra quelli realmente offerti (evita falsi positivi tipo "5 minuti").
_LEADING_NUMBER = re.compile(r"^\s*(\d{1,2})\b")


def _match_leading_number(message_text: str, offered_slots: list[OfferedSlot]) -> OfferedSlot | None:
    if not message_text:
        return None
    m = _LEADING_NUMBER.match(message_text)
    if not m:
        return None
    number = int(m.group(1))
    return next((o for o in offered_slots if o.option == number), None)


def match_offered_slot(
    entities: dict,
    offered_slots: list[OfferedSlot],
    message_text: str = "",
) -> OfferedSlot | None:
    if not offered_slots:
        return None

    slot_number = entities.get("slot_number")
    if slot_number is not None:
        return next((o for o in offered_slots if o.option == int(slot_number)), None)

    weekday = (entities.get("weekday") or "").strip().lower()
    exact_time = entities.get("exact_time")
    exact_date = entities.get("date_from")

    if not (weekday or exact_time or exact_date):
        # AI#1 non ha estratto nulla di utile: proviamo comunque il
        # riconoscimento deterministico di un numero a inizio messaggio,
        # prima di arrenderci (copre casi come "3 ore 10" che l'AI a
        # volte classifica male).
        return _match_leading_number(message_text, offered_slots)

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
    if len(candidates) == 1:
        return candidates[0]
    return None
