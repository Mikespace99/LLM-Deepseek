"""
Webhook WhatsApp – pipeline nuova.
"""

from __future__ import annotations

from app.ai.context_summary import build_ai1_input
from app.ai.format_helpers import format_ack_message
from app.ai.interpreter import run_ai1_interpreter
from app.config import Config
from app.context import repository as context_repository
from app.context.models import ConversationStatus, Intent
from app.integrations.whatsapp import send_whatsapp_buttons, send_whatsapp_list, send_whatsapp_message
from app.orchestrator import OutgoingMessage, handle_message
from app.repositories.customer import get_or_create_customer, normalize_phone
from app.repositories.tenant import get_tenant_by_whatsapp_number, get_tenant_knowledge

_KNOWLEDGE_TEXT_KEYS = ("services_text", "locations_text", "working_hours_text")

# Intent che non richiedono verifica / ricerca.
_CHITCHAT_INTENTS_NO_ACK = {Intent.GREETING, Intent.THANKS}

# Intent abbastanza chiari da far partire subito il flusso operativo.
_CLEAR_INTENTS = {
    Intent.BOOK,
    Intent.RESCHEDULE,
    Intent.CANCEL,
    Intent.CHECK_APPOINTMENT,
    Intent.ASK_INFORMATION,
}

_MENU_BUTTONS = [
    ("book", "Voglio prenotare"),
    ("reschedule", "Voglio spostare"),
    ("cancel", "Voglio cancellare"),
]


def _build_presentation_text(tenant: dict, knowledge: dict | None = None) -> str:
    """
    Presentazione breve e generica (medico, avvocato, ingegnere, ...).
    Usa business_name + specialty come "professione/titolo" libero.
    """
    info = tenant.get("info") or {}

    name = (
        info.get("doctor_name")
        or info.get("professional_name")
        or info.get("display_name")
        or tenant.get("business_name")
        or tenant.get("name")
        or "lo studio"
    )

    # Campo generico: "Dermatologo", "Avvocato", "Ingegnere"...
    profession = (
        tenant.get("specialty")
        or info.get("profession")
        or info.get("specialty")
        or ""
    ).strip()

    city = (info.get("city") or info.get("studio_city") or "").strip()
    if not city and knowledge:
        locations = knowledge.get("locations") or []
        if locations:
            city = (locations[0].get("city") or "").strip()

    if profession:
        line1 = f"Chat ufficiale per la gestione appuntamenti di {name}, {profession}."
    else:
        line1 = f"Chat ufficiale per la gestione appuntamenti di {name}."

    line2 = f"Studio a {city}." if city else "Come posso aiutarla?"
    return f"{line1}\n{line2}"


async def _send_sequence(outgoing: OutgoingMessage, phone: str, token: str, phone_id: str) -> None:
    """Invia i messaggi in ordine; bottoni/lista solo sull'ultimo."""
    texts = outgoing.texts or [""]
    last_index = len(texts) - 1

    for i, text in enumerate(texts):
        is_last = i == last_index
        sent = None
        if is_last and outgoing.list_rows:
            sent = await send_whatsapp_list(
                phone, text, outgoing.list_button_label or "Scegli", outgoing.list_rows, token, phone_id
            )
        elif is_last and outgoing.buttons:
            sent = await send_whatsapp_buttons(phone, text, outgoing.buttons, token, phone_id)
        else:
            sent = await send_whatsapp_message(phone, text, token, phone_id)

        if sent is None and is_last and (outgoing.list_rows or outgoing.buttons):
            print("[new_pipeline] invio interattivo fallito, ripiego su testo semplice")
            await send_whatsapp_message(phone, text, token, phone_id)


async def handle_whatsapp_message(
    phone: str,
    business_phone: str,
    combined_text: str,
) -> None:
    context = None
    conv_row = None
    tenant = None

    outgoing = OutgoingMessage(
        texts=[
            "Non so rispondere su questo punto: deve chiedere direttamente allo studio. "
            "Se ha altre richieste sono qui, altrimenti la saluto."
        ]
    )

    try:
        tenant = get_tenant_by_whatsapp_number(business_phone)
        if not tenant:
            print(f"[new_pipeline] tenant non trovato per {business_phone}, messaggio ignorato")
            return

        wa_info = tenant.get("info") or {}
        token = wa_info.get("access_token") or Config.WHATSAPP_TOKEN
        phone_id = wa_info.get("phone_number_id") or Config.WHATSAPP_PHONE_NUMBER_ID

        tenant_id = tenant["id"]
        customer = get_or_create_customer(tenant_id, phone)

        context, conv_row, expired = context_repository.get_or_create_context(
            tenant_id, customer, phone
        )

        # ============================================================
        # Conversazione scaduta per timeout
        # ============================================================
        if expired:
            outgoing = OutgoingMessage(
                texts=[
                    "La richiesta precedente è scaduta perché è passato troppo tempo.\n\n"
                    "Cosa desidera fare adesso?"
                ],
                buttons=_MENU_BUTTONS,
            )
            try:
                context_repository.save_context(conv_row["id"], context)
            except Exception as save_err:
                print(f"[new_pipeline] salvataggio context fallito (expired): {save_err}")

            await _send_sequence(outgoing, phone, token, phone_id)
            return

        knowledge = get_tenant_knowledge(tenant_id) or {}
        precomputed_ai1 = None
        is_new = context.conversation.status == ConversationStatus.NEW

        # ============================================================
        # Primo messaggio di una nuova conversazione
        # ============================================================
        if is_new:
            precomputed_ai1 = run_ai1_interpreter(
                combined_text, build_ai1_input(context), tenant.get("timezone")
            )
            presentation = _build_presentation_text(tenant, knowledge)

            intent_clear = (
                precomputed_ai1.intent in _CLEAR_INTENTS
                and not precomputed_ai1.needs_clarification
            )

            if intent_clear:
                # Presentazione + ack, poi flusso normale
                await send_whatsapp_message(phone, presentation, token, phone_id)
                await send_whatsapp_message(
                    phone,
                    format_ack_message(tenant.get("timezone")),
                    token,
                    phone_id,
                )
            else:
                # Presentazione + menu, stop
                outgoing = OutgoingMessage(
                    texts=[presentation + "\n\nCosa desidera fare?"],
                    buttons=_MENU_BUTTONS,
                )
                try:
                    context_repository.save_context(conv_row["id"], context)
                except Exception as save_err:
                    print(f"[new_pipeline] salvataggio context fallito (menu): {save_err}")

                await _send_sequence(outgoing, phone, token, phone_id)
                return

        knowledge_texts = {key: knowledge.get(key) or "" for key in _KNOWLEDGE_TEXT_KEYS}

        context, outgoing = handle_message(
            context=context,
            message_text=combined_text,
            tenant=tenant,
            knowledge=knowledge,
            knowledge_texts=knowledge_texts,
            precomputed_ai1=precomputed_ai1,
        )

    except Exception as exc:
        print(f"[new_pipeline] ERRORE non gestito: {exc!r}")

    if context is not None and conv_row is not None:
        try:
            context_repository.save_context(conv_row["id"], context)
        except Exception as save_err:
            print(f"[new_pipeline] salvataggio context fallito: {save_err}")

    wa_info = (tenant or {}).get("info") or {}
    token = wa_info.get("access_token") or Config.WHATSAPP_TOKEN
    phone_id = wa_info.get("phone_number_id") or Config.WHATSAPP_PHONE_NUMBER_ID

    try:
        await _send_sequence(outgoing, phone, token, phone_id)
    except Exception as send_err:
        print(f"[new_pipeline] invio WhatsApp fallito: {send_err}")
