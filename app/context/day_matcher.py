"""
Riconoscimento di quale giorno (tra quelli mostrati nella panoramica
settimanale) l'utente ha scelto - stesso principio di slot_matcher.py,
applicato ai giorni invece che agli orari.

Perché serve: quando mostriamo "Lunedì 14, Mercoledì 16, Venerdì 18"
e l'utente risponde "venerdì" (o "domani", o clicca un bottone), non
vogliamo che il sistema debba ri-dedurre da zero DI QUALE settimana si
tratta - i giorni mostrati sono già la risposta, basta confrontare.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.context.models import OfferedDay
from app.utils.it_dates import ITALIAN_WEEKDAYS


def match_offered_day(entities: dict, offered_days: list[OfferedDay], today: date) -> OfferedDay | None:
    if not offered_days:
        return None

    # "domani"/"oggi" (period) si risolvono in una data assoluta e si
    # confrontano direttamente.
    period = entities.get("period")
    if period == "today":
        return next((d for d in offered_days if d.date == today), None)
    if period == "tomorrow":
        return next((d for d in offered_days if d.date == today + timedelta(days=1)), None)

    weekday = (entities.get("weekday") or "").strip().lower()
    if weekday:
        matches = [d for d in offered_days if ITALIAN_WEEKDAYS[d.date.isoweekday() % 7] == weekday]
        # Tra i giorni mostrati un nome di giorno è per costruzione
        # univoco (non mostriamo mai due volte lo stesso giorno della
        # settimana), ma per sicurezza controlliamo comunque.
        if len(matches) == 1:
            return matches[0]

    exact_date = entities.get("date_from")
    if exact_date:
        return next((d for d in offered_days if d.date.isoformat() == exact_date), None)

    return None
