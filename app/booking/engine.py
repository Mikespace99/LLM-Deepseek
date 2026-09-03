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

MAX_CANDIDATE_SLOTS = 5


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
            else collected_data.get("service")
        ),
        "service_id": matched.get("id") if matched else None,
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


def _compute_search_window(ctx: dict) -> dict:
    prefs = ctx["preferences"] or {}

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
        base = (
            today + timedelta(days=7)
            if prefs.get("period") == "next_week"
            else today
        )

        from_date = base
        to_date = base + timedelta(days=6)

    else:
        from_date = today
        to_date = today + timedelta(
            days=ctx["slot_search_days"]
        )

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

    valid = bool(
        slot.get("datetime")
        and ctx.get("service_name")
        and ctx.get("person_name")
    )

    if not valid:
        return {
            "selected_slot": slot,
            "result": {
                "success": False,
                "error": "missing_data",
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

        return {
            "selected_slot": slot,
            "result": {
                "success": False,
                "error": str(exc),
            },
        }
