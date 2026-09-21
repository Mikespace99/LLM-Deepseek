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

from app.utils.it_dates import normalize_weekday


_MONTHS = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
}


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def _resolve_month_window(
    month_name: str,
    month_part: str | None,
    today: date,
) -> tuple[date, date] | None:
    """
    Traduce month + month_part in un range di date.
    Policy:
      start  → giorni 1–10
      mid    → giorni 11–20
      end    → giorno 21 → fine mese
      whole / None → tutto il mese
    Anno: se il mese (intero) è già alle spalle rispetto a oggi → anno successivo.
    """
    m = _MONTHS.get((month_name or "").strip().lower())
    if not m:
        return None

    year = today.year
    # Se siamo già oltre quel mese (o nello stesso mese ma oltre la fine del range "start"
    # non serve complicare: se il 1° del mese è prima di oggi e non siamo in quel mese, anno+1)
    first_of_month = date(year, m, 1)
    if first_of_month < date(today.year, today.month, 1):
        year += 1
        first_of_month = date(year, m, 1)

    last = _last_day_of_month(year, m)
    part = (month_part or "whole").strip().lower()

    if part == "start":
        return date(year, m, 1), date(year, m, min(10, last))
    if part == "mid":
        return date(year, m, min(11, last)), date(year, m, min(20, last))
    if part == "end":
        return date(year, m, min(21, last)), date(year, m, last)
    # whole
    return date(year, m, 1), date(year, m, last)


