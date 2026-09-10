"""
Persistenza del ConversationContext tipizzato.

NON duplica la logica già esistente in app/repositories/conversation.py
(normalizzazione telefono, timeout, chiusura conversazioni scadute):
la riusa cosi' com'e' per trovare/creare la riga in "conversations".
Qui ci occupiamo SOLO della nuova colonna "context_v2": leggerla,
scriverla, e inizializzarla quando è ancora vuota (prima volta che
questa conversazione passa dalla nuova pipeline).
"""

from __future__ import annotations

from app.context.models import Conversation, ConversationContext
from app.repositories import conversation as conversation_repo
from app.supabase_client import get_supabase


def _parse_context(conversation_row: dict) -> ConversationContext | None:
    raw = conversation_row.get("context_v2")
    if not raw:
        return None
    try:
        return ConversationContext.model_validate(raw)
    except Exception as e:
        # Un context_v2 corrotto/non più compatibile con lo schema non deve
        # far crashare la conversazione: meglio ripartire da zero (e loggare
        # rumorosamente, perché non dovrebbe succedere) che bloccare l'utente.
        print(f"[context_repository] context_v2 non valido per conv {conversation_row.get('id')}: {e}")
        return None


def get_or_create_context(
    tenant_id: str,
    customer_id: str,
    phone_number: str,
) -> tuple[ConversationContext, dict, bool]:
    """
    Ritorna (context, conversation_row, expired).

    Riusa get_or_create_conversation esistente per trovare/creare la
    riga; se la riga non ha ancora un context_v2 (conversazione nuova, o
    mai passata dalla nuova pipeline), ne crea uno vuoto agganciato allo
    stesso conversation_id.
    """
    conv_row, expired = conversation_repo.get_or_create_conversation(
        tenant_id, customer_id, phone_number
    )

    context = _parse_context(conv_row)
    if context is None or expired:
        context = ConversationContext(conversation=Conversation(id=str(conv_row["id"])))

    if not context.customer.id:
        # Serve a identify_target (reschedule/cancel) per cercare gli
        # appuntamenti del cliente. Idempotente: non tocca nient'altro.
        context.customer.id = customer_id

    return context, conv_row, expired


def save_context(conversation_id: str, context: ConversationContext) -> None:
    """
    Salva SOLO context_v2. Le colonne della pipeline attuale
    (workflow/step/collected_data/recent_messages) non vengono toccate
    da questa funzione: le due pipeline restano indipendenti finché non
    decidiamo il cutover definitivo.
    """
    sb = get_supabase()
    sb.table("conversations").update(
        {"context_v2": context.model_dump(mode="json")}
    ).eq("id", conversation_id).execute()
