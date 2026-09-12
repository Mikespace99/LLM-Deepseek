"""
Motore locale di disponibilità e prenotazione (Agentic Enhanced Mode).

Distingue esplicitamente tra:
- giorni di chiusura/festivi
- giorni feriali senza orari
- giorni feriali completi
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.repositories import appointment as appointment_repo
from app.repositories import customer as customer_repo


ITALIAN_WEEKDAYS = [
    "domenica",
    "lunedì",
    "martedì",
    "mercoledì",
    "giovedì",
    "venerdì",
    "sabato",
]

ITALIAN_MONTHS = [
    "gennaio",
    "febbraio",
    "marzo",
    "aprile",
    "maggio",
    "giugno",
    "luglio",
    "agosto",
    "settembre",
    "ottobre",
    "novembre",
    "dicembre",
]

MAX_CANDIDATE_SLOTS = 3

_WEEKDAY_NAME_TO_ISO = {
    "lunedì": 1,
    "martedì": 2,
    "mercoledì": 3,
    "giovedì": 4,
    "venerdì": 5,
    "sabato": 6,
    "domenica": 7,
}


def _match_service(
    services: list[dict],
    requested_name: str | None,
) -> dict | None:
    if not requested_name:
        return None

    wanted = requested_name.lower().strip()

    for service in services:
        if (service.get("name") or "").lower().strip() == wanted:
            return service

    for service in services:
        service_name = (service.get("name") or "").lower().strip()

        if wanted in service_name:
            return service

    return None


def _build_context(
    tenant: dict,
    knowledge: dict,
    collected_data: dict,
) -> dict:
    services = knowledge.get("services") or []
    matched = _match_service(
        services,
        collected_data.get("service"),
    )

    duration_minutes = (matched or {}).get("duration_minutes") or 30
    buffer_before = (matched or {}).get("buffer_before") or 0

    buffer_after = (
        (matched or {}).get("buffer_after")
        if matched
        else 5
    )

    if buffer_after is None:
        buffer_after = 5

    location_id = (
        collected_data.get("location_id")
        or collected_data.get("locationId")
        or ((collected_data.get("location") or {}).get("id"))
    )

    tz_name = tenant.get("timezone") or "Europe/Rome"

    min_lead_hours = tenant.get("min_lead_hours")
    min_lead_hours = (
        float(min_lead_hours)
        if min_lead_hours is not None
        else 2.0
    )

    return {
        "tenant_id": tenant.get("id"),
        "timezone": tz_name,
        "slot_search_days": tenant.get("slot_search_days") or 30,
        "min_lead_hours": min_lead_hours,
        "service_name": (
            matched.get("name")
            if matched
            else (
                collected_data.get("service")
                or (
                    # fallback: se c'è un solo servizio nel tenant, usalo
                    services[0].get("name")
                    if len(services) == 1
                    else (
                        services[0].get("name")
                        if services and not collected_data.get("service")
                        else None
                    )
                )
            )
        ),
        "service_id": (
            matched.get("id")
            if matched
            else (
                services[0].get("id")
                if services and (len(services) == 1 or not collected_data.get("service"))
                else None
            )
        ),
        "duration_minutes": duration_minutes,
        "buffer_before": buffer_before,
        "buffer_after": buffer_after,
        "block_minutes": (
            buffer_before
            + duration_minutes
            + buffer_after
        ),
        "working_hours": knowledge.get("working_hours") or [],
        "exceptions": knowledge.get("exceptions") or [],
        "holidays": knowledge.get("holidays") or [],
        "location_id": location_id,
        "preferences": collected_data.get("preferences") or {},
        "person_name": collected_data.get("person_name"),
        "selected_slot": collected_data.get("selected_slot"),
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
    richiesto (es. "mercoledì"), in modo puramente deterministico:
    l'AI classifica solo "quale giorno" è stato nominato, questa
    funzione è l'UNICA responsabile del calcolo effettivo della data.

    1) Cerca prima dentro il periodo già stabilito nella conversazione
       (es. "prossima settimana"): è il caso normale di un follow-up
       tipo "mercoledì" dopo "prossima settimana".
    2) Se non trova nulla lì (periodo troppo stretto o già passato),
       cerca la prossima occorrenza futura di quel giorno entro
       l'orizzonte di ricerca del tenant.
    """
    target_iso = _WEEKDAY_NAME_TO_ISO.get(
        (weekday_name or "").strip().lower()
    )

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


