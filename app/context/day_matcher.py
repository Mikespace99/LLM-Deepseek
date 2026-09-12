from __future__ import annotations

import datetime
from datetime import date, timedelta
from app.context.models import ConversationContext
from app.utils.it_dates import ITALIAN_WEEKDAYS


def match_offered_day(entities: dict, context: ConversationContext, today: date) -> str | None:
    """
    Riconosce quale giorno (tra quelli mostrati nella panoramica settimanale) l'utente ha scelto.
    Ritorna la stringa della data in formato 'YYYY-MM-DD' o None se non trova corrispondenze.
    """
    displayed_days = context.search.displayed_days
    
    # LOG DIAGNOSTICO: Vediamo cosa ha mostrato il sistema all'utente nell'ultimo turno
    print(f"[DIAGNOSTICA DAY_MATCHER] Giorni attualmente memorizzati nel contesto (displayed_days): {displayed_days}")
    print(f"[DIAGNOSTICA DAY_MATCHER] Entità ricevute dall'utente: {entities}")

    if not displayed_days:
        return None

    # 1. Gestione "oggi" / "domani"
    period = entities.get("period")
    target_date = None
    
    if period == "today":
        target_date = today.isoformat()
    elif period == "tomorrow":
        target_date = (today + timedelta(days=1)).isoformat()

    if target_date and target_date in displayed_days:
        print(f"[DIAGNOSTICA DAY_MATCHER] Ancoraggio riuscito via periodo (today/tomorrow): {target_date}")
        return target_date

    # 2. Gestione del nome del giorno (es. "venerdì")
    weekday = (entities.get("weekday") or "").strip().lower()
    if weekday:
        matches = []
        for date_str in displayed_days:
            try:
                dt = datetime.date.fromisoformat(date_str)
                # Verifica la corrispondenza del nome del giorno in italiano
                if ITALIAN_WEEKDAYS[dt.isoweekday() % 7] == weekday:
                    matches.append(date_str)
            except ValueError:
                continue
                
        if len(matches) == 1:
            print(f"[DIAGNOSTICA DAY_MATCHER] Ancoraggio riuscito via weekday ('{weekday}'): {matches[0]}")
            return matches[0]
        elif len(matches) > 1:
            print(f"[DIAGNOSTICA DAY_MATCHER] Ambiguità rilevata! Più date corrispondono a '{weekday}': {matches}")

    # 3. Gestione della data esatta ISO (se l'AI ha già estratto una data assoluta)
    exact_date = entities.get("date_from")
    if exact_date and exact_date in displayed_days:
        print(f"[DIAGNOSTICA DAY_MATCHER] Ancoraggio riuscito via data esatta: {exact_date}")
        return exact_date

    print("[DIAGNOSTICA DAY_MATCHER] Nessun ancoraggio deterministico trovato tra i giorni mostrati.")
    return None
