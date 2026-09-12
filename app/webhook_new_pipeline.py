"""
Punto di ingresso della NUOVA pipeline dal webhook WhatsApp.

Isolato apposta: main.py fa solo UNA chiamata a should_use_new_pipeline()
e, se True, delega tutto qui e ritorna. Nessun'altra riga di main.py né
di process_messages viene eseguita per queste conversazioni: le due
pipeline sono completamente indipendenti, quindi un baco qui non può
in alcun modo rompere il comportamento esistente per tutti gli altri
utenti.
"""

from __future__ import annotations

from app.ai.format_helpers import format_ack_message
from app.config import Config
from app.context import repository as context_repository
from app.context.models import ConversationStatus
from app.integrations.whatsapp import send_whatsapp_buttons, send_whatsapp_list, send_whatsapp_message
from app.orchestrator import OutgoingMessage, handle_message
from app.repositories.customer import get_or_create_customer, normalize_phone
from app.repositories.tenant import get_tenant_by_whatsapp_number, get_tenant_knowledge

_KNOWLEDGE_TEXT_KEYS = ("services_text", "locations_text", "working_hours_text")


def should_use_new_pipeline(phone: str) -> bool:
    """
    Unico punto di decisione. Vuoto per default (vedi Config): finché
    non viene esplicitamente valorizzato NEW_PIPELINE_TEST_PHONES,
    ritorna sempre False e il comportamento attuale resta invariato.
    """
    if not Config.NEW_PIPELINE_TEST_PHONES:
        return False
    test_phones = {normalize_phone(p) for p in Config.NEW_PIPELINE_TEST_PHONES}
    return normalize_phone(phone) in test_phones


async def _send_sequence(outgoing: OutgoingMessage, phone: str, token: str, phone_id: str) -> None:
    """Invia i messaggi in ordine; bottoni/lista solo sull'ultimo. Ripiega su testo se l'interattivo fallisce."""
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


async def handle_whatsapp_message_new_pipeline(
    phone: str,
    business_phone: str,
    combined_text: str,
) -> None:
    context = None
    conv_row = None
    tenant = None
    # Messaggio di default: usato SOLO se qualcosa va storto prima di
    # arrivare a una risposta vera. Onesto, non tecnico, e lascia
    # sempre una via d'uscita al cliente.
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

        # Ack IMMEDIATO, prima ancora di interpellare AI#1/il motore di
        # ricerca: solo al primo messaggio di una conversazione nuova,
        # così il cliente sa subito che stiamo verificando, invece di
        # aspettare in silenzio mentre facciamo la ricerca vera.
        if context.conversation.status == ConversationStatus.NEW:
            await send_whatsapp_message(phone, format_ack_message(tenant.get("timezone")), token, phone_id)

        knowledge = get_tenant_knowledge(tenant_id) or {}
        knowledge_texts = {key: knowledge.get(key) or "" for key in _KNOWLEDGE_TEXT_KEYS}

        context, outgoing = handle_message(
            context=context,
            message_text=combined_text,
            tenant=tenant,
            knowledge=knowledge,
            knowledge_texts=knowledge_texts,
        )
    except Exception as exc:
        # Qualunque errore imprevisto, in QUALUNQUE fase (non solo
        # nell'interpretazione del messaggio, ma anche nel recupero di
        # cliente/contesto/tenant) non deve mai lasciare l'utente senza
        # risposta. L'errore resta visibile nei log per essere corretto;
        # `outgoing` è già pronto con il messaggio onesto definito sopra.
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
