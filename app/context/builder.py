"""
Costruisce il Context ufficiale che viaggia tra Backend e n8n.
Versione con knowledge strutturata (sedi, orari, buffer, chiusure).
"""

from datetime import datetime, timezone


def build_context(
    tenant: dict,
    customer: dict,
    conversation: dict,
    message: dict,
    services: list | None = None,
    working_hours: list | None = None,
    knowledge: dict | None = None,
) -> dict:
    """
    Context completo.
    `knowledge` ha priorità su services/working_hours legacy.
    """

    knowledge = knowledge or {}
    services = knowledge.get("services") or services or []
    working_hours = knowledge.get("working_hours") or working_hours or []
    locations = knowledge.get("locations") or []
    exceptions = knowledge.get("exceptions") or []
    holidays = knowledge.get("holidays") or []

    collected = conversation.get("collected_data") or {}
    recent = conversation.get("recent_messages") or []
    info = tenant.get("info") or {}

    booking = {
     "candidate_slots": collected.get("last_slots") or [],
     "selected_slot": collected.get("selected_slot"),
     "matched_preferences": collected.get("matched_preferences"),
     "result": collected.get("last_booking_result"),
     
     "slot_context": {
         "status": collected.get("slot_context_status", "none"),
         "search_preferences": collected.get("search_preferences") or {},
     },
    }

    context = {
        "tenant": {
            "id": tenant["id"],
            "business_name": tenant.get("business_name"),
            "specialty": tenant.get("specialty"),
            "assistant_name": tenant.get("assistant_name"),
            "timezone": tenant.get("timezone", "Europe/Rome"),
            "language": tenant.get("language", "it"),
            "slot_search_days": tenant.get("slot_search_days", 30),
            "min_lead_hours": tenant.get("min_lead_hours", 2),
            "max_appointments_per_day": tenant.get("max_appointments_per_day", 12),
            "info": info,
        },

        "customer": {
            "id": customer["id"],
            "phone_number": customer.get("phone_number"),
            "full_name": customer.get("full_name"),
        },

        "conversation": {
            "id": conversation["id"],
            "status": conversation.get("status", "active"),
            "workflow": conversation.get("workflow", "idle"),
            "step": conversation.get("step", "none"),
            "retry_count": conversation.get("retry_count", 0),
            "timeout_at": conversation.get("timeout_at"),
            "last_message_at": conversation.get("last_message_at"),
        },

        "collected_data": collected,

        "request": {
            "message": message.get("message") or message.get("text"),
            "message_id": message.get("message_id") or message.get("id"),
            "received_at": message.get("received_at"),
        },

        "recent_messages": recent,

        "ai": {
            "intent": None,
            "confidence": None,
            "entities": {},
            "preferences": {},
        },

        "booking": booking,

        "knowledge": {
            "locations": locations,
            "working_hours": working_hours,
            "services": services,
            "exceptions": exceptions,
            "holidays": holidays,
            "working_hours_text": knowledge.get("working_hours_text") or "",
            "services_text": knowledge.get("services_text") or "",
            "locations_text": knowledge.get("locations_text") or "",
        },

        "runtime": {
            "timezone": tenant.get("timezone", "Europe/Rome"),
            "processed_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    return context
