import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.config import Config
from app.constants import WORKFLOW_IDLE, STEP_NONE
from app.repositories.tenant import (
    get_tenant_by_whatsapp_number,
    get_tenant_knowledge,
)
from app.repositories.customer import get_or_create_customer
from app.repositories.conversation import (
    get_or_create_conversation,
    update_conversation,
    append_message,
)
from app.context.builder import build_context
# Invochiamo la nuova pipeline agentica centralizzata
from app.ai.intent_parser import run_agent_pipeline
from app.decision import decide
from app.templates import messages as tpl
from app.integrations.whatsapp import send_whatsapp_message
from app.booking.engine import search_availability, create_booking
from app.message_buffer import message_buffer
from app.web.routes import router as web_router

app = FastAPI(title="AI Booking Simple", version="0.2.0")

# Web UI (login, register, onboarding)
app.include_router(web_router)


# ============================================================
# UTILITIES E HELPER DI FORMATTAZIONE
# ============================================================

def _slot_labels(slots: list) -> list[str]:
    """Prende una lista di slot e restituisce una lista di stringhe formattate in modo corretto."""
    labels = []
    
    # Mappatura standard ISO (Python: Monday=0, Sunday=6)
    iso_weekdays = {
        0: "Lunedì", 1: "Martedì", 2: "Mercoledì", 3: "Giovedì",
        4: "Venerdì", 5: "Sabato", 6: "Domenica"
    }
    
    iso_months = {
        1: "gennaio", 2: "febbraio", 3: "marzo", 4: "aprile", 5: "maggio", 6: "giugno",
        7: "luglio", 8: "agosto", 9: "settembre", 10: "ottobre", 11: "novembre", 12: "dicembre"
    }

    for s in slots:
        if isinstance(s, dict) and s.get("date") and s.get("time"):
            try:
                # Leggiamo la data reale estratta (es. "2026-09-11")
                dt = datetime.strptime(s["date"][:10], "%Y-%m-%d")
                giorno_settimana = iso_weekdays[dt.weekday()]
                mese_str = iso_months[dt.month]
                time_str = s["time"][:5] # Prende HH:MM
                
                # Generiamo la label perfetta senza sfasamenti di array esterni
                labels.append(f"{giorno_settimana} {dt.day} {mese_str} alle {time_str}")
            except Exception:
                labels.append(s.get("label") or s.get("datetime") or str(s))
        else:
            labels.append(str(s))
    return labels



def _build_reply_after_n8n(context: dict, decision: dict) -> str:
    """Costruisce la risposta testuale unendo l'intelligenza dell'IA con gli slot reali del DB."""
    booking = context.get("booking") or {}
    slots = booking.get("candidate_slots") or []
    result = booking.get("result") or {}
    n8n_action = decision.get("n8n_action")
    ai_reply = decision.get("whatsapp_reply_override")

    # Caso Creazione Prenotazione
    if n8n_action == "create_booking":
        if ai_reply:
            return ai_reply
        return tpl.BOOKING_CONFIRMED if result.get("success") else tpl.BOOKING_FAILED

    # Caso Ricerca Slot (search_availability)
    if slots:
        labels = _slot_labels(slots)
        # Costruiamo la lista numerata in modo pulito
        slots_text = "\n".join(f"{i+1}. {label}" for i, label in enumerate(labels))
        
        # Se l'IA ha generato un testo empatico (es. "Ecco i posti per la settimana prossima:"), usiamo quello come intro!
        if ai_reply and "Non ho trovato" not in ai_reply:
            return f"{ai_reply}\n\n{slots_text}\n\nQuale preferisci? (puoi rispondere con il numero o con l'orario)"
        
        # Altrimenti testo standard lineare senza dire "Non ho trovato come richiesto"
        return f"Ecco le disponibilità trovate:\n\n{slots_text}\n\nQuale preferisci? (puoi rispondere con il numero o con l'orario)"

    # Caso in cui NON ci sono slot
    if ai_reply: 
        return ai_reply  # Lasciamo che l'IA spieghi in modo umano perché non c'è posto e cosa fare
        
    if result.get("no_slots") and result.get("search_was_narrow"):
        days = (context.get("tenant") or {}).get("slot_search_days") or 30
        return tpl.no_slots_narrow(days)
        
    return tpl.NO_SLOTS_FOUND