def _compute_search_window(ctx: dict) -> dict:
    prefs = dict(ctx.get("preferences") or {})

    # week_part è solo start|mid|weekend; fascia oraria va in time_preference
    _wp = str(prefs.get("week_part") or "").strip().lower()
    if _wp in ("morning", "afternoon", "evening", "mattina", "pomeriggio", "sera"):
        _map = {
            "mattina": "morning",
            "pomeriggio": "afternoon",
            "sera": "evening",
        }
        if not prefs.get("time_preference"):
            prefs["time_preference"] = _map.get(_wp, _wp)
        prefs["week_part"] = None
        ctx = dict(ctx)
        ctx["preferences"] = prefs

    ignore_prefs = bool(
        prefs.get("ignore_preferences")
    )

    today = date.today()

    if ignore_prefs:
        from_date = today
        to_date = today + timedelta(
            days=ctx["slot_search_days"]
        )

    elif prefs.get("date"):
        from_date = to_date = _parse_date(
            prefs["date"]
        )

    elif prefs.get("date_from") and prefs.get("date_to"):
        from_date = _parse_date(
            prefs["date_from"]
        )
        to_date = _parse_date(
            prefs["date_to"]
        )

    elif prefs.get("period") == "today":
        from_date = to_date = today

    elif prefs.get("period") == "tomorrow":
        from_date = to_date = today + timedelta(days=1)

    elif prefs.get("period") in (
        "this_week",
        "next_week",
    ):
        # Lunedì della settimana CORRENTE, calcolato dal calendario reale
        # (non da un'AI): isoweekday() 1=lunedì ... 7=domenica.
        this_monday = today - timedelta(
            days=today.weekday()
        )

        monday = (
            this_monday + timedelta(days=7)
            if prefs.get("period") == "next_week"
            else this_monday
        )

        from_date = monday
        to_date = monday + timedelta(days=6)

        # Sotto-porzione della settimana (es. "inizio settimana"),
        # anch'essa calcolata qui e mai dall'AI.
        week_part = prefs.get("week_part")

        if week_part == "start":
            to_date = from_date + timedelta(days=2)
        elif week_part == "mid":
            from_date = from_date + timedelta(days=1)
            to_date = from_date + timedelta(days=2)
        elif week_part == "weekend":
            from_date = from_date + timedelta(days=3)
            to_date = from_date + timedelta(days=2)

    else:
        from_date = today
        to_date = today + timedelta(
            days=ctx["slot_search_days"]
        )

    # Giorno della settimana esplicito (es. "mercoledì"): risolto qui,
    # con codice deterministico, mai dall'AI.
    weekday_name = prefs.get("weekday")

    if (
        weekday_name
        and not prefs.get("date")
        and not ignore_prefs
    ):
        resolved = _resolve_weekday_date(
            weekday_name,
            from_date,
            to_date,
            today,
            ctx["slot_search_days"],
        )

        if resolved:
            from_date = to_date = resolved

    preferred_window = None

    if not ignore_prefs:
        time_preference = prefs.get("time_preference")

        if time_preference == "morning":
            preferred_window = {
                "start_hour": 6,
                "end_hour": 12,
            }

        elif time_preference == "afternoon":
            preferred_window = {
                "start_hour": 12,
                "end_hour": 18,
            }

        elif time_preference == "evening":
            preferred_window = {
                "start_hour": 18,
                "end_hour": 21,
            }

        elif (
            time_preference == "exact"
            and prefs.get("exact_time")
        ):
            preferred_window = {
                "exact": prefs["exact_time"],
            }

    has_date_constraint = bool(
        prefs.get("date")
        or prefs.get("date_from")
        or prefs.get("period")
        or prefs.get("weekday")
        or prefs.get("week_part")
    )

    has_time_constraint = preferred_window is not None

    search_was_narrow = (
        not ignore_prefs
        and (has_date_constraint or has_time_constraint)
    )

    ctx = dict(ctx)

    ctx.update(
        {
            "from_date": from_date,
            "to_date": to_date,
            "preferred_window": preferred_window,
            "search_was_narrow": search_was_narrow,
        }
    )

    print(f"[SEARCH-WINDOW] today={today} from_date={from_date} to_date={to_date} prefs={prefs}")

    return ctx


