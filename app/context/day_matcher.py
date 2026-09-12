from __future__ import annotations

import datetime
from datetime import date, timedelta

from app.context.models import OfferedDay
from app.utils.it_dates import ITALIAN_WEEKDAYS


def match_offered_day(entities: dict, offered_days: list[OfferedDay], today: date) -> OfferedDay | None:
    """
    Riconosce quale giorno (tra quelli mostrati nella panoramica settimanale)
    l'utente ha scelto.

    Riceve direttamente la lista dei giorni proposti (context.offered_days),
    non l'intero context: ogni elemento e' un OfferedDay con campi
    option / date / label. Ritorna l'OfferedDay corrispondente, o None se
    non trova corrispondenze - mai una stringa, cosi' il chiamante
    (_apply_day_selection) puo' continuare a leggere matched.date/.label
    senza altre modifiche.
    """
    # LOG DIAGNOSTICO: vediamo cosa ha mostrato il sistema all'utente nell'ultimo turno
    print(f"[DIAGNOSTICA DAY_MATCHER] Giorni attualmente proposti (offered_days): {offered_days}")
    print(f"[DIAGNOSTICA DAY_MATCHER] Entità ricevute dall'utente: {entities}")

    if not offered_days:
        return None

    by_date = {od.date: od for od in offered_days}

    # 1. Gestione "oggi" / "domani"
    period = entities.get("period")
    target_date = None

    if period == "today":
        target_date = today
    elif period == "tomorrow":
        target_date = today + timedelta(days=1)

    if target_date and target_date in by_date:
        print(f"[DIAGNOSTICA DAY_MATCHER] Ancoraggio riuscito via periodo (today/tomorrow): {target_date.isoformat()}")
        return by_date[target_date]

    # 2. Gestione del nome del giorno (es. "venerdì")
    weekday = (entities.get("weekday") or "").strip().lower()
    if weekday:
        matches = [
            od for od in offered_days
            if ITALIAN_WEEKDAYS[od.date.isoweekday() % 7] == weekday
        ]

        if len(matches) == 1:
            print(f"[DIAGNOSTICA DAY_MATCHER] Ancoraggio riuscito via weekday ('{weekday}'): {matches[0].date.isoformat()}")
            return matches[0]
        elif len(matches) > 1:
            print(
                "[DIAGNOSTICA DAY_MATCHER] Ambiguità rilevata! Più date corrispondono a "
                f"'{weekday}': {[m.date.isoformat() for m in matches]}"
            )

    # 3. Gestione della data esatta ISO (se l'AI ha già estratto una data assoluta)
    exact_date_str = entities.get("date_from")
    if exact_date_str:
        try:
            exact_date = (
                exact_date_str
                if isinstance(exact_date_str, date)
                else datetime.date.fromisoformat(str(exact_date_str)[:10])
            )
        except ValueError:
            exact_date = None

        if exact_date and exact_date in by_date:
            print(f"[DIAGNOSTICA DAY_MATCHER] Ancoraggio riuscito via data esatta: {exact_date.isoformat()}")
            return by_date[exact_date]

    print("[DIAGNOSTICA DAY_MATCHER] Nessun ancoraggio deterministico trovato tra i giorni mostrati.")
    return None