def _resolve_template(decision: dict, context: dict) -> str:
    """Associa la chiave del template decisa dal motore al testo finale."""
    # RETE DI SICUREZZA AGENTICA: Se l'IA ha generato la risposta fluida personalizzata,
    # la usiamo direttamente scavalcando tutti i vecchi template rigidi del backend.
    if decision.get("whatsapp_reply_override"):
        return decision["whatsapp_reply_override"]

    key = decision.get("template_key")
    collected = context.get("collected_data") or {}
    booking = context.get("booking") or {}
    tenant_info = (context.get("tenant") or {}).get("info") or {}
    ai = context.get("ai") or {}
    entities = ai.get("entities") or {}

    static = tpl.get_template(key) if key else None
    knowledge = context.get("knowledge") or {}

    if key == "ask_service":
        return tpl.ask_service_with_list(knowledge.get("services"))

    if key == "confirmation_summary":
        slot = collected.get("selected_slot") or {}
        slot_date = slot.get("date") if isinstance(slot, dict) else None
        slot_time = slot.get("time") if isinstance(slot, dict) else None
        slot_label = slot.get("label") if isinstance(slot, dict) else None
        return tpl.confirmation_summary(
            service=collected.get("service") or "—",
            date=slot_date or slot_label or "—",
            time=slot_time or "—",
            person_name=collected.get("person_name") or "—",
        )

    if key == "confirm_slot":
        slot = collected.get("selected_slot") or {}
        label = slot.get("label") if isinstance(slot, dict) else None
        return tpl.confirm_slot(label or "questo slot")

    if key == "no_slots_narrow":
        days = (context.get("tenant") or {}).get("slot_search_days") or 30
        return tpl.no_slots_narrow(days)

    if key == "showing_slots":
        slots = booking.get("candidate_slots") or collected.get("last_slots") or []
        labels = _slot_labels(slots)
        if labels:
            return tpl.showing_slots(labels)
        return tpl.NO_SLOTS_FOUND

    if key == "lateral_info":
        info_type = entities.get("info_type")
        msg = ((context.get("request") or {}).get("message") or "").lower()
        knowledge = context.get("knowledge") or {}
        tenant_ctx = context.get("tenant") or {}

        if info_type == "parking" or "parcheggio" in msg:
            parking = tenant_info.get("parking") or "Per il parcheggio ti consiglio di chiedere in studio."
            return f"{parking}\n\n{tpl.LATERAL_CONTINUE}"
        if info_type == "price" or "prezzo" in msg or "costa" in msg:
            services_text = knowledge.get("services_text") or ""
            if services_text:
                return f"Ecco i servizi e i prezzi:\n\n{services_text}\n\n{tpl.LATERAL_CONTINUE}"
            return f"I prezzi dipendono dal servizio. Dimmi pure quale ti interessa.\n\n{tpl.LATERAL_CONTINUE}"
        if info_type == "address" or "indirizzo" in msg or "dove siete" in msg or "sede" in msg:
            locations_text = knowledge.get("locations_text") or ""
            if locations_text:
                return f"Le nostre sedi:\n\n{locations_text}\n\n{tpl.LATERAL_CONTINUE}"
            address = tenant_info.get("address") or "L'indirizzo è disponibile su richiesta."
            return f"{address}\n\n{tpl.LATERAL_CONTINUE}"
        if info_type == "hours" or "orari" in msg:
            hours_text = knowledge.get("working_hours_text") or ""
            if hours_text:
                return f"Orari di apertura:\n\n{hours_text}\n\n{tpl.LATERAL_CONTINUE}"
            return f"Gli orari dipendono dal giorno. Scrivimi pure per quale giorno ti serve sapere.\n\n{tpl.LATERAL_CONTINUE}"
        if "serviz" in msg:
            services_text = knowledge.get("services_text") or ""
            if services_text:
                return f"I nostri servizi:\n\n{services_text}\n\n{tpl.LATERAL_CONTINUE}"
        specialty = tenant_ctx.get("specialty")
        if specialty and ("specializz" in msg or "cosa fate" in msg or "chi siete" in msg):
            name = tenant_ctx.get("business_name") or "Lo studio"
            return f"{name} – {specialty}.\n\n{tpl.LATERAL_CONTINUE}"
        return f"Certo, dimmi pure cosa ti serve sapere (orari, sedi, servizi, prezzi…).\n\n{tpl.LATERAL_CONTINUE}"

    if static:
        return static
    return tpl.UNCLEAR


