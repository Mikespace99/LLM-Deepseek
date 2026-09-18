from datetime import datetime, timezone, timedelta
from app.supabase_client import get_supabase
from app.config import Config
from app.constants import WORKFLOW_IDLE, STEP_NONE
from app.repositories.customer import normalize_phone


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value) -> datetime | None:
    """
    Parsing robusto di timestamp da Supabase.
    Accetta:
    - stringhe ISO con Z / +00:00
    - stringhe senza timezone (le tratta come UTC)
    - oggetti datetime
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None

def get_active_conversation(tenant_id: str, phone_number: str) -> dict | None:
    """Ritorna solo una conversazione realmente attiva e non scaduta."""
    phone_number = normalize_phone(phone_number)
    sb = get_supabase()

    result = (
        sb.table("conversations")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("phone_number", phone_number)
        .eq("status", "active")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not result.data:
        return None

    conv = result.data[0]
    last_dt = _parse_ts(conv.get("last_message_at"))

    if last_dt:
        timeout = timedelta(minutes=Config.CONVERSATION_TIMEOUT_MINUTES)

        if _now() - last_dt > timeout:
            close_conversation(conv["id"], reason="timeout")
            return None

    return conv

def create_conversation(tenant_id: str, customer_id: str, phone_number: str) -> dict:
    phone_number = normalize_phone(phone_number)
    sb = get_supabase()
    now = _now().isoformat()
    timeout_at = (_now() + timedelta(minutes=Config.CONVERSATION_TIMEOUT_MINUTES)).isoformat()

    insert = (
        sb.table("conversations")
        .insert({
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "phone_number": phone_number,
            "status": "active",
            "workflow": WORKFLOW_IDLE,
            "step": STEP_NONE,
            "collected_data": {},
            "recent_messages": [],
            "retry_count": 0,
            "last_message_at": now,
            "timeout_at": timeout_at,
            "created_at": now,
        })
        .execute()
    )
    return insert.data[0]

def get_or_create_conversation(
    tenant_id: str,
    customer_id: str,
    phone_number: str,
) -> tuple[dict, bool]:
    """
    Restituisce:
        (conversation, expired)

    expired=True significa che una precedente conversazione attiva
    è appena scaduta ed è stata chiusa.
    """
    phone_number = normalize_phone(phone_number)

    sb = get_supabase()

    result = (
        sb.table("conversations")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("phone_number", phone_number)
        .eq("status", "active")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if result.data:
        conv = result.data[0]

        last_dt = _parse_ts(conv.get("last_message_at"))

        if last_dt:
            timeout = timedelta(
                minutes=Config.CONVERSATION_TIMEOUT_MINUTES
            )

            if _now() - last_dt > timeout:
                # Chiudi definitivamente la vecchia conversazione
                close_conversation(conv["id"], reason="timeout")

                # Crea una nuova conversazione vuota
                new_conv = create_conversation(
                    tenant_id,
                    customer_id,
                    phone_number,
                )

                return new_conv, True

        return conv, False

    # Nessuna conversazione precedente
    return create_conversation(
        tenant_id,
        customer_id,
        phone_number,
    ), False

def close_conversation(conversation_id: str, reason: str = "completed"):
    sb = get_supabase()

    sb.table("conversations").update({
        "status": "closed",
        "closed_at": _now().isoformat(),
        "close_reason": reason,
    }).eq("id", conversation_id).execute()


def update_conversation(conversation_id: str, **fields):
    """Aggiorna uno o più campi della conversazione."""
    sb = get_supabase()
    fields["last_message_at"] = _now().isoformat()
    fields["timeout_at"] = (
        _now() + timedelta(minutes=Config.CONVERSATION_TIMEOUT_MINUTES)
    ).isoformat()
    sb.table("conversations").update(fields).eq("id", conversation_id).execute()


def append_message(
    conversation_id: str,
    role: str,
    content: str,
    current_messages: list | None = None,
):
    """
    Aggiunge un messaggio a recent_messages e tiene solo gli ultimi N.
    """
    messages = list(current_messages or [])
    messages.append({
        "role": role,
        "content": content,
        "at": _now().isoformat(),
    })
    messages = messages[-Config.MAX_RECENT_MESSAGES:]
    update_conversation(conversation_id, recent_messages=messages)
    return messages