def _parse_date(value: str) -> date:
    return datetime.strptime(
        value[:10],
        "%Y-%m-%d",
    ).date()


def _parse_time(value: str) -> tuple[int, int]:
    parts = str(value)[:5].split(":")

    return (
        int(parts[0]),
        int(parts[1]),
    )


def _is_closed_day(
    date_str: str,
    holidays: list[dict],
    exceptions: list[dict],
) -> bool:
    holiday_dates = {
        str(holiday.get("date"))[:10]
        for holiday in holidays
    }

    if date_str in holiday_dates:
        return True

    for exception in exceptions:
        if (
            str(exception.get("date"))[:10] == date_str
            and exception.get("type") == "closed"
            and exception.get("active") is not False
        ):
            return True

    return False


def _is_in_closed_period(
    slot_start: datetime,
    slot_end: datetime,
    date_str: str,
    exceptions: list[dict],
    tz: ZoneInfo,
) -> bool:
    for exception in exceptions:
        if (
            str(exception.get("date"))[:10]
            != date_str
        ):
            continue

        if (
            exception.get("type")
            != "closed_period"
            or exception.get("active") is False
        ):
            continue

        if (
            not exception.get("start_time")
            or not exception.get("end_time")
        ):
            continue

        start_hour, start_minute = _parse_time(
            exception["start_time"]
        )

        end_hour, end_minute = _parse_time(
            exception["end_time"]
        )

        year, month, day = (
            int(part)
            for part in date_str.split("-")
        )

        closed_start = datetime(
            year,
            month,
            day,
            start_hour,
            start_minute,
            tzinfo=tz,
        )

        closed_end = datetime(
            year,
            month,
            day,
            end_hour,
            end_minute,
            tzinfo=tz,
        )

        if (
            slot_start < closed_end
            and slot_end > closed_start
        ):
            return True

    return False


