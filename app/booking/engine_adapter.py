"""
Repository per le conversazioni - con supporto colonna state.
"""

from datetime import datetime, timedelta, timezone
from app.repositories.supabase import get_supabase_client
from app.state.manager import StateManager


def get_or_create_conversation(tenant_id: str, customer_id: str, phone_number: str) -> tuple[dict, bool]:
    """Recupera o crea una conversazione, includendo il campo state."""
    supabase = get_supabase_client()

    result = supabase.table("conversations").select("*").eq(
        "tenant_id", tenant_id
    ).eq("customer_id", customer_id).eq(
        "status", "active"
    ).order("created_at", desc=True).limit(1).execute()

    if result.data:
        conv = result.data[0]
        if "state" not in conv or not conv["state"]:
            conv["state"] = StateManager.initial_state()
        expired = _check_expired(conv)
        if expired:
            close_conversation(conv["id"], "expired")
            return _create_conversation(tenant_id, customer_id, phone_number), True
        return conv, False

    return _create_conversation(tenant_id, customer_id, phone_number), False


def _create_conversation(tenant_id: str, customer_id: str, phone_number: str) -> dict:
    supabase = get_supabase_client()
    result = supabase.table("conversations").insert({
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
    }).execute()
    return result.data[0]


def _check_expired(conversation: dict) -> bool:
    from app.config import Config
    timeout_minutes = Config.CONVERSATION_TIMEOUT_MINUTES
    last_at = conversation.get("last_message_at")
    if not last_at:
        return False
    try:
        last = datetime.fromisoformat(last_at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - last) > timedelta(minutes=timeout_minutes)
    except:
        return False


def close_conversation(conversation_id: str, reason: str = "expired") -> None:
    supabase = get_supabase_client()
    supabase.table("conversations").update({
        "status": "closed",
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "close_reason": reason,
    }).eq("id", conversation_id).execute()


def update_conversation(conversation_id: str, **kwargs) -> None:
    supabase = get_supabase_client()
    kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
    if "last_message_at" not in kwargs:
        kwargs["last_message_at"] = datetime.now(timezone.utc).isoformat()
    supabase.table("conversations").update(kwargs).eq("id", conversation_id).execute()


def append_message(conversation_id: str, role: str, content: str, current_messages: list = None) -> list:
    supabase = get_supabase_client()
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

    supabase.table("conversations").update({
        "recent_messages": messages,
        "last_message_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", conversation_id).execute()

    return messages