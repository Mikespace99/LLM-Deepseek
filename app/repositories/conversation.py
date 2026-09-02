cat > app/repositories/conversation.py << 'EOF'
"""
Repository per le conversazioni.
Stile identico a appointment.py.
"""

from datetime import datetime, timezone
from app.supabase_client import get_supabase
from app.state.manager import StateManager


def get_or_create_conversation(tenant_id: str, customer_id: str, phone_number: str) -> dict:
    """
    Recupera la conversazione attiva o ne crea una nuova.
    """
    sb = get_supabase()
    
    # Cerca conversazione attiva
    res = (
        sb.table("conversations")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("customer_id", customer_id)
        .eq("status", "active")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    
    if res.data:
        conv = res.data[0]
        if "state" not in conv or not conv["state"]:
            conv["state"] = StateManager.initial_state()
        return conv
    
    # Crea nuova conversazione
    payload = {
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "phone_number": phone_number,
        "status": "active",
        "workflow": "idle",
        "step": "none",
        "state": StateManager.initial_state(),
        "collected_data": {},
        "recent_messages": [],
        "last_message_at": datetime.now(timezone.utc).isoformat(),
    }
    res = sb.table("conversations").insert(payload).execute()
    return res.data[0]


def update_conversation(conversation_id: str, **kwargs) -> None:
    """
    Aggiorna una conversazione.
    """
    sb = get_supabase()
    kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
    if "last_message_at" not in kwargs:
        kwargs["last_message_at"] = datetime.now(timezone.utc).isoformat()
    
    sb.table("conversations").update(kwargs).eq("id", conversation_id).execute()


def append_message(conversation_id: str, role: str, content: str, current_messages: list = None) -> list:
    """
    Aggiunge un messaggio alla conversazione.
    """
    sb = get_supabase()
    from app.config import Config
    max_messages = Config.MAX_RECENT_MESSAGES

    messages = current_messages or []
    messages.append({
        "role": role,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat()
    })
    if len(messages) > max_messages:
        messages = messages[-max_messages:]

    sb.table("conversations").update({
        "recent_messages": messages,
        "last_message_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", conversation_id).execute()

    return messages


def close_conversation(conversation_id: str, reason: str = "expired") -> None:
    """
    Chiude una conversazione.
    """
    sb = get_supabase()
    sb.table("conversations").update({
        "status": "closed",
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "close_reason": reason,
    }).eq("id", conversation_id).execute()
EOF