def _generate_and_filter_slots(
    ctx: dict,
    busy_events: list[dict],
) -> dict:
    tz = ZoneInfo(
        ctx["timezone"] or "Europe/Rome"
    )

    busy = []

    for event in busy_events:
        appointment_date = event.get(
            "appointment_date"
        )
        appointment_time = event.get(
            "appointment_time"
        )

        duration = (
            event.get("duration_minutes")
            or 30
        )

        if not appointment_date or not appointment_time:
            continue

        year, month, day = (
            int(part)
            for part in str(appointment_date)[:10].split("-")
        )

        hour, minute = _parse_time(
            appointment_time
        )

        start = datetime(
            year,
            month,
            day,
            hour,
            minute,
            tzinfo=tz,
        )

        end = start + timedelta(
            minutes=duration
        )

        busy.append((start, end))

    def overlaps_busy(
        start: datetime,
        end: datetime,
    ) -> bool:
        return any(
            start < busy_end
            and end > busy_start
            for busy_start, busy_end in busy
        )

    working_hours = ctx["working_hours"]

    location_id = ctx.get("location_id")

    if location_id:
        working_hours = [
            wh
            for wh in working_hours
            if (
                not wh.get("location_id")
                or wh.get("location_id") == location_id
            )
        ]

    duration_min = ctx["duration_minutes"]
    buffer_before = ctx["buffer_before"]
    buffer_after = ctx["buffer_after"]

    lead_time = timedelta(
        hours=ctx["min_lead_hours"]
    )

    now = datetime.now(tz)

    holidays = ctx["holidays"]
    exceptions = ctx["exceptions"]

    all_slots = []

    day = ctx["from_date"]
    last_day = ctx["to_date"]

    # Flag diagnostici di chiusura feriale.
    any_day_was_open = False
    has_theoretical_hours = False

    while day <= last_day:
        date_str = day.isoformat()

        if not _is_closed_day(
            date_str,
            holidays,
            exceptions,
        ):
            any_day_was_open = True

            # isoweekday():
            # 1 = lunedì ... 7 = domenica
            day_of_week = day.isoweekday()

            day_hours = [
                wh
                for wh in working_hours
                if int(
                    wh.get("day_of_week", -1)
                ) == day_of_week
            ]

            if day_hours:
                has_theoretical_hours = True

            for wh in day_hours:
                start_hour, start_minute = _parse_time(
                    wh["start_time"]
                )

                end_hour, end_minute = _parse_time(
                    wh["end_time"]
                )

                cursor = datetime(
                    day.year,
                    day.month,
                    day.day,
                    start_hour,
                    start_minute,
                    tzinfo=tz,
                )

                day_end = datetime(
                    day.year,
                    day.month,
                    day.day,
                    end_hour,
                    end_minute,
                    tzinfo=tz,
                )

                while (
                    cursor
                    + timedelta(minutes=duration_min)
                    <= day_end
                ):
                    visit_start = cursor

                    visit_end = (
                        cursor
                        + timedelta(
                            minutes=duration_min
                        )
                    )

                    block_start = (
                        visit_start
                        - timedelta(
                            minutes=buffer_before
                        )
                    )

                    block_end = (
                        visit_end
                        + timedelta(
                            minutes=buffer_after
                        )
                    )

                    is_future = (
                        visit_start
                        >= now + lead_time
                    )

                    free = (
                        is_future
                        and not overlaps_busy(
                            block_start,
                            block_end,
                        )
                        and not _is_in_closed_period(
                            visit_start,
                            visit_end,
                            date_str,
                            exceptions,
                            tz,
                        )
                    )

                    if free:
                        all_slots.append(
                            {
                                "start": visit_start,
                                "location_id": (
                                    wh.get("location_id")
                                    or location_id
                                ),
                            }
                        )

                    cursor += timedelta(
                        minutes=duration_min
                    )

        day += timedelta(days=1)

    # Filtro fascia preferita.
    preferred_window = ctx.get(
        "preferred_window"
    )

    filtered = all_slots
    matched_preferences = False

    if preferred_window:
        if preferred_window.get("exact"):
            exact_hour, exact_minute = _parse_time(
                preferred_window["exact"]
            )

            exact = [
                slot
                for slot in all_slots
                if (
                    slot["start"].hour
                    == exact_hour
                    and slot["start"].minute
                    == exact_minute
                )
            ]

            if exact:
                filtered = exact
                matched_preferences = True

            else:
                target = (
                    exact_hour * 60
                    + exact_minute
                )

                def distance(slot):
                    slot_minutes = (
                        slot["start"].hour * 60
                        + slot["start"].minute
                    )

                    return abs(
                        slot_minutes - target
                    )

                filtered = sorted(
                    all_slots,
                    key=distance,
                )

        else:
            in_window = [
                slot
                for slot in all_slots
                if (
                    preferred_window["start_hour"]
                    <= slot["start"].hour
                    < preferred_window["end_hour"]
                )
            ]

            if in_window:
                filtered = in_window
                matched_preferences = True
            else:
                # Preferenza fascia rispettata: se non c'è nulla in fascia,
                # NON ricadere sugli slot fuori fascia (es. mattina quando
                # è stato chiesto afternoon). Lista vuota → no_slots a monte.
                filtered = []
                matched_preferences = False
                print(
                    "[ENGINE] preferred_window senza match: "
                    f"{preferred_window} su {len(all_slots)} slot grezzi"
                )

    filtered = sorted(
        filtered,
        key=lambda slot: slot["start"],
    )

    top = filtered[:MAX_CANDIDATE_SLOTS]

    candidate_slots = []

    for slot in top:
        dt = slot["start"]

        weekday = ITALIAN_WEEKDAYS[
            dt.isoweekday() % 7
        ]

        month = ITALIAN_MONTHS[
            dt.month - 1
        ]

        hour = f"{dt.hour:02d}"
        minute = f"{dt.minute:02d}"

        candidate_slots.append(
            {
                "datetime": dt.astimezone(
                    ZoneInfo("UTC")
                ).isoformat(),
                "date": dt.strftime(
                    "%Y-%m-%d"
                ),
                "time": f"{hour}:{minute}",
                "location_id": slot[
                    "location_id"
                ],
                "label": (
                    f"{weekday.capitalize()} "
                    f"{dt.day} {month} "
                    f"alle {hour}:{minute}"
                ),
            }
        )

    # Calcolo diagnostico reale.
    studio_closed = (
        not any_day_was_open
        or not has_theoretical_hours
    )

    studio_full = (
        len(candidate_slots) == 0
        and not studio_closed
    )

    return {
        "candidate_slots": candidate_slots,
        "matched_preferences": matched_preferences,
        "result": {
            "success": True,
            "no_slots": len(candidate_slots) == 0,
            "is_studio_closed": studio_closed,
            "is_studio_full": studio_full,
            "search_was_narrow": ctx[
                "search_was_narrow"
            ],
        },
    }