def _resolve_week_of_month_window(
    month_name: str,
    week_of_month: str | None,
    today: date,
) -> tuple[date, date] | None:
    """
    Traduce month + week_of_month ("1"|"2"|"3"|"4"|"last") in un range
    di date. Alternativa a _resolve_month_window (start/mid/end/whole):
    qui la granularita' e' la settimana, non il terzo di mese.

    Policy (bande fisse di 7 giorni, stesso principio di semplicita' e
    prevedibilita' usato per month_part - non calendario lun-dom, che
    varierebbe in modo poco intuitivo a seconda del giorno con cui
    inizia il mese):
      1    → giorni 1-7
      2    → giorni 8-14
      3    → giorni 15-21
      4    → giorni 22-28
      last → ultimi 7 giorni del mese (last-6 → last), cosi' copre
             correttamente sia mesi di 28 sia di 31 giorni.
    Anno: stessa regola di _resolve_month_window (mese gia' alle spalle
    → anno successivo).
    """
    m = _MONTHS.get((month_name or "").strip().lower())
    if not m:
        return None

    week = (week_of_month or "").strip().lower()
    if week not in {"1", "2", "3", "4", "last"}:
        return None

    year = today.year
    first_of_month = date(year, m, 1)
    if first_of_month < date(today.year, today.month, 1):
        year += 1

    last = _last_day_of_month(year, m)

    if week == "last":
        return date(year, m, max(1, last - 6)), date(year, m, last)

    week_n = int(week)
    start_day = (week_n - 1) * 7 + 1
    end_day = week_n * 7
    if start_day > last:
        # settimana richiesta oltre la fine del mese (mese corto): non
        # esiste, meglio non inventare un range vuoto/errato a valle.
        return None
    return date(year, m, start_day), date(year, m, min(end_day, last))


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
    
    target_iso = _WEEKDAY_NAME_TO_ISO.get(normalize_weekday(weekday_name))
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

    # ============================================================
    # CASO SPECIALE: giorno già scelto + solo cambio fascia oraria
    # ============================================================
    already_has_specific_day = (
        current.date_from is not None
        and current.date_to is not None
        and current.date_from == current.date_to
    )

    only_time_change = (
        (entities.get("time_preference") or entities.get("exact_time"))
        and not entities.get("period")
        and not entities.get("weekday")
        and not entities.get("week_part")
        and not entities.get("date_from")
    )

    if already_has_specific_day and only_time_change:
        # Blocca il giorno già scelto
        updated.date_from = current.date_from
        updated.date_to = current.date_to
        updated.preferred_date = current.date_from

        # Aggiorna solo l'orario
        if entities.get("time_preference"):
            updated.time_preference = entities["time_preference"]

        if entities.get("exact_time"):
            updated.preferred_time = _parse_hhmm(entities["exact_time"])
            updated.time_preference = "exact"
            updated.time_from = None
            updated.time_to = None
        elif updated.time_preference in _TIME_WINDOWS:
            start_h, end_h = _TIME_WINDOWS[updated.time_preference]
            updated.time_from = _hour(start_h)
            updated.time_to = _hour(end_h)
            updated.preferred_time = None

        return updated

    # --- Mese esplicito + settimana ordinale ("seconda settimana di ottobre") ---
    # Ha priorita' su month_part: sono due modi alternativi di restringere
    # lo stesso mese, e week_of_month e' il piu' specifico dei due quando
    # entrambi sono presenti (non dovrebbe succedere se l'AI segue il
    # prompt, ma in caso di ambiguita' la settimana vince perche' e' il
    # segnale piu' preciso che il cliente ha dato).
    if entities.get("month") and entities.get("week_of_month"):
        window = _resolve_week_of_month_window(
            entities["month"],
            entities.get("week_of_month"),
            today,
        )
        if window:
            from_date, to_date = window
            updated.date_from = from_date
            updated.date_to = to_date
            updated.preferred_date = None
            updated.period = None
            updated.week_part = None
            updated.preferred_weekday = None

            if entities.get("time_preference"):
                updated.time_preference = entities["time_preference"]
            if entities.get("exact_time"):
                updated.preferred_time = _parse_hhmm(entities["exact_time"])
                updated.time_from = updated.time_to = None
            elif updated.time_preference in _TIME_WINDOWS:
                start_h, end_h = _TIME_WINDOWS[updated.time_preference]
                updated.time_from = _hour(start_h)
                updated.time_to = _hour(end_h)
                updated.preferred_time = None

            print(
                f"[RESOLVER] month={entities.get('month')} "
                f"week_of_month={entities.get('week_of_month')} → {from_date}..{to_date}"
            )
            return updated
        # window None (es. settimana "4" richiesta su un mese troppo corto
        # per contenerla): niente panico, si scende normalmente al ramo
        # month_part qui sotto, che copre comunque il mese richiesto.

    # --- Mese esplicito (inizio/metà/fine/tutto) ---
    if entities.get("month"):
        window = _resolve_month_window(
            entities["month"],
            entities.get("month_part"),
            today,
        )
        if window:
            from_date, to_date = window
            updated.date_from = from_date
            updated.date_to = to_date
            updated.preferred_date = None
            updated.period = None
            updated.week_part = None
            updated.preferred_weekday = None

            # fascia oraria se presente nel messaggio
            if entities.get("time_preference"):
                updated.time_preference = entities["time_preference"]
            if entities.get("exact_time"):
                updated.preferred_time = _parse_hhmm(entities["exact_time"])
                updated.time_from = updated.time_to = None
            elif updated.time_preference in _TIME_WINDOWS:
                start_h, end_h = _TIME_WINDOWS[updated.time_preference]
                updated.time_from = _hour(start_h)
                updated.time_to = _hour(end_h)
                updated.preferred_time = None

            print(
                f"[RESOLVER] month={entities.get('month')} "
                f"part={entities.get('month_part')} → {from_date}..{to_date}"
            )
            return updated


  
    # --- 1. Aggiorna le etichette grezze, solo se il messaggio le porta ---
    if entities.get("period"):
        updated.period = entities["period"]
        # un nuovo period esplicito azzera un week_part di un periodo diverso
        updated.week_part = entities.get("week_part") or None
    elif entities.get("week_part"):
        updated.week_part = entities["week_part"]

    if entities.get("weekday"):
        updated.preferred_weekday = normalize_weekday(entities["weekday"]) or entities["weekday"]

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
