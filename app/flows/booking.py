"""
FLOW: BOOK (creazione di un nuovo appuntamento)

Da quando esiste anche reschedule.py, questo file contiene SOLO ciò che
è davvero specifico di "prenotare": la ricerca disponibilità è condivisa
(flows/common.run_availability_search), la selezione slot generica pure
(advance_after_slot_selection). Qui restano: l'ingresso nel flusso e la
creazione vera e propria alla conferma.

Nomi delle funzioni allineati con reschedule.py (start_search,
slot_selected, confirm, reject_confirmation) cosi' il Router puo'
scegliere il modulo giusto in base a context.operation.type senza
bisogno di sapere altro (vedi router/routing_table.py).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.booking import engine
from app.context.models import ConversationContext, ConversationStep, Operation, OfferedDay, PendingAction, SystemResult
from app.flows.common import (
    advance_after_customer_data,
    advance_after_slot_selection,
    build_search_collected_data,
    find_selected_offered_slot,
    has_search_criteria,
    offered_slot_to_engine_dict,
    reset_for_new_operation,
    run_availability_search,
)
from app.utils.it_dates import today_in_tz
from app.web.sse import notify_agenda


def _week_effectively_over(knowledge: dict, tenant: dict, today: date, this_sunday: date) -> bool:
    """
    True se mancano meno di 2 ore alla fine degli orari di lavoro
    rimasti in questa settimana (o se non ne restano proprio) - in
    quel caso non ha senso dire "questa settimana non c'è
    disponibilità", si passa direttamente alla prossima.
    """
    try:
        tz = ZoneInfo(tenant.get("timezone") or "Europe/Rome")
    except Exception:
        tz = ZoneInfo("Europe/Rome")
    now = datetime.now(tz)

    latest_end = None
    for wh in knowledge.get("working_hours") or []:
        try:
            day_of_week = int(wh.get("day_of_week", -1))
        except (TypeError, ValueError):
            continue
        if day_of_week < today.isoweekday() or day_of_week > 7:
            continue
        wh_date = today + timedelta(days=day_of_week - today.isoweekday())
        if wh_date > this_sunday:
            continue
        end_time = wh.get("end_time")
        if not end_time:
            continue
        try:
            hh, mm = (int(p) for p in str(end_time)[:5].split(":"))
        except ValueError:
            continue
        candidate_end = datetime(wh_date.year, wh_date.month, wh_date.day, hh, mm, tzinfo=tz)
        if latest_end is None or candidate_end > latest_end:
            latest_end = candidate_end

    if latest_end is None:
        return True  # nessun orario di lavoro rimasto questa settimana
    return now >= latest_end - timedelta(hours=2)


def start_search(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    if context.conversation.current_step == ConversationStep.COMPLETED:
        context = reset_for_new_operation(context)

    s = context.search

    # Giorno singolo già ancorato → vai agli orari
    single_day = (
        s.preferred_date is not None
        or (s.date_from is not None and s.date_to is not None and s.date_from == s.date_to)
    )
    if single_day:
        return run_availability_search(context, tenant, knowledge)

    # Range, settimana, mese, o nessun criterio → panoramica giorni
    return show_week_overview(context, tenant, knowledge)

def show_week_overview(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    """
    Panoramica giorni disponibili. Rispetta:
    - period (this_week / next_week / today / tomorrow / aperto)
    - time_preference (morning / afternoon): filtra i giorni che hanno
      quella fascia; non salta agli slot orari.
    """
    context = context.model_copy(deep=True)

    today = today_in_tz(tenant.get("timezone"))
    this_monday = today - timedelta(days=today.weekday())
    this_sunday = this_monday + timedelta(days=6)
    next_monday = this_sunday + timedelta(days=1)
    next_sunday = this_sunday + timedelta(days=7)

    period = context.search.period
    time_pref = context.search.time_preference  # morning | afternoon | evening | None


    period = context.search.period
    time_pref = context.search.time_preference

    # Se il resolver ha già fissato un range (es. inizio ottobre), usalo.
    if (
        context.search.date_from
        and context.search.date_to
        and context.search.date_from != context.search.date_to
    ):
        date_from = max(context.search.date_from, today)
        date_to = context.search.date_to
    elif period == "next_week":
        date_from, date_to = next_monday, next_sunday
    elif period == "this_week":
        date_from, date_to = today, this_sunday
    elif period == "today":
        date_from = date_to = today
    elif period == "tomorrow":
        date_from = date_to = today + timedelta(days=1)
    else:
        date_from, date_to = today, next_sunday


    base_data = {
        "service": context.service.value,
        "location_id": context.professional.location_id if context.professional else None,
    }

    try:
        two_weeks = engine.search_available_days(
            tenant,
            knowledge,
            {
                **base_data,
                "preferences": {
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                },
            },
            max_days=14,
        )
    except Exception as exc:
        print(f"[flows.booking.show_week_overview] errore ricerca giorni disponibili: {exc!r}")
        return context, SystemResult(success=False, error_code="TECHNICAL_ERROR")

    available_days = two_weeks.get("available_days") or []

    if time_pref == "morning":
        available_days = [d for d in available_days if d.get("morning")]
    elif time_pref == "afternoon":
        available_days = [d for d in available_days if d.get("afternoon")]

    # Primi 3 giorni disponibili nel range richiesto
    shown = available_days[:3]

    context.conversation.current_step = ConversationStep.SEARCH_AVAILABILITY
    context.conversation.pending_action = PendingAction.PROVIDE_DATE
    context.offered_days = [
        OfferedDay(option=i, date=date.fromisoformat(d["date"]), label=d["label"])
        for i, d in enumerate(shown, start=1)
    ]

    return context, SystemResult(
        success=True,
        data={
            "this_week": shown,          # riuso campo esistente per AI#2
            "next_week": [],
            "first_available": None,
            "this_week_over": False,
            "time_preference": time_pref,
            "period": period,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
        },
    )


    # Filtro fascia: tieni solo i giorni che hanno quella disponibilità.
    if time_pref == "morning":
        available_days = [d for d in available_days if d.get("morning")]
    elif time_pref == "afternoon":
        available_days = [d for d in available_days if d.get("afternoon")]
    # evening: l'engine espone solo morning/afternoon; non filtriamo qui.

    this_week = [d for d in available_days if d["date"] <= this_sunday.isoformat()]
    next_week = [d for d in available_days if d["date"] > this_sunday.isoformat()]

    first_available = None
    if not this_week and not next_week:
        try:
            wide = engine.search_available_days(
                tenant, knowledge, {**base_data, "preferences": {}}, max_days=1
            )
        except Exception as exc:
            print(f"[flows.booking.show_week_overview] errore ricerca prima disponibilità: {exc!r}")
            return context, SystemResult(success=False, error_code="TECHNICAL_ERROR")
        wide_days = wide.get("available_days") or []
        first_available = wide_days[0] if wide_days else None

    context.conversation.current_step = ConversationStep.SEARCH_AVAILABILITY
    context.conversation.pending_action = PendingAction.PROVIDE_DATE

    shown_days = (this_week[:3] + next_week[:3]) if (this_week or next_week) else []
    context.offered_days = [
        OfferedDay(option=i, date=date.fromisoformat(d["date"]), label=d["label"])
        for i, d in enumerate(shown_days, start=1)
    ]

    return context, SystemResult(
        success=True,
        data={
            "this_week": this_week[:3],
            "next_week": next_week[:3],
            "first_available": first_available,
            "this_week_over": _week_effectively_over(knowledge, tenant, today, this_sunday),
            "time_preference": time_pref,
            "period": period,
        },
    )


def slot_selected(context: ConversationContext, **_ignored) -> tuple[ConversationContext, SystemResult]:
    """Intent SELECT_SLOT -> passo generico: chiede il nome se manca, altrimenti chiede conferma."""
    if find_selected_offered_slot(context) is None:
        return context, SystemResult(success=False, error_code="SLOT_NOT_RECOGNIZED")
    return advance_after_slot_selection(context)


def customer_data_provided(context: ConversationContext, tenant: dict, **_ignored) -> tuple[ConversationContext, SystemResult]:
    """Step: COLLECTING_CUSTOMER_DATA + intent PROVIDE_DATA -> passo generico."""
    return advance_after_customer_data(context, tenant)


def confirm(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    """Intent CONFIRM -> crea davvero l'appuntamento."""
    context = context.model_copy(deep=True)

    offered = find_selected_offered_slot(context)
    if offered is None:
        return context, SystemResult(success=False, error_code="NO_SLOT_SELECTED")

    if not (context.customer.full_name.value or "").strip():
        # Non dovrebbe succedere (il Router arriva qui solo dopo
        # COLLECTING_CUSTOMER_DATA), ma se succede è meglio un errore
        # chiaro che un tentativo di creazione destinato a fallire.
        context.conversation.current_step = ConversationStep.COLLECTING_CUSTOMER_DATA
        return context, SystemResult(success=False, error_code="MISSING_CUSTOMER_NAME")

    collected_data = build_search_collected_data(context)
    collected_data["selected_slot"] = offered_slot_to_engine_dict(offered)
    collected_data["person_name"] = context.customer.full_name.value

    context.conversation.current_step = ConversationStep.EXECUTING

    try:
        booking_res = engine.create_booking(
            tenant=tenant,
            knowledge=knowledge,
            collected_data=collected_data,
            customer={"id": context.customer.id} if context.customer.id else None,
            phone_number=context.customer.phone.value,
        )
    except Exception as exc:
        print(f"[flows.booking.confirm] errore creazione appuntamento: {exc!r}")
        return context, SystemResult(success=False, error_code="TECHNICAL_ERROR")

    result = booking_res.get("result") or {}

    if not result.get("success"):
        error = result.get("error") or "UNKNOWN_ERROR"

        if error == "too_many_names_for_phone":
            # Stesso trattamento del controllo anticipato: stop
            # completo e pulito, non si riprende come se nulla fosse.
            context = reset_for_new_operation(context)
            context.conversation.current_step = ConversationStep.IDLE
            context.conversation.pending_action = PendingAction.NONE
            context.operation = Operation()
            return context, SystemResult(success=False, error_code=error.upper())

        # Torniamo a proporre lo slot: l'utente potrà scegliere un'altra opzione.
        context.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
        context.booking.status = context.booking.status.__class__.FAILED
        return context, SystemResult(success=False, error_code=error.upper(), data=result)

    context.booking.id = result.get("appointment_id")
    context.booking.status = context.booking.status.__class__.CONFIRMED
    context.conversation.current_step = ConversationStep.COMPLETED
    context.conversation.pending_action = PendingAction.NONE
    context.confirmation.required = False

    notify_agenda(tenant["id"])

    return context, SystemResult(success=True, data={"appointment_id": context.booking.id})


def reject_confirmation(context: ConversationContext, **_ignored) -> tuple[ConversationContext, SystemResult]:
    """Intent REJECT -> torna a proporre gli slot già trovati."""
    context = context.model_copy(deep=True)
    context.booking.slot_id = None
    context.confirmation.required = False
    context.confirmation.status = None
    context.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
    context.conversation.pending_action = PendingAction.NONE
    return context, SystemResult(success=True, data={"next": "reask_slot"})