# ============================================================
# API PUBBLICA
# ============================================================


def search_availability(
    tenant: dict,
    knowledge: dict,
    collected_data: dict,
) -> dict:
    ctx = _build_context(
        tenant,
        knowledge,
        collected_data,
    )

    ctx = _compute_search_window(ctx)

    busy_events = (
        appointment_repo.list_busy_for_availability(
            tenant_id=ctx["tenant_id"],
            date_from=ctx[
                "from_date"
            ].isoformat(),
            date_to=ctx[
                "to_date"
            ].isoformat(),
        )
    )

    return _generate_and_filter_slots(
        ctx,
        busy_events,
    )


def revalidate_slots(
    tenant: dict,
    knowledge: dict,
    collected_data: dict,
    slots: list[dict],
) -> list[dict]:
    """
    Riverifica che una lista di slot proposti in un turno precedente
    sia ANCORA libera adesso (nessuna prenotazione concorrente nel
    frattempo, nessuno slot ormai nel passato o sotto il min_lead_hours).

    Usata dal fallback "ti ripropongo le opzioni di prima": il backend,
    non l'AI, decide quali orari sono davvero ancora disponibili.
    L'AI Redattrice non deve mai riscrivere a mano date/orari già
    proposti, perché nel frattempo potrebbero non essere più validi.
    """
    if not slots:
        return []

    ctx = _build_context(tenant, knowledge, collected_data)
    tz = ZoneInfo(ctx["timezone"] or "Europe/Rome")

    dates = [str(s["date"])[:10] for s in slots if s.get("date")]

    if not dates:
        return []

    busy_events = appointment_repo.list_busy_for_availability(
        tenant_id=ctx["tenant_id"],
        date_from=min(dates),
        date_to=max(dates),
    )

    busy = []

    for event in busy_events:
        appointment_date = event.get("appointment_date")
        appointment_time = event.get("appointment_time")
        duration = event.get("duration_minutes") or 30

        if not appointment_date or not appointment_time:
            continue

        year, month, day = (
            int(part)
            for part in str(appointment_date)[:10].split("-")
        )

        hour, minute = _parse_time(appointment_time)

        start = datetime(
            year, month, day, hour, minute, tzinfo=tz
        )

        busy.append((start, start + timedelta(minutes=duration)))

    def overlaps_busy(start, end):
        return any(
            start < busy_end and end > busy_start
            for busy_start, busy_end in busy
        )

    duration_min = ctx["duration_minutes"]
    buffer_before = ctx["buffer_before"]
    buffer_after = ctx["buffer_after"]
    lead_time = timedelta(hours=ctx["min_lead_hours"])
    now = datetime.now(tz)

    valid = []

    for slot in slots:
        try:
            year, month, day = (
                int(part)
                for part in str(slot["date"])[:10].split("-")
            )
            hour, minute = _parse_time(slot["time"])
        except (KeyError, ValueError, TypeError):
            continue

        visit_start = datetime(
            year, month, day, hour, minute, tzinfo=tz
        )
        visit_end = visit_start + timedelta(minutes=duration_min)

        block_start = visit_start - timedelta(minutes=buffer_before)
        block_end = visit_end + timedelta(minutes=buffer_after)

        if visit_start < now + lead_time:
            continue

        if overlaps_busy(block_start, block_end):
            continue

        valid.append(slot)

    return valid[:MAX_CANDIDATE_SLOTS]


