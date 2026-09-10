"""
Resolver deterministico delle preferenze di ricerca.

Porta 1:1 l'algoritmo gia' collaudato in app/booking/engine.py
(_compute_search_window + _resolve_weekday_date), ma:

  - e' una funzione PURA: nessun accesso al DB, nessuna dipendenza da
    Supabase/repositories. Prende in input le entities di AI1Result e
    il SearchCriteria corrente, restituisce un SearchCriteria nuovo.
  - lavora su modelli tipizzati (SearchCriteria) invece che su un dict
    libero "preferences": lo stesso identico output (from_date/to_date/
    preferred_window) e' ora espresso come campi Pydantic verificabili.
  - gestisce esplicitamente la CONTINUITA' tra turni: se l'utente
    aggiunge solo un giorno o una fascia oraria, i campi non menzionati
    nel messaggio corrente vengono preservati da quanto gia' stabilito
    (invece di richiedere che sia l'AI a "ricordarsi" di ripeterli).

Principio invariato: l'AI classifica etichette (period/weekday/week_part/
time_preference), SOLO questo modulo calcola le date esatte.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.context.models import SearchCriteria

_WEEKDAY_NAME_TO_ISO = {
    "lunedì": 1,
    "martedì": 2,
    "mercoledì": 3,
    "giovedì": 4,
    "venerdì": 5,
    "sabato": 6,
    "domenica": 7,
}

_TIME_WINDOWS = {
    "morning": (6, 12),
    "afternoon": (12, 18),
    "evening": (18, 21),
}


def _resolve_weekday_date(
    weekday_name: str,
    from_date: date,
    to_date: date,
    today: date,
    slot_search_days: int,
) -> date | None:
    """
    Trova la data esatta corrispondente al giorno della settimana
    richiesto (es. "mercoledì").

    1) Cerca prima dentro la finestra gia' individuata (es. "prossima
       settimana"): il caso tipico di "mercoledì" dopo "prossima
       settimana".
    2) Se non la trova li', cerca la prossima occorrenza futura entro
       l'orizzonte di ricerca.
    """
    target_iso = _WEEKDAY_NAME_TO_ISO.get((weekday_name or "").strip().lower())
    if not target_iso:
        return None

    cursor = max(from_date, today)
    while cursor <= to_date:
        if cursor.isoweekday() == target_iso:
            return cursor
        cursor += timedelta(days=1)

    cursor = today
    horizon = today + timedelta(days=slot_search_days)
    while cursor <= horizon:
        if cursor.isoweekday() == target_iso:
            return cursor
        cursor += timedelta(days=1)

    return None


def resolve_search_criteria(
    entities: dict,
    current: SearchCriteria,
    today: date,
    slot_search_days: int = 30,
) -> SearchCriteria:
    """
    Combina le entities appena estratte da AI#1 con il SearchCriteria
    gia' in memoria, e ricalcola in modo deterministico le date esatte.

    - I campi "etichetta" (period, week_part, weekday, time_preference)
      vengono aggiornati SOLO se presenti nel messaggio corrente,
      altrimenti restano quelli gia' stabiliti (continuita').
    - date_from/date_to espliciti (l'utente ha detto una data assoluta,
      es. "il 15 settembre") hanno sempre priorita' su period/weekday.
    """
    updated = current.model_copy(deep=True)

    # --- 1. Aggiorna le etichette grezze, solo se il messaggio le porta ---
    if entities.get("period"):
        updated.period = entities["period"]
        # un nuovo period esplicito azzera un week_part di un periodo diverso
        updated.week_part = entities.get("week_part") or None
    elif entities.get("week_part"):
        updated.week_part = entities["week_part"]

    if entities.get("weekday"):
        updated.preferred_weekday = entities["weekday"]

    if entities.get("time_preference"):
        updated.time_preference = entities["time_preference"]

    # Una data assoluta esplicita ("il 15 settembre") sovrascrive period/weekday:
    # e' un nuovo vincolo piu' specifico, non un dettaglio aggiuntivo.
    explicit_date_from = entities.get("date_from")
    explicit_date_to = entities.get("date_to")
    if explicit_date_from:
        updated.date_from = date.fromisoformat(explicit_date_from)
        updated.date_to = date.fromisoformat(explicit_date_to or explicit_date_from)
        updated.period = None
        updated.week_part = None
        updated.preferred_weekday = None

    # --- 2. Se c'e' una data assoluta esplicita, la finestra e' gia' definita ---
    if explicit_date_from:
        from_date, to_date = updated.date_from, updated.date_to

    # --- 3. Altrimenti calcola la finestra dalle etichette (period/week_part) ---
    elif updated.period == "today":
        from_date = to_date = today

    elif updated.period == "tomorrow":
        from_date = to_date = today + timedelta(days=1)

    elif updated.period in ("this_week", "next_week"):
        this_monday = today - timedelta(days=today.weekday())
        monday = this_monday + timedelta(days=7) if updated.period == "next_week" else this_monday

        from_date = monday
        to_date = monday + timedelta(days=6)

        if updated.week_part == "start":
            to_date = from_date + timedelta(days=2)
        elif updated.week_part == "mid":
            from_date = from_date + timedelta(days=1)
            to_date = from_date + timedelta(days=2)
        elif updated.week_part == "weekend":
            from_date = from_date + timedelta(days=3)
            to_date = from_date + timedelta(days=2)

    else:
        # Ricerca aperta: nessun vincolo di giorno ancora espresso.
        from_date = today
        to_date = today + timedelta(days=slot_search_days)

    # --- 4. Giorno della settimana esplicito, risolto dentro la finestra ---
    if updated.preferred_weekday and not explicit_date_from:
        resolved = _resolve_weekday_date(
            updated.preferred_weekday, from_date, to_date, today, slot_search_days
        )
        if resolved:
            from_date = to_date = resolved

    updated.date_from = from_date
    updated.date_to = to_date
    updated.preferred_date = from_date if from_date == to_date else None

    # --- 5. Fascia oraria ---
    if entities.get("exact_time"):
        updated.preferred_time = _parse_hhmm(entities["exact_time"])
        updated.time_from = updated.time_to = None
    elif updated.time_preference in _TIME_WINDOWS:
        start_h, end_h = _TIME_WINDOWS[updated.time_preference]
        updated.time_from = _hour(start_h)
        updated.time_to = _hour(end_h)
        updated.preferred_time = None

    return updated


def _hour(h: int):
    from datetime import time
    return time(hour=h)


def _parse_hhmm(value: str):
    from datetime import time
    hh, mm = value.split(":")
    return time(hour=int(hh), minute=int(mm))
