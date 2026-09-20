"""
Adattatore condiviso tra il ConversationContext tipizzato e le funzioni
esistenti in app/booking/engine.py (che lavorano ancora su dict liberi,
"collected_data").

Questo modulo esiste apposta per essere condiviso da PIÙ domini: la
ricerca disponibilità e la selezione di uno slot funzionano allo stesso
modo sia per una prenotazione nuova (booking.py) sia per uno
spostamento (reschedule.py, quando lo aggiungeremo) - cambia solo cosa
succede dopo la conferma. Non duplicarlo nei singoli flow.

Nota: NON tocca app/booking/engine.py. È un ponte a senso unico,
cosi' l'engine esistente (già collaudato) resta l'unica fonte di verità
per il calcolo di slot e la scrittura a DB, finché non lo migreremo
anch'esso.
"""

from __future__ import annotations

from app.booking import engine
from app.repositories import appointment as appointment_repo
from app.context.models import (
    AvailableSlot,
    Booking,
    Confirmation,
    ContextValue,
    ConversationContext,
    ConversationStep,
    Operation,
    OfferedSlot,
    PendingAction,
    SearchCriteria,
    SystemResult,
)


def abandon_current_request(context: ConversationContext, **_ignored) -> tuple[ConversationContext, SystemResult]:
    """
    L'utente vuole "annullare" mentre una richiesta è ancora in corso
    (nessun appuntamento reale ancora creato/spostato): non è la stessa
    cosa di cancellare un appuntamento già esistente (quello è un
    dominio a parte, non ancora costruito). Qui basta tornare a uno
    stato neutro - nessuna scrittura sul database, perché non c'è
    ancora nulla di reale da toccare.
    """
    context = reset_for_new_operation(context)
    context.conversation.current_step = ConversationStep.IDLE
    context.conversation.pending_action = PendingAction.NONE
    context.operation = Operation()
    context.conversation.current_operation = context.operation.type
    return context, SystemResult(success=True, data={"next": "abandoned"})


def reset_for_new_operation(context: ConversationContext) -> ConversationContext:
    """
    Ripulisce lo stato "per-operazione" (ricerca, slot proposti,
    prenotazione, conferma...) quando si riparte da zero dopo che
    un'operazione precedente si era già conclusa (COMPLETED) - così
    una nuova richiesta non eredita per sbaglio criteri/slot della
    prenotazione precedente. NON tocca l'identità del cliente (nome,
    telefono, id) né context.operation.type/status, già impostati
    correttamente dal Context Manager per QUESTO nuovo giro.
    """
    context = context.model_copy(deep=True)
    context.search = SearchCriteria()
    context.service = ContextValue()
    context.offered_slots = []
    context.offered_days = []
    context.booking = Booking()
    context.confirmation = Confirmation()
    context.appointments = []
    context.selected_appointment_id = None
    context.operation.target_appointment_id = None
    return context


def has_search_criteria(context: ConversationContext) -> bool:
    s = context.search
    if s.preferred_weekday:
        return True
    if s.preferred_date:
        return True
    # Range o giorno esplicito (es. inizio ottobre → 1..10)
    if s.date_from and s.date_to:
        return True
    if s.period in ("today", "tomorrow", "this_week", "next_week", "any"):
        return True
    if s.time_preference or s.preferred_time or s.time_from:
        return True
    return False


