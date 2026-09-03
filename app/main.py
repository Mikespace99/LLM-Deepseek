import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.config import Config
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
# Importiamo le due macro-funzioni dell'IA dell'architettura a 5 Step
from app.ai.intent_parser import run_step1_analysis, run_step3_response
from app.templates import messages as tpl
from app.integrations.whatsapp import send_whatsapp_message
from app.booking.engine import search_availability, create_booking
from app.message_buffer import message_buffer
from app.web.routes import router as web_router

app = FastAPI(title="AI Booking 5-Steps Loop", version="2.0.0")

app.include_router(web_router)


# ============================================================
# HELPER MATEMATICI DI FORMATTAZIONE ETICHETTE SLOT
# ============================================================

def _slot_labels(slots: list) -> list[str]:
    """Prende una lista di slot e restituisce una lista di stringhe formattate matematicamente."""
    labels = []
    iso_weekdays = {0: "Lunedì", 1: "Martedì", 2: "Mercoledì", 3: "Giovedì", 4: "Venerdì", 5: "Sabato", 6: "Domenica"}
    iso_months = {1: "gennaio", 2: "febbraio", 3: "marzo", 4: "aprile", 5: "maggio", 6: "giugno", 7: "luglio", 8: "agosto", 9: "settembre", 10: "ottobre", 11: "novembre", 12: "dicembre"}

    for s in slots:
        if isinstance(s, dict) and s.get("date") and s.get("time"):
            try:
                dt = datetime.strptime(s["date"][:10], "%Y-%m-%d")
                giorno_settimana = iso_weekdays[dt.weekday()]
                mese_str = iso_months[dt.month]
                time_str = s["time"][:5]
                labels.append(f"{giorno_settimana} {dt.day} {mese_str} alle {time_str}")
            except Exception:
                labels.append(s.get("label") or s.get("datetime") or str(s))
        else:
            labels.append(str(s))
    return labels


# ============================================================
# WEBHOOK ENDPOINTS
# ============================================================

@app.get("/api/status")
def api_status():
    return {"status": "running", "message": "Backend WhatsApp AI 5-Steps Attivo!"}


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
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]
        value = change["value"]
        messages = value.get("messages")
        if not messages:
            return {"status": "ignored"}

        msg = messages[0]
        if msg.get("type") != "text":
            return {"status": "ignored"}

        metadata = value.get("metadata", {})
        ts = msg.get("timestamp")
        received_at = (
            datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
            if ts else datetime.now(timezone.utc).isoformat()
        )

        message_data = {
            "to": metadata.get("display_phone_number"),
            "from": msg.get("from"),
            "message": msg["text"]["body"],
            "message_id": msg.get("id"),
            "received_at": received_at,
        }
        
        await message_buffer.add_message(message_data["from"], message_data, process_messages)
        return {"status": "accepted"}
    except Exception as e:
        print(f"[WEBHOOK ERROR] {e}")
        return {"status": "error"}


# ============================================================
# PIPELINE A 5 STEP SEQUENZIALI (INVERSIBILE E DEFINITIVA)
# ============================================================