def create_booking(
    tenant: dict,
    knowledge: dict,
    collected_data: dict,
    customer: dict | None = None,
    phone_number: str | None = None,
) -> dict:
    ctx = _build_context(
        tenant,
        knowledge,
        collected_data,
    )

    slot = ctx.get("selected_slot") or {}

    missing = []
    if not slot.get("datetime"):
        missing.append("slot.datetime")
    if not ctx.get("service_name"):
        missing.append("service_name")
    if not ctx.get("person_name"):
        missing.append("person_name")

    valid = not missing

    if not valid:
        print(
            f"[create_booking] missing_data: {missing} | "
            f"person_name={ctx.get('person_name')!r} "
            f"service={ctx.get('service_name')!r} "
            f"slot_keys={list(slot.keys()) if slot else None}"
        )
        return {
            "selected_slot": slot,
            "result": {
                "success": False,
                "error": "missing_data",
                "missing_fields": missing,
            },
        }

    try:
        row = appointment_repo.create_appointment(
            tenant_id=ctx["tenant_id"],
            appointment_date=slot["date"],
            appointment_time=slot["time"],
            duration_minutes=ctx["block_minutes"],
            source="whatsapp",
            customer_id=(
                customer.get("id")
                if customer
                else None
            ),
            phone_number=phone_number,
            service=ctx["service_name"],
            service_id=ctx.get("service_id"),
            location_id=(
                slot.get("location_id")
                or ctx.get("location_id")
            ),
            status="confirmed",
        )

        customer_id = customer.get("id") if customer else None
        if customer_id and ctx.get("person_name"):
            # Il nome era richiesto come obbligatorio per prenotare ma
            # non veniva mai salvato: lo scriviamo sull'anagrafica del
            # cliente, cosi' compare anche in Agenda (che mostra
            # customers.full_name) e non va richiesto di nuovo la
            # prossima volta. Non deve mai far fallire una prenotazione
            # gia' andata a buon fine.
            try:
                customer_repo.update_customer_name(ctx["tenant_id"], customer_id, ctx["person_name"])
            except Exception as name_exc:
                print(f"[create_booking] impossibile salvare il nome sul cliente: {name_exc!r}")

        return {
            "selected_slot": slot,
            "result": {
                "success": True,
                "appointment_id": row.get("id"),
            },
        }

    except Exception as exc:
        if appointment_repo.is_overlap_error(exc):
            return {
                "selected_slot": slot,
                "result": {
                    "success": False,
                    "error": "slot_conflict",
                },
            }

        # Qualunque altro errore (vincolo diverso, dato non valido, timeout...)
        # NON è un conflitto di orario: va loggato per intero lato server,
        # perché il chiamante lo deve poter distinguere da uno slot
        # davvero occupato invece di raccontare all'utente una bugia
        # innocente ("è già occupato") che nasconde il problema reale.
        print(f"[BOOKING ERROR] create_appointment ha fallito per un motivo non previsto: {exc!r}")

        return {
            "selected_slot": slot,
            "result": {
                "success": False,
                "error": str(exc),
            },
        }


# ============================================================
# RICERCA GUIDATA A GIORNI (menu scelte progressive)
# ============================================================

