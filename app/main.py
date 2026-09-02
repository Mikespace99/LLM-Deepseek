import hashlib
import hmac
import json
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from app.config import Config
from app.repositories.tenant import get_tenant_by_whatsapp_number, get_tenant_knowledge
from app.repositories.customer import get_or_create_customer
from app.repositories.conversation import get_or_create_conversation, update_conversation, append_message
from app.ai.conversation_agent import ConversationAgent
from app.state.manager import StateManager
from app.booking.engine_adapter import search_and_update_state, create_and_update_state
from app.integrations.whatsapp import send_whatsapp_message
from app.message_buffer import message_buffer

app = FastAPI(title="AI Booking V2", version="2.0.0")


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0.0"}


# ============================================================
# WHATSAPP WEBHOOK VERIFICATION
# ============================================================

@app.get("/webhook/whatsapp")
async def verify_whatsapp(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == Config.WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(challenge or "")
    return PlainTextResponse("Forbidden", status_code=403)


# ============================================================
# WHATSAPP MESSAGE WEBHOOK
# ============================================================

def _verify_meta_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    received = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, received)


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    raw_body = await request.body()

    if Config.WHATSAPP_APP_SECRET:
        signature_header = request.headers.get("x-hub-signature-256")
        if not _verify_meta_signature(raw_body, signature_header, Config.WHATSAPP_APP_SECRET):
            print("--- WEBHOOK RIFIUTATO: firma non valida ---")
            return PlainTextResponse("Forbidden", status_code=403)

    payload = json.loads(raw_body)
    print("--- WEBHOOK RICEVUTO ---", payload)

    message = _extract_message(payload)
    if not message:
        return {"status": "ignored"}

    await message_buffer.add_message(message["from"], message, process_messages)
    return {"status": "accepted"}


def _extract_message(payload: dict) -> dict | None:
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]
        value = change["value"]
        messages = value.get("messages")
        if not messages:
            return None
        msg = messages[0]
        if msg.get("type") != "text":
            return None
        metadata = value.get("metadata", {})
        ts = msg.get("timestamp")
        received_at = (
            datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
            if ts else datetime.now(timezone.utc).isoformat()
        )
        return {
            "to": metadata.get("display_phone_number"),
            "from": msg.get("from"),
            "message": msg["text"]["body"],
            "message_id": msg.get("id"),
            "received_at": received_at,
        }
    except (KeyError, IndexError, TypeError):
        return None


# ============================================================
# PIPELINE PRINCIPALE
# ============================================================

async def process_messages(messages: list[dict]):
    if not messages:
        return

    last = messages[-1]
    phone = last["from"]
    business_phone = last["to"]
    combined_text = "\n".join(m["message"].strip() for m in messages if m.get("message"))

    print(f"=== PROCESS {len(messages)} MSG da {phone} ===")
    print(combined_text)

    # 1. Tenant
    tenant = get_tenant_by_whatsapp_number(business_phone)
    if not tenant:
        print("Tenant non trovato per numero:", business_phone)
        return

    # 2. Customer
    customer = get_or_create_customer(tenant["id"], phone)

    # 3. Conversazione
    conversation, expired = get_or_create_conversation(tenant["id"], customer["id"], phone)

    # 4. Aggiorna storico
    recent = conversation.get("recent_messages") or []
    for m in messages:
        recent = append_message(conversation["id"], "user", m["message"], recent)
    conversation["recent_messages"] = recent

    # 5. Knowledge
    knowledge = get_tenant_knowledge(tenant["id"])

    # 6. Conversazione scaduta
    if expired:
        wa_info = tenant.get("info") or {}
        token = wa_info.get("access_token") or Config.WHATSAPP_TOKEN
        phone_id = wa_info.get("phone_number_id") or Config.WHATSAPP_PHONE_NUMBER_ID
        reply = "⏰ La conversazione precedente è scaduta. Ricominciamo da capo. Cosa ti serve?"
        await send_whatsapp_message(phone, reply, token, phone_id)
        append_message(conversation["id"], "assistant", reply, recent)
        update_conversation(
            conversation["id"],
            workflow="idle",
            step="none",
            state=StateManager.initial_state(),
            collected_data={},
        )
        return

    # 7. Stato
    state = conversation.get("state") or StateManager.initial_state()

    # 8. Agente
    agent = ConversationAgent(tenant, knowledge)

    # 9. Contesto
    context = {
        "state": state,
        "recent_messages": conversation.get("recent_messages", []),
        "message": combined_text,
        "slots_found": False,
        "search_result": None,
        "booking_result": None,
    }

    # 10. Processa
    result = agent.process(context)
    new_state = StateManager.merge(state, result["state"])
    reply = result["reply"]
    action = result.get("action")

    # 11. Azioni
    wa_info = tenant.get("info") or {}
    token = wa_info.get("access_token") or Config.WHATSAPP_TOKEN
    phone_id = wa_info.get("phone_number_id") or Config.WHATSAPP_PHONE_NUMBER_ID

    if action == "search_availability":
        await send_whatsapp_message(phone, "🔍 Verifico la disponibilità... un attimo.", token, phone_id)
        new_state, search_result = search_and_update_state(tenant, knowledge, new_state)
        context["state"] = new_state
        context["slots_found"] = True
        context["search_result"] = search_result
        result2 = agent.process(context)
        reply = result2["reply"]
        new_state = StateManager.merge(new_state, result2["state"])

    elif action == "create_booking":
        new_state, booking_result = create_and_update_state(tenant, knowledge, new_state, customer, phone)
        if booking_result.get("result", {}).get("success"):
            slot = new_state.get("selected_slot", {})
            reply = f"""✅ Appuntamento confermato!

📋 Servizio: {new_state.get('service', '—')}
👤 Nome: {new_state.get('person_name', '—')}
📅 Data: {slot.get('date', '—') if slot else '—'}
🕐 Ora: {slot.get('time', '—') if slot else '—'}

A presto! 👋"""
        else:
            reply = "❌ Non è stato possibile confermare. Vuoi provare con un altro orario?"
            new_state["step"] = "showing_slots"

    elif action == "request_human":
        reply = "👤 Ti metto in contatto con un operatore. Un attimo..."

    # 12. Salva
    if new_state.get("conversation_ended"):
        workflow = "idle"
        step = "none"
        new_state["conversation_ended"] = False
    else:
        workflow = "booking"
        step = new_state.get("step", "collecting_info")

    update_conversation(
        conversation["id"],
        workflow=workflow,
        step=step,
        state=new_state,
        collected_data=StateManager.extract_collected_data(new_state),
    )

    # 13. Invia risposta
    if reply:
        await send_whatsapp_message(phone, reply, token, phone_id)
        append_message(conversation["id"], "assistant", reply, conversation.get("recent_messages", []))

    print("=== DONE ===")
