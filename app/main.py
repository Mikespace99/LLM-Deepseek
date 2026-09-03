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
from app.ai.intent_parser import run_agent_pipeline
from app.templates import messages as tpl
from app.integrations.whatsapp import send_whatsapp_message
from app.booking.engine import search_availability, create_booking
from app.message_buffer import message_buffer
from app.web.routes import router as web_router

app = FastAPI(title="AI Booking Agentic", version="1.0.0")

app.include_router(web_router)


# ============================================================
# HELPER MATEMATICI DI FORMATTAZIONE ETICHETTE SLOT
# ============================================================

def _slot_labels(slots: list) -> list[str]:
    """Prende una lista di slot e restituisce una lista di stringhe formattate matematicamente."""
    labels = []
    
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
# ROTTE DI VERIFICA E WEBHOOK
# ============================================================

@app.get("/api/status")
def api_status():
    return {"status": "running", "message": "Backend WhatsApp AI Agentic attivo!"}


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
# PIPELINE ESECUTIVA PRINCIPALE (DETERMINISTICA)
# ============================================================

async def process_messages(messages: list[dict]):
    if not messages:
        return

    last = messages[-1]
    phone = last["from"]
    business_phone = last["to"]

    combined_text = "\n".join(m["message"].strip() for m in messages if m.get("message"))
    print(f"=== ENGINE PROCESS {len(messages)} MSG DA {phone} ===")

    # 1. Recupero Dati Tenant, Customer e Conversazione
    tenant = get_tenant_by_whatsapp_number(business_phone)
    if not tenant:
        print("Tenant non trovato per numero:", business_phone)
        return

    tenant_id = tenant["id"]
    customer = get_or_create_customer(tenant_id, phone)
    conversation, expired = get_or_create_conversation(tenant["id"], customer["id"], phone)

    # 2. Append e aggiornamento cronologia a DB
    recent = conversation.get("recent_messages") or []
    for m in messages:
        recent = append_message(conversation["id"], role="user", content=m["message"], current_messages=recent)
    conversation["recent_messages"] = recent

    # Gestione della sessione scaduta
    if expired:
        wa_info = tenant.get("info") or {}
        token = wa_info.get("access_token") or Config.WHATSAPP_TOKEN
        phone_id = wa_info.get("phone_number_id") or Config.WHATSAPP_PHONE_NUMBER_ID
        await send_whatsapp_message(phone, tpl.CONVERSATION_EXPIRED, token, phone_id)
        append_message(conversation["id"], role="assistant", content=tpl.CONVERSATION_EXPIRED, current_messages=conversation.get("recent_messages"))
        return

    knowledge = get_tenant_knowledge(tenant_id)

    # 3. Costruzione del Context completo per il Prompt
    fake_message = {
        "message": combined_text,
        "message_id": last.get("message_id"),
        "received_at": last.get("received_at"),
        "from": phone,
        "to": business_phone,
    }
    context = build_context(tenant=tenant, customer=customer, conversation=conversation, message=fake_message, knowledge=knowledge)

    # 4. CHIAMATA AL CERVELLO DELL'AGENTE AI (Solo interpretazione e comandi)
    print("[PIPELINE] Invocazione Agente AI Centralizzato...")
    agent_output = run_agent_pipeline(message_text=combined_text, full_context_dict=context)
    
    whatsapp_reply = agent_output.get("whatsapp_reply", "")
    action = agent_output.get("action", "JUST_TALK")
    parameters = agent_output.get("parameters") or {}

    collected = conversation.get("collected_data") or {}
    new_collected = dict(collected)
    reply_text = whatsapp_reply

    # ============================================================
    # SMISTAMENTO COMANDI DELL'IA AL MOTORE DETERMINISTICO
    # ============================================================

    # COMANDO 1: RICERCA DISPONIBILITÀ (Controllata dal codice)
    if action == "SEARCH_SLOTS":
        # Svuotiamo i residui legacy e salviamo i range puliti dell'IA
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

        # Invio immediato notifica di cortesia fissa se richiesta
        if not whatsapp_reply:
            wa_info = tenant.get("info") or {}
            await send_whatsapp_message(phone, tpl.VERIFYING_AVAILABILITY, wa_info.get("access_token") or Config.WHATSAPP_TOKEN, wa_info.get("phone_number_id") or Config.WHATSAPP_PHONE_NUMBER_ID)

        # Chiamata al database calendari locale
        try:
            booking_res = search_availability(tenant=tenant, knowledge=knowledge, collected_data=new_collected)
            slots = booking_res.get("candidate_slots") or []
            result = booking_res.get("result") or {}
            
            if slots:
                labels = _slot_labels(slots)
                slots_text = "\n".join(f"{i+1}. {label}" for i, label in enumerate(labels))
                reply_text = f"{whatsapp_reply}\n\n{slots_text}\n\nQuale preferisci? (puoi rispondere con il numero o l'orario)"
                new_collected["last_slots"] = slots
            else:
                if result.get("search_was_narrow"):
                    days = tenant.get("slot_search_days") or 30
                    reply_text = f"{whatsapp_reply}\n\nNon ho trovato disponibilità nel periodo richiesto feriale. Vuoi che allarghi la ricerca ai prossimi {days} giorni?"
                else:
                    reply_text = f"{whatsapp_reply}\n\nPurtroppo non ho trovato nessuno slot disponibile nel raggio dei giorni lavorativi dello studio."
        except Exception as e:
            print(f"[ENGINE ERROR] Errore durante search_availability: {e}")
            reply_text = f"{whatsapp_reply}\n\nSi è verificato un problema nel verificare i calendari. Riprova tra un attimo."

    # COMANDO 2: PRENOTAZIONE DETERMINISTICA (Risoluzione numerica a codice)
    elif action == "CONFIRM_BOOKING":
        slot_number = parameters.get("slot_number")
        exact_time = parameters.get("exact_time")
        
        resolved_slot = None
        all_available_slots = (new_collected.get("last_slots") or []) + (new_collected.get("historical_slots") or [])
        
        # 1. Verifica matematica dell'indice numerico (es. "Fisso il numero 3")
        if slot_number is not None:
            try:
                idx = int(slot_number) - 1
                if 0 <= idx < len(new_collected.get("last_slots", [])):
                    resolved_slot = new_collected["last_slots"][idx]
            except (TypeError, ValueError):
                pass
        
        # 2. Verifica testuale dell'orario esatto (es. "Fisso alle 16:00")
        elif exact_time:
            wanted = str(exact_time).strip()
            for slot in all_available_slots:
                if slot.get("time") == wanted or wanted in slot.get("label", ""):
                    resolved_slot = slot
                    break

        # Se il backend trova lo slot cercato dall'utente, esegue l'inserimento protetto su Supabase
        if resolved_slot:
            new_collected["selected_slot"] = resolved_slot
            if parameters.get("person_name"):
                new_collected["person_name"] = parameters.get("person_name")
                
            try:
                booking_res = create_booking(tenant=tenant, knowledge=knowledge, collected_data=new_collected, customer=customer, phone_number=phone)
                if booking_res.get("result", {}).get("success"):
                    reply_text = whatsapp_reply if whatsapp_reply else tpl.BOOKING_CONFIRMED
                    # Svuotiamo la memoria a transazione felicemente conclusa
                    new_collected = {}
                else:
                    reply_text = "Non è stato possibile confermare lo slot richiesto perché l'orario risulta occupato. Posso cercarne un altro?"
            except Exception as e:
                print(f"[ENGINE ERROR] Errore in create_booking: {e}")
                reply_text = "Si è verificato un errore tecnico durante il salvataggio dell'appuntamento. Riprova."
        else:
            # Se l'utente ha provato a selezionare qualcosa che non esiste o che è scaduto, il backend blocca l'allucinazione dell'IA
            reply_text = "Scusami, non sono riuscito ad agganciare lo slot numerato richiesto. Potresti indicarmi nuovamente il numero o l'orario esatto tra quelli mostrati sopra?"

    # COMANDO 3: JUST_TALK (Chiacchiere, Saluti, Chiusure o Annullamenti)
    else:
        if parameters.get("service"):
            new_collected["service"] = parameters.get("service")
        if parameters.get("person_name"):
            new_collected["person_name"] = parameters.get("person_name")
            
        # Se l'IA rileva che l'utente ha esplicitamente cancellato o completato la conversazione (es. "Lascia stare")
        if "annull" in combined_text.lower() or "lascia stare" in combined_text.lower() or "grazie" in combined_text.lower():
            new_collected = {} # Resetta interamente il database in totale sicurezza

    # 5. Memorizzazione dello storico degli slot per i ripensamenti futuri
    if new_collected and collected.get("last_slots"):
        historical = new_collected.get("historical_slots") or []
        for old_slot in collected["last_slots"]:
            if old_slot not in historical:
                historical.append(old_slot)
        new_collected["historical_slots"] = historical[-15:]

    # 6. Salvataggio definitivo dello stato su Supabase
    update_conversation(conversation["id"], collected_data=new_collected, workflow="idle", step="none")

    # 7. Invio del messaggio su WhatsApp Cloud API
    print("[DEBUG 10] Invio risposta finale stabilita dal codice feriale: ", reply_text)
    if reply_text:
        wa_info = tenant.get("info") or {}
        await send_whatsapp_message(phone, reply_text, wa_info.get("access_token") or Config.WHATSAPP_TOKEN, wa_info.get("phone_number_id") or Config.WHATSAPP_PHONE_NUMBER_ID)
        append_message(conversation["id"], role="assistant", content=reply_text, current_messages=conversation.get("recent_messages"))
        
    print("=== PIPELINE COMPLETATA E SALVATA ===")
