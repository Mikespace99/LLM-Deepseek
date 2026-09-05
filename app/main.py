import asyncio
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.ai.intent_parser import (
    run_step1_analysis,
    run_step3_response,
)
from app.booking.engine import (
    create_booking,
    revalidate_slots,
    search_availability,
)
from app.config import Config
from app.constants import (
    STEP_NONE,
    STEP_SHOWING_SLOTS,
    WORKFLOW_BOOKING,
    WORKFLOW_IDLE,
)
from app.context.builder import build_context
from app.integrations.whatsapp import send_whatsapp_message
from app.message_buffer import message_buffer
from app.repositories.conversation import (
    append_message,
    get_or_create_conversation,
    update_conversation,
)
from app.repositories.customer import (
    get_or_create_customer,
)
from app.repositories.tenant import (
    get_tenant_by_whatsapp_number,
    get_tenant_knowledge,
)
from app.templates import messages as tpl
from app.web.routes import router as web_router


app = FastAPI(
    title="AI Booking 5-Steps Loop",
    version="2.0.0",
)

app.include_router(web_router)


_GREETING_PATTERN = re.compile(
    r"^\s*(buon\s*giorno|buon\s*d[ìi]|buona\s*sera|buon\s*pomeriggio|salve|ciao)\b",
    re.IGNORECASE,
)


def _looks_like_greeting(text: str) -> bool:
    """
    Rilevamento deterministico (NIENTE AI) di un saluto di apertura
    tipico italiano a inizio messaggio. Usato come segnale che il
    cliente sta iniziando una richiesta nuova, non rispondendo a una
    proposta di slot già in corso.
    """
    return bool(_GREETING_PATTERN.match((text or "").strip()))


def _time_of_day_greeting(tz_name: str | None) -> str:
    """
    Sceglie il saluto corretto in base all'ora locale reale del tenant.
    Calcolo deterministico (mai lasciato all'AI, che non ha un orologio
    affidabile): 05:00-13:00 Buongiorno, 13:00-19:00 Buon pomeriggio,
    19:00-05:00 Buonasera.
    """
    try:
        tz = ZoneInfo(tz_name or "Europe/Rome")
    except Exception:
        tz = ZoneInfo("Europe/Rome")

    hour = datetime.now(tz).hour

    if 5 <= hour < 13:
        return "Buongiorno"
    elif 13 <= hour < 19:
        return "Buon pomeriggio"
    else:
        return "Buonasera"


def _normalize_time_str(value) -> str | None:
    """
    Normalizza un orario espresso in forme diverse ("17", "17.30",
    "17:30") nel formato "HH:MM" usato dagli slot, per un confronto
    di coerenza affidabile. Ritorna None se non interpretabile.
    """
    if not value:
        return None

    text = str(value).strip().replace(".", ":").replace(",", ":")

    if ":" not in text:
        if not text.isdigit():
            return None
        text = f"{text}:00"

    parts = text.split(":")
    if len(parts) != 2:
        return None

    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    return f"{hour:02d}:{minute:02d}"


def _slot_labels(slots: list) -> list[str]:
    """
    Prende una lista di slot e restituisce
    una lista di stringhe formattate.
    """

    labels = []

    iso_weekdays = {
        0: "Lunedì",
        1: "Martedì",
        2: "Mercoledì",
        3: "Giovedì",
        4: "Venerdì",
        5: "Sabato",
        6: "Domenica",
    }

    iso_months = {
        1: "gennaio",
        2: "febbraio",
        3: "marzo",
        4: "aprile",
        5: "maggio",
        6: "giugno",
        7: "luglio",
        8: "agosto",
        9: "settembre",
        10: "ottobre",
        11: "novembre",
        12: "dicembre",
    }

    for slot in slots:
        if (
            isinstance(slot, dict)
            and slot.get("date")
            and slot.get("time")
        ):
            try:
                dt = datetime.strptime(
                    slot["date"][:10],
                    "%Y-%m-%d",
                )

                giorno_settimana = (
                    iso_weekdays[dt.weekday()]
                )

                mese_str = iso_months[dt.month]
                time_str = slot["time"][:5]

                labels.append(
                    f"{giorno_settimana} "
                    f"{dt.day} "
                    f"{mese_str} "
                    f"alle {time_str}"
                )

            except Exception:
                labels.append(
                    slot.get("label")
                    or slot.get("datetime")
                    or str(slot)
                )

        else:
            labels.append(str(slot))

    return labels