# ============================================================
# ROTTE API E WEBHOOK VERIFICATION
# ============================================================

@app.get("/api/status")
def api_status():
    return {"status": "running", "message": "Backend WhatsApp AI attivo e funzionante!"}


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.2.0"}


@app.get("/webhook/whatsapp")
async def verify_whatsapp(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == Config.WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(challenge or "")
    return PlainTextResponse("Forbidden", status_code=403)


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
    print("--- WEBHOOK RICEVUTO DA META ---", payload)

    message = _extract_message(payload)
    if not message:
        return {"status": "ignored"}

    await message_buffer.add_message(message["from"], message, process_messages)
    return {"status": "accepted"}


def _extract_message(payload: dict) -> dict | None:
    """Estrae in modo sicuro i metadati del messaggio WhatsApp dal payload Meta."""
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
# PIPELINE PRINCIPALE (MODALITÀ AGENTICA LOCALE)
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

    # 1. Caricamento Tenant
    tenant = get_tenant_by_whatsapp_number(business_phone)
    if not tenant:
        print("Tenant non trovato per numero:", business_phone)
        return

    tenant_id = tenant["id"]

    # 2. Caricamento Customer
    customer = get_or_create_customer(tenant_id, phone)

    # 3. Caricamento Conversazione dello stato
    conversation, expired = get_or_create_conversation(tenant["id"], customer["id"], phone)

    # 4. Aggiornamento storico messaggi nel DB
    recent = conversation.get("recent_messages") or []
    for m in messages:
        recent = append_message(
            conversation["id"],
            role="user",
            content=m["message"],
            current_messages=recent,
        )
    conversation["recent_messages"] = recent

    # 5. Caricamento Knowledge strutturata
    knowledge = get_tenant_knowledge(tenant_id)

    # 6. Costruzione del Context completo da dare all'Agente
    fake_message = {
        "message": combined_text,
        "message_id": last.get("message_id"),
        "received_at": last.get("received_at"),
        "from": phone,
        "to": business_phone,
    }
    context = build_context(
        tenant=tenant,
        customer=customer,
        conversation=conversation,
        message=fake_message,
        knowledge=knowledge,
    )

    # Gestione della conversazione scaduta
    if expired:
        wa_info = tenant.get("info") or {}
        token = wa_info.get("access_token") or Config.WHATSAPP_TOKEN
        phone_id = wa_info.get("phone_number_id") or Config.WHATSAPP_PHONE_NUMBER_ID

        await send_whatsapp_message(phone, tpl.CONVERSATION_EXPIRED, token, phone_id)
        append_message(
            conversation["id"],
            role="assistant",
            content=tpl.CONVERSATION_EXPIRED,
            current_messages=conversation.get("recent_messages"),
        )
        return

    # 7. ESECUZIONE DEL CERVELLO CENTRALE (Agente AI-Driven)
    print("[DEBUG 7] Invoco run_agent_pipeline con l'Agente centrale...")
    agent_result = run_agent_pipeline(
        message_text=combined_text,
        full_context_dict=context
    )
    
    # Sincronizziamo l'output dell'agente nel dizionario context per retrocompatibilità
    context["ai"] = {
        "intent": agent_result.get("new_workflow"),
        "entities": agent_result.get("backend_action", {}).get("parameters", {})
    }

    # 8. Esecuzione materiale dei comandi e allineamento degli stati
    print("[DEBUG 8] Eseguo la trasposizione degli stati con il nuovo decide...")
    decision = decide(agent_result, conversation)
    
    collected = decision.get("updated_collected") or conversation.get("collected_data") or {}

    update_fields = {
        "workflow": decision["workflow"],
        "step": decision["step"],
        "collected_data": collected,
    }
    update_conversation(conversation["id"], **update_fields)
    conversation.update(update_fields)

    context["conversation"]["workflow"] = decision["workflow"]
    context["conversation"]["step"] = decision["step"]
    context["collected_data"] = collected

    # 9. Esecuzione Azioni Tecniche sul Calendario/DB locale
    print("[DEBUG 9] Valutazione esecuzione azioni tecniche locali...")
    reply_text = None

    if decision["action"] == "request_human":
        reply_text = "Ti metto in contatto con un operatore. Un attimo di pazienza…"

    elif decision["action"] == "call_n8n":
        # Se dobbiamo cercare disponibilità e l'IA non ha ancora inviato un testo di attesa personalizzato
        if decision.get("template_key") == "verifying_availability" and not decision.get("whatsapp_reply_override"):
            wa_info = tenant.get("info") or {}
            token = wa_info.get("access_token") or Config.WHATSAPP_TOKEN
            phone_id = wa_info.get("phone_number_id") or Config.WHATSAPP_PHONE_NUMBER_ID
            await send_whatsapp_message(phone, tpl.VERIFYING_AVAILABILITY, token, phone_id)

        n8n_action = decision.get("n8n_action")  # "search_availability" | "create_booking"
        context.setdefault("booking", {})["action"] = n8n_action

        try:
            if n8n_action == "create_booking":
                context["booking"] = create_booking(
                    tenant=tenant,
                    knowledge=knowledge,
                    collected_data=collected,
                    customer=customer,
                    phone_number=phone,
                )
            else:
                context["booking"] = search_availability(
                    tenant=tenant,
                    knowledge=knowledge,
                    collected_data=collected,
                )
        except Exception as e:
            print(f"[main] Errore critico nel motore di prenotazione locale: {e}")
            context.setdefault("booking", {})["result"] = {
                "success": False,
                "error": str(e),
            }

        # Genera la risposta post-azione feriale/motore
        reply_text = _build_reply_after_n8n(context, decision)

        booking = context.get("booking") or {}
        if booking:
            new_collected = dict(collected)
            
            # --- LOGICA MEMORIA STORICA SLOT PER RIPENSAMENTI ---
            if new_collected.get("last_slots"):
                historical = new_collected.get("historical_slots") or []
                for old_slot in new_collected["last_slots"]:
                    if old_slot not in historical:
                        historical.append(old_slot)
                new_collected["historical_slots"] = historical[-15:]  # Conserva gli ultimi 15 slot
            # --- FINE LOGICA MEMORIA STORICA ---

            if booking.get("candidate_slots"):
                new_collected["last_slots"] = booking["candidate_slots"]
            if booking.get("selected_slot"):
                new_collected["selected_slot"] = booking["selected_slot"]
            if booking.get("result"):
                result = booking["result"]
                new_collected["last_booking_result"] = result
                if result.get("no_slots"):
                    new_collected["no_slots_state"] = (
                        "offer_widen" if result.get("search_was_narrow") else "offer_operator"
                    )
                    new_collected.pop("last_slots", None)
                else:
                    new_collected.pop("no_slots_state", None)

            update_conversation(
                conversation["id"],
                collected_data=new_collected,
                step=decision["step"],
            )
            context["collected_data"] = new_collected
            conversation["collected_data"] = new_collected


        # Genera la risposta post-azione feriale/motore
        reply_text = _build_reply_after_n8n(context, decision)

        booking = context.get("booking") or {}
        if booking:
            new_collected = dict(collected)
            if booking.get("candidate_slots"):
                new_collected["last_slots"] = booking["candidate_slots"]
            if booking.get("selected_slot"):
                new_collected["selected_slot"] = booking["selected_slot"]
            if booking.get("result"):
                result = booking["result"]
                new_collected["last_booking_result"] = result
                if result.get("no_slots"):
                    new_collected["no_slots_state"] = (
                        "offer_widen" if result.get("search_was_narrow") else "offer_operator"
                    )
                    new_collected.pop("last_slots", None)
                else:
                    new_collected.pop("no_slots_state", None)

            update_conversation(
                conversation["id"],
                collected_data=new_collected,
                step=decision["step"],
            )
            context["collected_data"] = new_collected
            conversation["collected_data"] = new_collected

    else:
        # Se non ci sono azioni tecniche, risolve l'override fluido dell'IA o i vecchi template statici
        reply_text = _resolve_template(decision, context)

    # 10. Invia risposta finale su WhatsApp
    print("[DEBUG 10] Invio risposta finale all'utente: ", reply_text)
    if reply_text:
        wa_info = tenant.get("info") or {}
        token = wa_info.get("access_token") or Config.WHATSAPP_TOKEN
        phone_id = wa_info.get("phone_number_id") or Config.WHATSAPP_PHONE_NUMBER_ID

        send_result = await send_whatsapp_message(phone, reply_text, token, phone_id)
        if send_result is None:
            print(f"[main] Invio fallito su API Cloud WhatsApp per {phone}.")

        append_message(
            conversation["id"],
            role="assistant",
            content=reply_text,
            current_messages=conversation.get("recent_messages"),
        )
    print("=== DONE ===")
