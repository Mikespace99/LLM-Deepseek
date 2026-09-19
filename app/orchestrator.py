"""
ORCHESTRATOR

Collega, in un'unica funzione, il flusso lineare discusso:

  USER -> AI#1 -> Context Manager -> Router -> Business Logic
        -> SystemResult -> response_type_resolver -> AI#2 -> USER

Isolato: non sostituisce (ancora) il webhook reale in main.py. Serve a
dimostrare e testare che tutti i pezzi nuovi si incastrino correttamente
prima del collegamento definitivo.

Il saluto orario NON viene più aggiunto qui: il messaggio di "un attimo,
verifico" viene inviato immediatamente da webhook.py, PRIMA
di chiamare handle_message - così l'utente riceve un segnale subito,
non solo insieme alla risposta finale (che può richiedere una ricerca).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.ai.context_summary import build_ai1_input
from app.ai.format_helpers import (
    build_appointment_buttons,
    build_offered_slots_rows,
    format_appointment_selection_question,
    format_appointment_target_question,
    format_booking_summary,
    format_week_overview,
    yes_no_buttons,
)
from app.ai.interpreter import run_ai1_interpreter
from app.ai.responder import compose_final_message, run_ai2_responder
from app.ai.response_type_resolver import resolve_response_type
from app.context.context_manager import apply_ai1_result
from app.context.models import ConversationContext, Intent, Message, ResponseType, SystemResult
from app.flows.common import find_selected_offered_slot
from app.router.intent_router import route
from app.utils.it_dates import today_in_tz

# Intent che non richiedono alcuna business logic / accesso al DB:
# saltano il Router e vanno dritti alla risposta.
_CHITCHAT_INTENTS = {Intent.GREETING, Intent.THANKS, Intent.ASK_INFORMATION}

# response_type per cui proponiamo bottoni Sì/No invece di aspettare testo libero.
_YES_NO_RESPONSE_TYPES = {ResponseType.ASK_CONFIRMATION, ResponseType.CONFIRM_APPOINTMENT_TARGET}


@dataclass
class OutgoingMessage:
    """
    Cosa mandare all'utente: una SEQUENZA di messaggi di testo, inviati
    in ordine (quasi sempre uno solo; la panoramica settimanale può
    produrne due). `buttons`/`list_rows` si attaccano solo all'ULTIMO
    messaggio della sequenza, quello su cui l'utente deve agire.
    """
    texts: list[str]
    buttons: list[tuple[str, str]] | None = None
    list_button_label: str | None = None
    list_rows: list[dict] = field(default_factory=list)


def handle_message(
    context: ConversationContext,
    message_text: str,
    tenant: dict,
    knowledge: dict,
    knowledge_texts: dict | None = None,
    precomputed_ai1=None,
) -> tuple[ConversationContext, OutgoingMessage]:
    # 1. AI#1: interpreta (solo il blocco minimo, non l'intero context).
    # Se il chiamante l'ha già classificato (es. per decidere se inviare
    # l'ack prima di iniziare), riusiamo quello - non lo richiediamo due volte.
    if precomputed_ai1 is not None:
        ai1 = precomputed_ai1
    else:
        ai1 = run_ai1_interpreter(message_text, build_ai1_input(context), tenant.get("timezone"))
    print(f"[DIAGNOSTICA AI1] intent={ai1.intent} entities={ai1.entities} needs_clarification={ai1.needs_clarification} reason={ai1.clarification_reason}")

    # 2. Context Manager: aggiorna lo stato
    context = apply_ai1_result(context, ai1, message_text)

    # 3. Router: decide ed esegue la business logic (skip per le chiacchiere)
    if ai1.needs_clarification:
        system_result = SystemResult(success=False, error_code="NEEDS_CLARIFICATION")
    elif ai1.intent in _CHITCHAT_INTENTS:
        system_result = SystemResult(success=True)
    else:
        context, system_result = route(context, tenant=tenant, knowledge=knowledge)

    # 4. Determina il tipo di risposta (deterministico, non lo decide l'AI)
    response_type = resolve_response_type(ai1.intent, system_result, context)
    if ai1.needs_clarification:
        response_type = ResponseType.ASK_CLARIFICATION

    # 5. Costruzione del messaggio, testo (o sequenza) + eventuali bottoni/lista.
    if response_type == ResponseType.REQUEST_ABANDONED:
        # Testo fisso e deterministico: non serve l'AI per dire
        # "va bene, ho annullato" - zero rischio, zero costo.
        outgoing = OutgoingMessage(
            texts=["Va bene, ho annullato la richiesta in corso. Se vorrà prenotare in futuro sono a disposizione."]
        )
    elif response_type == ResponseType.CONFIRM_APPOINTMENT_TARGET and context.appointments:
        # Caso speciale: la domanda contiene una data/ora/nome reali,
        # quindi la componiamo in modo deterministico invece di farla
        # scrivere ad AI#2 - stesso motivo per cui non le facciamo mai
        # scrivere gli slot proposti. Il nome è SEMPRE quello salvato
        # su questo specifico appuntamento (person_name), mai il nome
        # generico del cliente: lo stesso numero può avere appuntamenti
        # intestati a persone diverse.
        outgoing = OutgoingMessage(
            texts=[format_appointment_target_question(context.appointments[0], context.appointments[0].person_name)],
            buttons=yes_no_buttons(),
        )
    elif response_type == ResponseType.ASK_APPOINTMENT and len(context.appointments) > 1:
        # Caso speciale, stesso principio di CONFIRM_APPOINTMENT_TARGET:
        # date/ore reali, mai scritte dall'AI. Qui però sono PIÙ di un
        # appuntamento, quindi bottoni per la scelta invece di sì/no.
        outgoing = OutgoingMessage(
            texts=[format_appointment_selection_question(context.appointments)],
            buttons=build_appointment_buttons(context.appointments),
        )
    elif response_type == ResponseType.SHOW_WEEK_OVERVIEW:
        # Stesso principio: giorni/fasce orarie reali, mai scritti
        # dall'AI. Sequenza di 1-2 messaggi (questa settimana / prossima).
        today = today_in_tz(tenant.get("timezone"))
        outgoing = OutgoingMessage(texts=format_week_overview(system_result.data, today))
    elif response_type in (ResponseType.BOOKING_CONFIRMED, ResponseType.RESCHEDULE_CONFIRMED) and context.booking.slot_id:
        # Riepilogo finale: nome + data/ora, MAI l'ID tecnico
        # dell'appuntamento (non significa nulla per il cliente).
        # Per RESCHEDULE il nome è quello dell'appuntamento spostato
        # (person_name), non necessariamente quello dell'attuale
        # interlocutore; per una nuova prenotazione è il nome appena
        # fornito in questa conversazione.
        offered = find_selected_offered_slot(context)
        if offered:
            is_reschedule = response_type == ResponseType.RESCHEDULE_CONFIRMED
            verb = "spostato" if is_reschedule else "fissato"
            name = (
                (context.appointments[0].person_name if context.appointments else None)
                if is_reschedule
                else context.customer.full_name.value
            )
            outgoing = OutgoingMessage(texts=[format_booking_summary(name, offered, verb)])
        else:
            outgoing = OutgoingMessage(texts=["L'appuntamento è stato confermato con successo."])
    else:
        # Solo i messaggi dell'UTENTE, mai le risposte precedenti del bot:
        # quelle potevano contenere liste di slot scritte in un turno precedente,
        # con il rischio che AI#2 le "riciclasse" invece di scrivere un testo
        # nuovo e corretto.
        history_text = "\n".join(
            m.text for m in context.memory.recent_messages[-6:] if m.role == "user"
        )
        ai2 = run_ai2_responder(
            response_type=response_type,
            system_result=system_result,
            message_text=message_text,
            history_text=history_text,
            knowledge_texts=knowledge_texts,
            operation_type=context.operation.type.value,
        )

        if response_type == ResponseType.SHOW_AVAILABILITY and context.offered_slots:
            # Max 3 slot sempre garantiti (slots_to_offered li limita):
            # bottoni visibili subito in chat, mai la lista nascosta.
            rows = build_offered_slots_rows(context.offered_slots)
            slot_buttons = [(r["id"], r["title"]) for r in rows]
            outgoing = OutgoingMessage(texts=[ai2.message], buttons=slot_buttons)
        elif response_type in _YES_NO_RESPONSE_TYPES:
            outgoing = OutgoingMessage(texts=[ai2.message], buttons=yes_no_buttons())
        else:
            outgoing = OutgoingMessage(texts=[compose_final_message(ai2, context)])

    context.memory.recent_messages.append(
        Message(
            role="assistant",
            text="\n".join(outgoing.texts),
            timestamp=context.conversation.timeout.last_activity_at,
        )
    )

    return context, outgoing