def search_or_ask_preference(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    """
    Punto di ingresso condiviso per "voglio cercare disponibilità":
    non lancia MAI una ricerca alla cieca. Se non c'è ancora nessun
    criterio, chiede la preferenza invece di cercare su una finestra
    aperta di default - identico per BOOK e RESCHEDULE.
    """
    if not has_search_criteria(context):
        context = context.model_copy(deep=True)
        context.conversation.current_step = ConversationStep.SEARCH_AVAILABILITY
        context.conversation.pending_action = PendingAction.PROVIDE_DATE
        return context, SystemResult(success=True, data={"next": "ask_preference"})

    return run_availability_search(context, tenant, knowledge)


def reject_offered_slots(context: ConversationContext, **_ignored) -> tuple[ConversationContext, SystemResult]:
    """
    Intent REJECT mentre ci sono slot proposti (e non siamo in fase di
    conferma di un singolo slot).

    1. Aggiunge gli slot appena mostrati alla blacklist (excluded_slots)
       così la prossima ricerca non li ripropone.
    2. Svuota la lista offerta.
    3. Torna in SEARCH_AVAILABILITY e chiede preferenze alternative
       (data e/o orario) in modo elastico.
    """
    context = context.model_copy(deep=True)

    # Blacklist: gli slot appena rifiutati non devono riapparire subito.
    existing = set(context.search.excluded_slots or [])
    for offered in context.offered_slots:
        existing.add(offered.slot.id)
    context.search.excluded_slots = list(existing)

    context.offered_slots = []
    context.booking.slot_id = None
    context.confirmation.required = False
    context.confirmation.status = None
    context.confirmation.confirmation_type = None

    context.conversation.current_step = ConversationStep.SEARCH_AVAILABILITY
    context.conversation.pending_action = PendingAction.PROVIDE_DATE

    return context, SystemResult(
        success=True,
        data={"next": "ask_alternative_preference"},
    )


def run_availability_search(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    """
    Cerca disponibilità e aggiorna il contesto. Identica per BOOK e
    RESCHEDULE: cambia solo cosa succede DOPO la conferma (create vs
    update), non come si trovano gli slot.
    """
    context = context.model_copy(deep=True)
    collected_data = build_search_collected_data(context)

    try:
        booking_res = engine.search_availability(tenant=tenant, knowledge=knowledge, collected_data=collected_data)
    except Exception as exc:
        print(f"[flows.common.run_availability_search] errore ricerca disponibilità: {exc!r}")
        return context, SystemResult(success=False, error_code="TECHNICAL_ERROR")

    candidate_slots = booking_res.get("candidate_slots") or []
    result = booking_res.get("result") or {}

    if not candidate_slots:
        previous_alternatives = bool(context.offered_slots)  # non li cancelliamo: restano validi
        error_code = "NO_SLOTS_FOUND"
        if result.get("is_studio_closed"):
            error_code = "STUDIO_CLOSED"
        elif result.get("is_studio_full"):
            error_code = "STUDIO_FULL"
        return context, SystemResult(
            success=False, error_code=error_code,
            data={**result, "previous_alternatives": previous_alternatives},
        )

    context.offered_slots = slots_to_offered(candidate_slots)
    context.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
    context.conversation.pending_action = PendingAction.NONE

    return context, SystemResult(
        success=True,
        # Solo il conteggio, MAI le date/orari: l'AI non deve poter
        # riscrivere la lista con le sue parole. La lista vera la
        # mostra format_helpers.py in modo deterministico.
        data={"slots_count": len(context.offered_slots)},
    )


def build_search_collected_data(context: ConversationContext) -> dict:
    """
    Converte SearchCriteria + service nel dict "preferences" atteso dall'engine.
    - excluded_slots: blacklist persistente (search) + slot appena proposti
    - after_time: se ci sono slot già mostrati, cerca solo orari successivi
    """
    search = context.search

    # 1) blacklist già salvata nel contesto (es. da REJECT)
    excluded = set(search.excluded_slots or [])

    # 2) aggiungi gli slot attualmente proposti (caso "più tardi")
    for o in context.offered_slots:
        excluded.add(f"{o.slot.date.isoformat()}_{o.slot.time.strftime('%H:%M')}")

    after_time = None
    if context.offered_slots:
        after_time = max(o.slot.time for o in context.offered_slots).strftime("%H:%M")

    return {
        "service": context.service.value,
        "location_id": context.professional.location_id if context.professional else None,
        "preferences": {
            "period": search.period,
            "weekday": search.preferred_weekday,
            "week_part": search.week_part,
            "date_from": search.date_from.isoformat() if search.date_from else None,
            "date_to": search.date_to.isoformat() if search.date_to else None,
            "date": search.preferred_date.isoformat() if search.preferred_date else None,
            "time_preference": search.time_preference,
            "exact_time": search.preferred_time.strftime("%H:%M") if search.preferred_time else None,
            "excluded_slots": list(excluded),
            "excluded_dates": [d.isoformat() for d in (search.excluded_dates or [])],
            "after_time": after_time,
        },
    }



def slots_to_offered(candidate_slots: list[dict]) -> list[OfferedSlot]:
    """
    Numera gli slot trovati dall'engine (1, 2, 3...) cosi' che "il
    secondo" o "2" nel messaggio dell'utente sia deterministicamente
    risolvibile dal Context Manager (vedi _apply_slot_selection).

    Limitati a 3: cosi' possiamo mostrarli SEMPRE come bottoni WhatsApp
    (visibili subito in chat), senza mai dover ricorrere alla lista
    nascosta (che richiede un tap in più per aprirsi).
    """
    offered = []
    for i, raw in enumerate(candidate_slots[:3], start=1):
        slot_id = f"{raw['date']}_{raw['time']}"
        offered.append(
            OfferedSlot(
                option=i,
                slot=AvailableSlot.model_validate(
                    {"id": slot_id, "date": raw["date"], "time": raw["time"]}
                ),
            )
        )
    return offered


def offered_slot_to_engine_dict(offered: OfferedSlot) -> dict:
    """Ricostruisce la forma dict ("selected_slot") che create_booking si aspetta, a partire dallo slot scelto."""
    date_str = offered.slot.date.isoformat()
    time_str = offered.slot.time.strftime("%H:%M")
    return {
        "date": date_str,
        "time": time_str,
        "datetime": f"{date_str}T{time_str}",
    }


def find_selected_offered_slot(context: ConversationContext) -> OfferedSlot | None:
    if not context.booking.slot_id:
        return None
    return next(
        (o for o in context.offered_slots if o.slot.id == context.booking.slot_id),
        None,
    )


def advance_after_slot_selection(context: ConversationContext) -> tuple[ConversationContext, SystemResult]:
    """
    Passo generico, riusabile da qualunque dominio: dopo che l'utente ha
    scelto uno slot (gia' registrato in context.booking.slot_id dal
    Context Manager), si chiede SEMPRE il nome dell'intestatario prima
    di procedere alla conferma - anche se il cliente è già noto da una
    prenotazione precedente, cosi' c'e' sempre un passaggio esplicito
    di conferma tra "slot scelto" e "prenotazione creata".
    """
    context = context.model_copy(deep=True)

    context.conversation.current_step = ConversationStep.COLLECTING_CUSTOMER_DATA
    context.conversation.pending_action = PendingAction.PROVIDE_NAME
    context.confirmation.required = False
    return context, SystemResult(success=True, data={"next": "ask_name"})


def advance_after_customer_data(
    context: ConversationContext, tenant: dict
) -> tuple[ConversationContext, SystemResult]:
    """
    Una volta ottenuto il nome, si controlla SUBITO se è una terza
    persona diversa sullo stesso numero (non alla fine, dopo che il
    cliente ha già scelto slot e sta per confermare): inutile fargli
    fare tutto il percorso per poi rifiutarlo solo all'ultimo passo.

    Se rifiutato, è uno STOP completo e definitivo - non si torna a
    riproporre slot o a continuare come se nulla fosse: la richiesta
    corrente viene abbandonata, esattamente come un annullamento.
    """
    context = context.model_copy(deep=True)

    full_name = (context.customer.full_name.value or "").strip()
    if not full_name:
        # Il dato non e' ancora arrivato (il messaggio non lo conteneva):
        # restiamo nello stesso step, il Router ripropone la domanda.
        return context, SystemResult(success=False, error_code="MISSING_CUSTOMER_NAME")

    if context.customer.id:
        try:
            existing_names = appointment_repo.list_person_names_for_customer(tenant["id"], context.customer.id)
        except Exception as exc:
            print(f"[flows.common.advance_after_customer_data] errore lettura nomi esistenti: {exc!r}")
            existing_names = []

        is_new_name = full_name.lower() not in {n.lower() for n in existing_names}
        if is_new_name and len(existing_names) >= 2:
            context = reset_for_new_operation(context)
            context.conversation.current_step = ConversationStep.IDLE
            context.conversation.pending_action = PendingAction.NONE
            context.operation = Operation()
            return context, SystemResult(success=False, error_code="TOO_MANY_NAMES_FOR_PHONE")

    context.conversation.current_step = ConversationStep.WAITING_FOR_CONFIRMATION
    context.conversation.pending_action = PendingAction.CONFIRM
    context.confirmation.required = True
    context.confirmation.confirmation_type = "booking"
    return context, SystemResult(success=True, data={"next": "ask_confirmation"})