def search_available_days(
    tenant: dict,
    knowledge: dict,
    collected_data: dict,
    max_days: int = 3,
) -> dict:
    """
    Restituisce i primi `max_days` giorni con almeno uno slot libero
    nei prossimi slot_search_days del tenant, con indicazione
    mattina (< 13:00) / pomeriggio (>= 13:00).

    Ritorno:
    {
      "available_days": [
        {
          "date": "YYYY-MM-DD",
          "label": "Martedì 9 settembre",
          "morning": True,
          "afternoon": False,
        },
        ...
      ],
      "result": {"success": True, "no_days": False}
    }
    """
    # Ricerca ampia: ignoriamo preferenze temporali strette
    wide = dict(collected_data or {})
    prefs = dict(wide.get("preferences") or {})
    prefs["ignore_preferences"] = True
    # Pulisce eventuali vincoli di giorno/periodo
    for k in ("date", "date_from", "date_to", "period", "weekday", "week_part"):
        prefs.pop(k, None)
    wide["preferences"] = prefs

    ctx = _build_context(tenant, knowledge, wide)
    ctx = _compute_search_window(ctx)

    busy_events = appointment_repo.list_busy_for_availability(
        tenant_id=ctx["tenant_id"],
        date_from=ctx["from_date"].isoformat(),
        date_to=ctx["to_date"].isoformat(),
    )

    # Genera tutti gli slot nella finestra (il filtro preferred_window
    # è già None perché ignore_preferences=True).
    # _generate_and_filter_slots taglia a MAX_CANDIDATE_SLOTS: per i giorni
    # ci serve di più, quindi scansioniamo giorno per giorno.
    by_date: dict[str, list] = {}
    day = ctx["from_date"]
    last_day = ctx["to_date"]

    while day <= last_day and len(by_date) < max_days:
        date_str = day.isoformat()

        day_prefs = {
            "date": date_str,
            "date_from": date_str,
            "date_to": date_str,
        }
        day_collected = dict(wide)
        day_collected["preferences"] = day_prefs

        day_ctx = _build_context(tenant, knowledge, day_collected)
        day_ctx = _compute_search_window(day_ctx)
        raw = _generate_and_filter_slots(day_ctx, busy_events)
        slots = raw.get("candidate_slots") or []

        if slots:
            by_date[date_str] = slots

        day += timedelta(days=1)

    available_days = []
    for date_str in sorted(by_date.keys()):
        slots = by_date[date_str]

        morning = False
        afternoon = False
        for s in slots:
            try:
                hour = int(str(s.get("time") or "99:99")[:2])
            except ValueError:
                continue
            if hour < 13:
                morning = True
            else:
                afternoon = True

        try:
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
            weekday = ITALIAN_WEEKDAYS[dt.isoweekday() % 7]
            month = ITALIAN_MONTHS[dt.month - 1]
            label = f"{weekday.capitalize()} {dt.day} {month}"
        except Exception:
            label = date_str

        available_days.append({
            "date": date_str[:10],
            "label": label,
            "morning": morning,
            "afternoon": afternoon,
        })

    return {
        "available_days": available_days,
        "result": {
            "success": True,
            "no_days": len(available_days) == 0,
        },
    }


def search_times_for_day(
    tenant: dict,
    knowledge: dict,
    collected_data: dict,
    target_date: str,
) -> dict:
    """
    Restituisce gli slot orari reali di un singolo giorno.
    Riusa interamente search_availability con la data fissata.
    """
    data = dict(collected_data or {})
    prefs = dict(data.get("preferences") or {})
    prefs["date"] = target_date[:10]
    prefs["date_from"] = target_date[:10]
    prefs["date_to"] = target_date[:10]
    # Rimuove filtri di periodo ampi che potrebbero confliggere
    for k in ("period", "weekday", "week_part", "ignore_preferences"):
        prefs.pop(k, None)
    data["preferences"] = prefs

    return search_availability(
        tenant=tenant,
        knowledge=knowledge,
        collected_data=data,
    )