async def process_messages(messages: list[dict]):
    if not messages:
        return

    last = messages[-1]
    phone = last["from"]
    business_phone = last["to"]

    combined_text = "\n".join(m["message"].strip() for m in messages if m.get("message"))
    print(f"=== ENGINE PROCESS {len(messages)} MSG DA {phone} ===")

    # Caricamento Tenant, Customer e Conversazione dallo stato
    tenant = get_tenant_by_whatsapp_number(business_phone)
    if not tenant:
        return

    tenant_id = tenant["id"]
    customer = get_or_create_customer(tenant_id, phone)
    conversation, expired = get_or_create_conversation(tenant["id"], customer["id"], phone)

    # Aggiornamento storico messaggi nel DB
    recent = conversation.get("recent_messages") or []
    for m in messages:
        recent = append_message(conversation["id"], role="user", content=m["message"], current_messages=recent)
    conversation["recent_messages"] = recent

    # Gestione sessione scaduta
    if expired:
        wa_info = tenant.get("info") or {}
        await send_whatsapp_message(phone, tpl.CONVERSATION_EXPIRED, wa_info.get("access_token") or Config.WHATSAPP_TOKEN, wa_info.get("phone_number_id") or Config.WHATSAPP_PHONE_NUMBER_ID)
        append_message(conversation["id"], role="assistant", content=tpl.CONVERSATION_EXPIRED, current_messages=conversation.get("recent_messages"))
        return

    knowledge = get_tenant_knowledge(tenant_id)
    context = build_context(tenant=tenant, customer=customer, conversation=conversation, message={"message": combined_text, "message_id": last.get("message_id"), "received_at": last.get("received_at")}, knowledge=knowledge)

    # ------------------------------------------------------------
    # STEP 1: AI ANALISTA (Comprensione dell'intenzione pura)
    # ------------------------------------------------------------
    print("[STEP 1] Esecuzione AI Analista...")
    step1_result = run_step1_analysis(message_text=combined_text, full_context_dict=context)
    action_requested = step1_result.get("action_requested", "JUST_TALK")
    parameters = step1_result.get("parameters") or {}

    # Dati pronti per essere manipolati matematicamente dal backend
    collected = conversation.get("collected_data") or {}
    new_collected = dict(collected)
    
    # Inizializziamo l'oggetto dei risultati reali da passare allo Step 3
    backend_results = {
        "action_executed": action_requested,
        "slot_found": False,
        "slots_list": [],
        "historical_slots_proposti_prima": new_collected.get("historical_slots") or [],
        "booking_success": False,
        "error_type": None
    }
    slots_text_to_append = ""

    # ------------------------------------------------------------
    # STEP 2 & STEP 4: IL BACKEND ESEGUE LE VERIFICHE E LE TRANSAZIONI
    # ------------------------------------------------------------
    print(f"[STEP 2/4] Elaborazione deterministica backend per azione: {action_requested}")

    # Sotto-flusso A: Ricerca Disponibilità
    if action_requested == "SEARCH_SLOTS":
        new_collected["last_slots"] = []
        new_collected["preferences"] = {
            "date_from": parameters.get("date_from"),
            "date_to": parameters.get("date_to"),
            "time_preference": parameters.get("time_preference"),
            "exact_time": parameters.get("exact_time"),
            "date": None, "period": None, "weekday": None
        }
        if parameters.get("service"):
            new_collected["service"] = parameters.get("service")

        try:
            booking_res = search_availability(tenant=tenant, knowledge=knowledge, collected_data=new_collected)
            slots = booking_res.get("candidate_slots") or []
            result = booking_res.get("result") or {}
            
            if slots:
                backend_results["slot_found"] = True
                backend_results["slots_list"] = slots
                new_collected["last_slots"] = slots
                
                # Prepariamo la lista numerata rigida da appendere sotto il testo dell'IA
                labels = _slot_labels(slots)
                slots_text_to_append = "\n" + "\n".join(f"{i+1}. {label}" for i, label in enumerate(labels)) + "\n\nQuale preferisci? (puoi rispondere con il numero o con l'orario)"
            else:
                backend_results["error_type"] = "no_slots_found"
            if result.get("search_was_narrow"):
                    backend_results["error_type"] = "no_slots_narrow"
            except Exception as e:
                print(f"[BACKEND ERROR] Errore in search_availability: {e}")
                backend_results["error_type"] = "technical_error"

    # Sotto-flusso B: Prenotazione Deterministica e Transazione (Step 4 Consolidamento)
    elif action_requested == "CONFIRM_BOOKING":
                slot_number = parameters.get("slot_number")
        exact_time = parameters.get("exact_time")
        
        resolved_slot = None
        all_slots_in_memory = (new_collected.get("last_slots") or []) + (new_collected.get("historical_slots") or [])
        
        if slot_number is not None:
            try:
                idx = int(slot_number) - 1
                if 0 <= idx < len(new_collected.get("last_slots", [])):
                    resolved_slot = new_collected["last_slots"][idx]
            except (TypeError, ValueError):
                pass
                
        elif exact_time:
            wanted = str(exact_time).strip()
            for slot in all_slots_in_memory:
                if slot.get("time") == wanted or wanted in slot.get("label", ""):
                    resolved_slot = slot
                    break

        if resolved_slot:
            new_collected["selected_slot"] = resolved_slot
            if parameters.get("person_name"):
                new_collected["person_name"] = parameters.get("person_name")
            
            # [STEP 4]: Inserimento fisico e blindato su Supabase
            try:
                booking_res = create_booking(
                    tenant=tenant, 
                    knowledge=knowledge, 
                    collected_data=new_collected, 
                    customer=customer, 
                    phone_number=phone
                )
                if booking_res.get("result", {}).get("success"):
                    backend_results["booking_success"] = True
                    # Svuotiamo i filtri: la transazione è conclusa con successo!
                    new_collected = {}
                else:
                    backend_results["error_type"] = "slot_occupied"
            except Exception as e:
                print(f"[BACKEND ERROR] Errore in create_booking: {e}")
                backend_results["error_type"] = "technical_error"
        else:
            backend_results["error_type"] = "slot_not_found_in_memory"

    # Sotto-flusso C: Chiacchiere, Saluti o Annullamento ("Lascia stare")
    else:
        if parameters.get("service"):
            new_collected["service"] = parameters.get("service")
        if parameters.get("person_name"):
            new_collected["person_name"] = parameters.get("person_name")
            
        # [STEP 5]: Se l'utente esprime esplicitamente un congedo o un annullamento, pialliamo il DB
        lowered_text = combined_text.lower()
        if "lascia stare" in lowered_text or "annull" in lowered_text or "basta" in lowered_text or "grazie" in lowered_text:
            new_collected = {}
            backend_results["action_executed"] = "RESET_COMPLETED"

    # Memorizzazione degli slot storici se la conversazione è ancora attiva
    if new_collected and collected.get("last_slots"):
        historical = new_collected.get("historical_slots") or []
        for old_slot in collected["last_slots"]:
            if old_slot not in historical:
                historical.append(old_slot)
        new_collected["historical_slots"] = historical[-15:]

    # ------------------------------------------------------------
    # STEP 3: AI REDATTRICE (Generazione della risposta WhatsApp reale)
    # ------------------------------------------------------------
    print("[STEP 3] Invocazione AI Redattrice con i dati reali del backend...")
    
    # Costruiamo la stringa della cronologia per nutrire lo Step 3
    history_str = ""
    for m in recent[-5:]:
        role_label = "Cliente" if m.get("role") == "user" else "Assistente"
        history_str += f"- {role_label}: {m.get('content') or m.get('text', '')}\n"

    reply_text = run_step3_response(
        message_text=combined_text, 
        backend_results=backend_results, 
        history_text=history_str
    )

    # Se il backend ha trovato degli slot feriali reali, li appende rigidamente sotto il testo dell'IA
    if action_requested == "SEARCH_SLOTS" and backend_results["slot_found"]:
        reply_text = f"{reply_text}\n{slots_text_to_append}"
    elif action_requested == "CONFIRM_BOOKING" and backend_results["error_type"] == "slot_not_found_in_memory":
        reply_text = "Scusami, non sono riuscito a trovare lo slot richiesto. Potresti indicarmi il numero esatto tra quelli proposti sopra?"

    # ------------------------------------------------------------
    # STEP 5: CONSOLIDAMENTO E STRUTTURAZIONE INVIO FINALE
    # ------------------------------------------------------------
    print("[STEP 5] Salvataggio finale del DB e invio su WhatsApp Cloud API...")
    update_conversation(conversation["id"], collected_data=new_collected, workflow="idle", step="none")

    if reply_text:
        wa_info = tenant.get("info") or {}
        await send_whatsapp_message(
            phone, 
            reply_text, 
            wa_info.get("access_token") or Config.WHATSAPP_TOKEN, 
            wa_info.get("phone_number_id") or Config.WHATSAPP_PHONE_NUMBER_ID
        )
        append_message(
            conversation["id"], 
            role="assistant", 
            content=reply_text, 
            current_messages=conversation.get("recent_messages")
        )

    print("=== [LOOP CHIUSO FELICEMENTE] ===")