@app.get("/api/status")
def api_status():
    return {
        "status": "running",
        "message": (
            "Backend WhatsApp AI "
            "5-Steps Attivo!"
        ),
    }


@app.get("/webhook/whatsapp")
async def verify_whatsapp(
    request: Request,
):
    params = request.query_params

    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if (
        mode == "subscribe"
        and token == Config.WHATSAPP_VERIFY_TOKEN
    ):
        return PlainTextResponse(
            challenge or ""
        )

    return PlainTextResponse(
        "Forbidden",
        status_code=403,
    )


def _verify_meta_signature(
    raw_body: bytes,
    signature_header: str | None,
    app_secret: str,
) -> bool:
    if (
        not signature_header
        or not signature_header.startswith("sha256=")
    ):
        return False

    expected = hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    received = signature_header.split(
        "=",
        1,
    )

    return hmac.compare_digest(
        expected,
        received,
    )


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
):
    raw_body = await request.body()

    if Config.WHATSAPP_APP_SECRET:
        signature_header = request.headers.get(
            "x-hub-signature-256"
        )

        if not _verify_meta_signature(
            raw_body,
            signature_header,
            Config.WHATSAPP_APP_SECRET,
        ):
            print(
                "--- WEBHOOK RIFIUTATO: "
                "firma non valida ---"
            )

            return PlainTextResponse(
                "Forbidden",
                status_code=403,
            )

    payload = json.loads(raw_body)

    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]
        value = change["value"]

        messages = value.get("messages")

        if not messages:
            return {
                "status": "ignored"
            }

        msg = messages[0]

        if msg.get("type") != "text":
            return {
                "status": "ignored"
            }

        metadata = value.get(
            "metadata",
            {},
        )

        timestamp = msg.get("timestamp")

        received_at = (
            datetime.fromtimestamp(
                int(timestamp),
                tz=timezone.utc,
            ).isoformat()
            if timestamp
            else datetime.now(
                timezone.utc
            ).isoformat()
        )

        message_data = {
            "to": metadata.get(
                "display_phone_number"
            ),
            "from": msg.get("from"),
            "message": msg["text"]["body"],
            "message_id": msg.get("id"),
            "received_at": received_at,
        }

        await message_buffer.add_message(
            message_data["from"],
            message_data,
            process_messages,
        )

        return {
            "status": "accepted"
        }

    except Exception as exc:
        print(
            f"[WEBHOOK ERROR] {exc}"
        )

        return {
            "status": "error"
        }


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
        return

    tenant_id = tenant["id"]
    customer = get_or_create_customer(tenant_id, phone)
    conversation, expired = get_or_create_conversation(tenant["id"], customer["id"], phone)

    # 2. Append e aggiornamento cronologia a DB
    recent = conversation.get("recent_messages") or []
    for m in messages:
        recent = append_message(conversation["id"], role="user", content=m["message"], current_messages=recent)
    conversation["recent_messages"] = recent

    # La sessione precedente è scaduta: get_or_create_conversation ha già
    # creato un nuovo record vuoto (collected_data={}). Non blocchiamo più
    # la risposta qui: se il messaggio è autosufficiente (es. "vorrei un
    # appuntamento per giovedì prossimo") lo elaboriamo comunque. Se invece
    # dipende da un contesto che non abbiamo più (es. "confermo lo slot 3"),
    # ce ne accorgiamo più sotto, quando CONFIRM_BOOKING non trova nulla in
    # memoria, e SOLO in quel caso chiediamo di ripetere la richiesta.

    # Segnale di "nuova richiesta": un saluto di apertura ("Buongiorno",
    # "Salve", ...) mentre NON c'è una proposta di slot in sospeso
    # (workflow != booking) significa che il cliente sta iniziando da
    # capo. Azzeriamo tutto (preferenze di ricerca, servizio, slot
    # mostrati) PRIMA di costruire il contesto per l'AI, così Step 1 non
    # vede nemmeno le vecchie preferenze e non può "mantenerle". Il
    # segnale qui è il contenuto del messaggio, non il tempo trascorso.
    # Se invece c'è una proposta in sospeso, un saluto è solo educazione
    # (es. "Buongiorno, il 3 va bene") e NON deve cancellare last_slots.
    if (
        conversation.get("collected_data")
        and _looks_like_greeting(combined_text)
        and conversation.get("workflow") != WORKFLOW_BOOKING
    ):
        conversation["collected_data"] = {}

    # Vale sia per una conversazione davvero nuova (o scaduta, azzerata
    # da get_or_create_conversation) sia per il reset da saluto qui
    # sopra: in entrambi i casi non c'è nulla in "collected_data" e la
    # prima risposta di questo scambio deve aprirsi con il saluto giusto
    # per l'orario, calcolato dal backend (mai dall'AI).
    is_conversation_start = not bool(conversation.get("collected_data"))

    knowledge = get_tenant_knowledge(tenant_id)
    context = build_context(
        tenant=tenant, 
        customer=customer, 
        conversation=conversation, 
        message={"message": combined_text, "message_id": last.get("message_id"), "received_at": last.get("received_at")}, 
        knowledge=knowledge
    )

    # ------------------------------------------------------------
    # STEP 1: AI ANALISTA (Comprensione dell'intenzione pura)
    # ------------------------------------------------------------
    print("[STEP 1] Esecuzione AI Analista...")
    step1_result = run_step1_analysis(message_text=combined_text, full_context_dict=context)
    action_requested = step1_result.get("action_requested", "JUST_TALK")
    parameters = step1_result.get("parameters") or {}

    collected = conversation.get("collected_data") or {}
    new_collected = dict(collected)

    # Catturato SUBITO, prima che qualunque ramo sotto resetti "collected_data":
    # sono gli slot mostrati realmente al cliente nel turno precedente.
    previous_last_slots = collected.get("last_slots") or []

    backend_results = {
        "action_executed": action_requested,
        "slot_found": False,
        "slots_list": [],
        "repeated_previous_slots": False,
        "booking_success": False,
        "is_studio_closed": False,
        "is_studio_full": False,
        "error_type": None,
        "confirmed_slot_label": None,
        "failed_slot_label": None,
    }
    slots_text_to_append = ""

    # ------------------------------------------------------------
    # STEP 2 & STEP 4: IL BACKEND ESEGUE LE VERIFICHE E LE TRANSAZIONI
    # ------------------------------------------------------------
    print(f"[STEP 2/4] Elaborazione backend per: {action_requested}")

    # Sotto-flusso A: Ricerca Disponibilità
    if action_requested == "SEARCH_SLOTS":
        historical_backup = new_collected.get("historical_slots") or []
        current_service = parameters.get("service") or new_collected.get("service")
        
        # Pialliamo i residui feriali a livello radice
        new_collected = {
            "service": current_service,
            "historical_slots": historical_backup,
            "last_slots": [],
            "preferences": {
                "period": parameters.get("period"),
                "weekday": parameters.get("weekday"),
                "week_part": parameters.get("week_part"),
                "date_from": parameters.get("date_from"),
                "date_to": parameters.get("date_to"),
                "time_preference": parameters.get("time_preference"),
                "exact_time": parameters.get("exact_time"),
                "date": None, "ignore_preferences": None
            }
        }

        try:
            booking_res = search_availability(tenant=tenant, knowledge=knowledge, collected_data=new_collected)
            slots = booking_res.get("candidate_slots") or []
            result = booking_res.get("result") or {}
            
            backend_results["is_studio_closed"] = result.get("is_studio_closed", False)
            backend_results["is_studio_full"] = result.get("is_studio_full", False)

            if slots:
                backend_results["slot_found"] = True
                backend_results["slots_list"] = slots
                new_collected["last_slots"] = slots
                
                labels = _slot_labels(slots)
                slots_text_to_append = "\n" + "\n".join(f"{i+1}. {label}" for i, label in enumerate(labels)) + "\n\nQuale preferisci? (puoi rispondere con il numero o con l'orario)"
            else:
                # Nessuno slot nuovo: prima di arrenderci, riverifichiamo
                # (lato backend, MAI lato AI) se le opzioni mostrate nel
                # turno precedente sono ancora libere e le riproponiamo
                # in modo deterministico, come nel percorso di successo.
                fallback_candidates = previous_last_slots or (new_collected.get("historical_slots") or [])
                still_valid = revalidate_slots(
                    tenant=tenant,
                    knowledge=knowledge,
                    collected_data=new_collected,
                    slots=fallback_candidates,
                )

                if still_valid:
                    backend_results["slot_found"] = True
                    backend_results["slots_list"] = still_valid
                    backend_results["repeated_previous_slots"] = True
                    new_collected["last_slots"] = still_valid

                    labels = _slot_labels(still_valid)
                    slots_text_to_append = "\n" + "\n".join(f"{i+1}. {label}" for i, label in enumerate(labels)) + "\n\nQuale preferisci? (puoi rispondere con il numero o con l'orario)"
                else:
                    backend_results["error_type"] = "no_slots_found"
                    if result.get("search_was_narrow"):
                        backend_results["error_type"] = "no_slots_narrow"
        except Exception as e:
            print(f"[BACKEND ERROR] Errore in search_availability: {e}")
            backend_results["error_type"] = "technical_error"

    # Sotto-flusso B: Prenotazione Deterministica e Transazione (Step 4)
    elif action_requested == "CONFIRM_BOOKING":
        all_slots_in_memory = (new_collected.get("last_slots") or []) + (new_collected.get("historical_slots") or [])

        if not all_slots_in_memory:
            # Non c'è proprio nulla da risolvere (sessione azzerata per
            # saluto o per scadenza): non ha senso interpretare "slot 3"
            # o un orario, non sappiamo a cosa si riferiscano. Chiediamo
            # di ripetere la richiesta da capo invece di indovinare.
            backend_results["error_type"] = "no_context_available"
        else:
            # Accumulo persistente (lato backend, non lato AI): se il
            # cliente ha già indicato un numero e/o un orario in un
            # turno precedente di questa stessa negoziazione, li
            # ricordiamo qui e li aggiorniamo solo quando il messaggio
            # corrente ne porta uno nuovo. Così la verifica di coerenza
            # regge anche se i due segnali arrivano in messaggi diversi
            # (es. "slot 2 alle 17" -> poi solo "Mario Rossi" per il
            # nome), invece di dipendere dalla capacità dello Step 1 di
            # ricostruire tutto da zero dalla cronologia grezza a ogni
            # turno.
            slot_number = parameters.get("slot_number")
            if slot_number is None:
                slot_number = new_collected.get("pending_slot_number")

            exact_time = parameters.get("exact_time")
            if not exact_time:
                exact_time = new_collected.get("pending_exact_time")

            new_collected["pending_slot_number"] = slot_number
            new_collected["pending_exact_time"] = exact_time

            pending = new_collected.get("pending_confirmation_slot")

            resolved_slot = None
            mismatch_slot = None

            if (
                parameters.get("slot_number") is None
                and not parameters.get("exact_time")
                and pending
            ):
                # Il cliente sta confermando la proposta di chiarimento
                # fatta nel turno precedente (es. "sì", "confermo"), senza
                # ripetere un numero/orario nuovo in questo messaggio.
                resolved_slot = pending

            elif slot_number is not None:
                candidate = None
                try:
                    idx = int(slot_number) - 1
                    if 0 <= idx < len(new_collected.get("last_slots", [])):
                        candidate = new_collected["last_slots"][idx]
                except (TypeError, ValueError):
                    pass

                if candidate:
                    wanted = _normalize_time_str(exact_time)
                    # Verifica di coerenza completa: se il cliente ha
                    # indicato ANCHE un orario esplicito, deve coincidere
                    # con quello vero dello slot scelto per numero. Se
                    # non coincide, non prenotiamo alla cieca: chiediamo
                    # conferma citando l'orario reale (verità di backend,
                    # mai improvvisata dall'AI).
                    if wanted and candidate.get("time") != wanted:
                        mismatch_slot = candidate
                    else:
                        resolved_slot = candidate

            elif exact_time:
                wanted = _normalize_time_str(exact_time)
                for slot in all_slots_in_memory:
                    if wanted and slot.get("time") == wanted:
                        resolved_slot = slot
                        break

            # La proposta di chiarimento in sospeso vale per un solo
            # turno: la consumiamo qui, sia che sia stata confermata sia
            # che sia stata superata da una nuova scelta esplicita.
            new_collected["pending_confirmation_slot"] = None

            if mismatch_slot:
                backend_results["error_type"] = "slot_time_mismatch"
                backend_results["mismatch_slot_label"] = _slot_labels([mismatch_slot])[0]
                new_collected["pending_confirmation_slot"] = mismatch_slot
                # Passiamo alla domanda di chiarimento: da qui in poi la
                # conferma passa da pending_confirmation_slot, non serve
                # più tenere in memoria il numero/orario grezzi.
                new_collected["pending_slot_number"] = None
                new_collected["pending_exact_time"] = None

            elif resolved_slot:
                new_collected["selected_slot"] = resolved_slot
                if parameters.get("person_name"):
                    new_collected["person_name"] = parameters.get("person_name")

                try:
                    booking_res = create_booking(
                        tenant=tenant, 
                        knowledge=knowledge, 
                        collected_data=new_collected, 
                        customer=customer, 
                        phone_number=phone
                    )
                    result = booking_res.get("result") or {}

                    if result.get("success"):
                        backend_results["booking_success"] = True
                        backend_results["confirmed_slot_label"] = _slot_labels([resolved_slot])[0]
                        new_collected = {}
                    else:
                        # Distinguiamo SEMPRE il motivo reale: un vero
                        # conflitto ("slot_conflict") non è la stessa cosa
                        # di un dato mancante o di un errore tecnico
                        # diverso — raccontare sempre "è già occupato" a
                        # prescindere nasconderebbe il problema vero.
                        error = result.get("error")
                        backend_results["failed_slot_label"] = _slot_labels([resolved_slot])[0]

                        if error == "slot_conflict":
                            backend_results["error_type"] = "slot_occupied"
                            # Quello slot specifico non è più valido:
                            # non ha senso ritentarlo automaticamente,
                            # il cliente deve sceglierne un altro.
                            new_collected["pending_slot_number"] = None
                            new_collected["pending_exact_time"] = None
                        elif error == "missing_data":
                            backend_results["error_type"] = "missing_data"
                            # Lo slot resta valido: manca solo il nome.
                            # Teniamo pending_slot_number/exact_time così
                            # il prossimo turno (che darà solo il nome)
                            # non deve essere re-interpretato da capo.
                        else:
                            backend_results["error_type"] = "technical_error"
                            print(f"[BACKEND ERROR] create_booking fallita per un motivo non atteso: {error}")
                except Exception as e:
                    print(f"[BACKEND ERROR] Errore in create_booking: {e}")
                    backend_results["error_type"] = "technical_error"
            else:
                backend_results["error_type"] = "slot_not_found_in_memory"
                new_collected["pending_slot_number"] = None
                new_collected["pending_exact_time"] = None

    # Sotto-flusso C: Chiacchiere, Saluti o Annullamento
    else:
        if parameters.get("service"):
            new_collected["service"] = parameters.get("service")
        if parameters.get("person_name"):
            new_collected["person_name"] = parameters.get("person_name")
            
        lowered_text = combined_text.lower()
        if "lascia stare" in lowered_text or "annull" in lowered_text or "basta" in lowered_text or "grazie" in lowered_text:
            new_collected = {}
            backend_results["action_executed"] = "RESET_COMPLETED"

    # Memorizzazione degli slot storici se la conversazione è ancora attiva.
    # Usa previous_last_slots, catturato a inizio funzione PRIMA che i rami
    # sopra potessero resettare o sovrascrivere "collected"/"new_collected".
    if new_collected and previous_last_slots:
        historical = new_collected.get("historical_slots") or []
        for old_slot in previous_last_slots:
            if old_slot not in historical:
                historical.append(old_slot)
        new_collected["historical_slots"] = historical[-15:]

    # ------------------------------------------------------------
    # STEP 3: AI REDATTRICE (Generazione della risposta WhatsApp reale)
    # ------------------------------------------------------------
    print("[STEP 3] Invocazione AI Redattrice con i dati reali del backend...")
    history_str = ""
    for m in recent[-5:]:
        role_label = "Cliente" if m.get("role") == "user" else "Assistente"
        history_str += f"- {role_label}: {m.get('content') or m.get('text', '')}\n"

    reply_text = run_step3_response(message_text=combined_text, backend_results=backend_results, history_text=history_str)

    if action_requested == "SEARCH_SLOTS" and backend_results["slot_found"]:
        reply_text = f"{reply_text}\n{slots_text_to_append}"
    elif action_requested == "CONFIRM_BOOKING" and backend_results["error_type"] == "slot_not_found_in_memory":
        reply_text = "Scusami, non sono riuscito a trovare lo slot richiesto. Potresti indicarmi il numero esatto tra quelli proposti sopra?"
    elif action_requested == "CONFIRM_BOOKING" and backend_results["error_type"] == "no_context_available":
        reply_text = tpl.CONVERSATION_EXPIRED
    elif action_requested == "CONFIRM_BOOKING" and backend_results["error_type"] == "slot_time_mismatch":
        # Verifica di coerenza slot/orario: messaggio interamente
        # deterministico, mai improvvisato dall'AI, che cita la verità
        # esatta dello slot risolto per numero.
        label = backend_results.get("mismatch_slot_label")
        reply_text = (
            f"Attenzione: lo slot indicato corrisponde in realtà a {label}, non all'orario che hai scritto. "
            f"Confermi {label}? Rispondi 'sì' per confermare, oppure scegli un altro slot tra quelli proposti."
        )

    # Il saluto iniziale ("Buongiorno"/"Buon pomeriggio"/"Buonasera") è
    # calcolato qui dal backend in base all'ora locale reale del tenant,
    # non lasciato all'AI, e antepposto SOLO al primo messaggio di una
    # conversazione nuova (o resettata da un saluto del cliente).
    if is_conversation_start and reply_text:
        greeting = _time_of_day_greeting(tenant.get("timezone"))
        reply_text = f"{greeting}! {reply_text}"

    # ------------------------------------------------------------
    # STEP 5: CONSOLIDAMENTO E STRUTTURAZIONE INVIO FINALE
    # ------------------------------------------------------------
    print("[STEP 5] Salvataggio finale del DB e invio su WhatsApp Cloud API...")
    # Il workflow riflette ora lo stato reale: "booking" con una proposta
    # di slot in attesa di scelta, "idle" in ogni altro caso. Prima veniva
    # sempre forzato a "idle", rendendo lo stato inutilizzabile come
    # segnale (es. per il rilevamento del saluto qui sopra).
    if action_requested == "SEARCH_SLOTS" and backend_results["slot_found"]:
        workflow_to_save, step_to_save = WORKFLOW_BOOKING, STEP_SHOWING_SLOTS
    else:
        workflow_to_save, step_to_save = WORKFLOW_IDLE, STEP_NONE

    update_conversation(conversation["id"], collected_data=new_collected, workflow=workflow_to_save, step=step_to_save)

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

    print(
        "=== [LOOP CHIUSO FELICEMENTE] ==="
    )
