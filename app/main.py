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
    search_available_days,
    search_times_for_day,
)
from app.config import Config
from app.constants import (
    STEP_NONE,
    STEP_SHOWING_SLOTS,
    STEP_SHOWING_DAYS,
    STEP_SHOWING_TIMES,
    WORKFLOW_BOOKING,
    WORKFLOW_IDLE,
)
from app.context.builder import build_context
from app.integrations.whatsapp import send_whatsapp_message
from app.message_buffer import message_buffer
from app.repositories import appointment as appointment_repo
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


_ITALIAN_WEEKDAYS = {
    0: "Lunedì",
    1: "Martedì",
    2: "Mercoledì",
    3: "Giovedì",
    4: "Venerdì",
    5: "Sabato",
    6: "Domenica",
}

_ITALIAN_MONTHS = {
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


def _slot_labels(slots: list) -> list[str]:
    """
    Prende una lista di slot e restituisce
    una lista di stringhe formattate.
    """

    labels = []

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
                    _ITALIAN_WEEKDAYS[dt.weekday()]
                )

                mese_str = _ITALIAN_MONTHS[dt.month]
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


def _appointment_label(appt: dict) -> str:
    """
    Etichetta leggibile (stesso stile di _slot_labels) per un
    appuntamento esistente letto dal DB. Usata nei messaggi
    deterministici del flusso di modifica appuntamento, per citare
    sempre la verità reale, mai un orario ricostruito dall'AI.
    """
    date_str = str(appt.get("appointment_date") or "")[:10]
    time_str = str(appt.get("appointment_time") or "")[:5]

    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{_ITALIAN_WEEKDAYS[dt.weekday()]} {dt.day} {_ITALIAN_MONTHS[dt.month]} alle {time_str}"
    except Exception:
        return f"{date_str} alle {time_str}"


def _describe_search_criteria(parameters: dict) -> str | None:
    """
    Descrizione testuale del criterio di ricerca ORIGINALE (quello
    richiesto, non quello dedotto dai risultati), calcolata dal backend
    dagli stessi parametri usati per la ricerca. Passata allo Step 3
    perché l'introduzione rifletta sempre cosa è stato chiesto, non un
    giorno dedotto guardando quali slot sono usciti — es. una ricerca su
    "la prossima settimana" i cui unici risultati liberi cadono di
    mercoledì non deve diventare "ecco le disponibilità per mercoledì".
    """
    period = parameters.get("period")
    weekday = parameters.get("weekday")
    week_part = parameters.get("week_part")
    date_from = parameters.get("date_from")
    date_to = parameters.get("date_to")
    time_pref = parameters.get("time_preference")

    if weekday:
        base = f"{weekday} prossimo" if period == "next_week" else weekday
    elif date_from and date_to and date_from == date_to:
        base = f"il {date_from}"
    elif date_from and date_to:
        base = f"dal {date_from} al {date_to}"
    elif week_part == "start":
        base = "l'inizio della prossima settimana" if period == "next_week" else "l'inizio settimana"
    elif week_part == "mid":
        base = "metà della prossima settimana" if period == "next_week" else "metà settimana"
    elif week_part == "weekend":
        base = "il weekend prossimo" if period == "next_week" else "il weekend"
    elif period == "today":
        base = "oggi"
    elif period == "tomorrow":
        base = "domani"
    elif period == "this_week":
        base = "questa settimana"
    elif period == "next_week":
        base = "la prossima settimana"
    else:
        base = None

    if not base:
        return None

    time_suffix = {
        "morning": " di mattina",
        "afternoon": " di pomeriggio",
        "evening": " di sera",
    }.get(time_pref, "")

    return f"{base}{time_suffix}"





def _classify_awaiting_name_message(text: str) -> str:
    """
    Classificazione deterministica del messaggio mentre aspettiamo il nome.
    Ritorna: "name" | "info_question" | "cancel" | "yesno" | "doubt"
    """
    import re

    t = (text or "").strip()
    if not t:
        return "doubt"

    low = t.lower().strip()

    # Annulla
    if any(p in low for p in (
        "lascia stare", "annulla", "non voglio più", "dimentica", "stop",
    )):
        return "cancel"

    # Sì/No puri
    if low in {
        "si", "sì", "no", "ok", "va bene", "confermo", "certo", "esatto",
        "no grazie", "no no",
    }:
        return "yesno"

    # Domanda / richiesta informativa chiara
    info_markers = (
        "quanto", "costo", "costa", "prezzo", "prezzi", "tariffa",
        "parcheggio", "parch", "dove siete", "indirizzo", "orari",
        "che orari", "aperti", "chiusi", "accettate", "bancomat",
        "carta", "pagare", "pagamento", "?",
    )
    if any(m in low for m in info_markers):
        return "info_question"

    # Solo numero / scelta slot residua
    if t.isdigit() or re.fullmatch(r"il\s*\d+", low):
        return "doubt"

    # Sembra un nome: 1–4 parole, principalmente lettere, niente ?
    words = t.split()
    if 1 <= len(words) <= 4 and "?" not in t:
        letter_words = sum(1 for w in words if any(c.isalpha() for c in w))
        if letter_words >= len(words) and not any(ch.isdigit() for ch in t):
            # Escludi frasi operative residue
            if not any(p in low for p in (
                "prenot", "appuntament", "spost", "disponib", "quando",
                "grazie", "buongiorno", "buonasera", "salve",
                " per ", "mia moglie", "mio marito", "mio figlio", "mia figlia",
                "a nome", "intesta",
            )) and not low.startswith(("è ", "e ", "per ")):
                return "name"

    return "doubt"


def _preferences_are_open(parameters: dict) -> bool:
    """
    True se il cliente non ha indicato un giorno/periodo specifico:
    in quel caso mostriamo il menu dei primi giorni liberi invece
    di una lista di slot sparsi.
    """
    if not parameters:
        return True
    return not any(
        parameters.get(k)
        for k in (
            "date_from",
            "date_to",
            "period",
            "weekday",
            "week_part",
            "exact_time",
        )
    )


def _format_day_fascia(day: dict) -> str:
    parts = []
    if day.get("morning"):
        parts.append("mattina")
    if day.get("afternoon"):
        parts.append("pomeriggio")
    fascia = " e ".join(parts) if parts else "orari disponibili"
    return f"{day.get('label', day.get('date'))} ({fascia})"


def _resolve_day_choice(
    proposed_days: list,
    slot_number,
    parameters: dict,
) -> dict | None:
    """
    Risolve la scelta di un giorno dal menu proposed_days.
    Accetta numero (1-based) oppure testo che matcha il label.
    """
    if not proposed_days:
        return None

    # Per numero
    if slot_number is not None:
        try:
            idx = int(slot_number) - 1
            if 0 <= idx < len(proposed_days):
                return proposed_days[idx]
        except (TypeError, ValueError):
            pass

    # Per weekday / testo grezzo nei parameters
    weekday = (parameters.get("weekday") or "").strip().lower()
    if weekday:
        for d in proposed_days:
            label = (d.get("label") or "").lower()
            if weekday in label:
                return d

    return None


def _build_days_text(days: list) -> str:
    """Testo numerato del menu giorni (deterministico, niente AI)."""
    if not days:
        return ""
    lines = []
    for i, d in enumerate(days, 1):
        lines.append(f"{i}. {_format_day_fascia(d)}")
    return (
        "\n"
        + "\n".join(lines)
        + "\n\nScrivi il numero oppure il giorno che preferisci."
    )


def _resolve_search_slots(
    tenant: dict,
    knowledge: dict,
    parameters: dict,
    new_collected: dict,
    previous_last_slots: list,
    backend_results: dict,
) -> tuple[dict, str]:
    """
    Esegue una ricerca slot e prepara il testo da appendere alla
    risposta. Isolata qui per essere riusata IDENTICA sia da una
    prenotazione nuova (SEARCH_SLOTS) sia dal flusso di modifica
    appuntamento una volta identificato quale spostare: la ricerca del
    nuovo orario è uguale nei due casi, cambia solo cosa succede dopo
    la conferma (INSERT semplice, oppure INSERT + cancellazione del
    vecchio). "backend_results" viene aggiornato in place.
    """
    historical_backup = new_collected.get("historical_slots") or []
    modifying_backup = new_collected.get("modifying_appointment")
    current_service = parameters.get("service") or new_collected.get("service")

    backend_results["search_criteria_label"] = _describe_search_criteria(parameters)

    # Pialliamo i residui feriali a livello radice
    new_collected = {
        "service": current_service,
        "historical_slots": historical_backup,
        "last_slots": [],
        "modifying_appointment": modifying_backup,
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

    prefs = new_collected["preferences"]
    has_day_signal = any(
        prefs.get(k) for k in ("period", "weekday", "week_part", "date_from")
    )

    # Nel flusso di modifica, se il cliente indica SOLO un orario senza
    # un giorno diverso, presumiamo che voglia restare sullo stesso
    # giorno dell'appuntamento originale, invece di far scattare la
    # ricerca ampia di default.
    if modifying_backup and not has_day_signal and prefs.get("time_preference"):
        prefs["date"] = modifying_backup.get("appointment_date")

    slots_text_to_append = ""

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

    return new_collected, slots_text_to_append


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

    # Ack immediato solo all'inizio di una ricerca (niente menu/slot già
    # in sospeso): riduce i messaggi impulsivi durante l'elaborazione.
    # Sulle scelte numeriche ("2", "10:30") non serve e sarebbe rumoroso.
    _collected_early = conversation.get("collected_data") or {}
    _has_pending_menu = bool(
        _collected_early.get("last_slots")
        or _collected_early.get("proposed_days")
    )
    # Ack solo se non c'è menu in sospeso e non è un ringraziamento/saluto breve
    # post-prenotazione (es. "ok grazie"): in quel caso non serve verificare nulla.
    _text_low = (combined_text or "").strip().lower()
    _looks_like_thanks = bool(
        _text_low
        and len(_text_low) < 40
        and any(
            p in _text_low
            for p in (
                "grazie",
                "ok grazie",
                "va bene grazie",
                "perfetto grazie",
                "a presto",
                "arrivederci",
                "buona giornata",
                "buonasera",
            )
        )
        and not any(
            p in _text_low
            for p in ("prenot", "appuntament", "spost", "cancel", "annull")
        )
    )
    _sent_greeting_ack = False
    if not _has_pending_menu and not _looks_like_thanks:
        wa_info_early = tenant.get("info") or {}
        try:
            _greet = _time_of_day_greeting(tenant.get("timezone"))
            await send_whatsapp_message(
                phone,
                f"{_greet}! Un attimo, verifico…",
                wa_info_early.get("access_token") or Config.WHATSAPP_TOKEN,
                wa_info_early.get("phone_number_id") or Config.WHATSAPP_PHONE_NUMBER_ID,
            )
            _sent_greeting_ack = True
        except Exception as ack_err:
            print(f"[ACK] invio fallito (proseguo comunque): {ack_err}")

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

    # --- Gestione messaggio mentre aspettiamo il nome intestatario ---
    # Gerarchia: info/cancel deterministico → nome chiaro → AI solo nel dubbio.
    _awaiting_name = bool(
        new_collected.get("awaiting_person_name")
        or (
            (new_collected.get("pending_confirmation_slot") or new_collected.get("selected_slot"))
            and not new_collected.get("person_name")
        )
    )
    if (
        _awaiting_name
        and not parameters.get("person_name")
        and not new_collected.get("person_name")
        and (combined_text or "").strip()
    ):
        _kind = _classify_awaiting_name_message(combined_text)
        print(f"[NAME-GATE] kind={_kind} text={combined_text!r}")

        if _kind == "name":
            parameters = dict(parameters)
            parameters["person_name"] = combined_text.strip()
            parameters["confirmation"] = "yes"
            action_requested = "CONFIRM_BOOKING"
            print(f"[NAME] accettato come nome: {parameters['person_name']}")

        elif _kind == "info_question":
            # Rispondi all'info, resta in attesa del nome (non consumare lo slot)
            action_requested = "JUST_TALK"
            new_collected["awaiting_person_name"] = True
            backend_results_name_hint = True  # usato sotto per append reminder
            # marker su collected per il reminder in risposta
            new_collected["_remind_name_after_info"] = True

        elif _kind == "cancel":
            action_requested = "JUST_TALK"
            new_collected = {}
            # verrà gestito come chiusura semplice

        elif _kind == "yesno":
            # Un "sì" isolato mentre chiediamo il nome non è un nome
            action_requested = "JUST_TALK"
            new_collected["awaiting_person_name"] = True
            new_collected["_remind_name_after_info"] = True

        else:
            # Dubbio: chiedi a Step 1-light / interpretazione già fatta da Step 1
            # Se Step 1 ha già messo person_name, usalo; altrimenti resta in attesa.
            if parameters.get("person_name"):
                parameters = dict(parameters)
                parameters["confirmation"] = "yes"
                action_requested = "CONFIRM_BOOKING"
            else:
                # Prova classificazione AI dedicata (opzionale, best-effort)
                try:
                    from app.ai.intent_parser import classify_name_doubt
                    doubt = classify_name_doubt(combined_text)
                    print(f"[NAME-DOUBT AI] {doubt}")
                    if doubt.get("kind") == "name" and doubt.get("person_name"):
                        parameters = dict(parameters)
                        parameters["person_name"] = doubt["person_name"]
                        parameters["confirmation"] = "yes"
                        action_requested = "CONFIRM_BOOKING"
                    elif doubt.get("kind") == "info_question":
                        action_requested = "JUST_TALK"
                        new_collected["awaiting_person_name"] = True
                        new_collected["_remind_name_after_info"] = True
                    elif doubt.get("kind") == "cancel":
                        action_requested = "JUST_TALK"
                        new_collected = {}
                    else:
                        action_requested = "JUST_TALK"
                        new_collected["awaiting_person_name"] = True
                        new_collected["_remind_name_after_info"] = True
                except Exception as e:
                    print(f"[NAME-DOUBT] fallback: {e}")
                    action_requested = "JUST_TALK"
                    new_collected["awaiting_person_name"] = True
                    new_collected["_remind_name_after_info"] = True

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
    # ------------------------------------------------------------
    # Sotto-flusso A0: scelta di un giorno dal menu proposed_days
    # (il cliente ha risposto "1"/"2"/"martedì" mentre stavamo
    # mostrando i giorni, non ancora gli orari).
    # Step 1 può classificare questo come CONFIRM_BOOKING o
    # SEARCH_SLOTS: in entrambi i casi, se ci sono proposed_days
    # e non ci sono ancora last_slots, trattiamo la risposta come
    # selezione del giorno.
    # ------------------------------------------------------------
    _pending_days = new_collected.get("proposed_days") or []
    _has_slots = bool(new_collected.get("last_slots"))
    _choice_num = parameters.get("slot_number")
    _day_pick = None

    if (
        _pending_days
        and not _has_slots
        and action_requested in ("SEARCH_SLOTS", "CONFIRM_BOOKING")
    ):
        _day_pick = _resolve_day_choice(
            _pending_days, _choice_num, parameters
        )

    if _day_pick is not None:
        # Giorno scelto → cerca gli orari di quel giorno
        target_date = _day_pick["date"]
        new_collected["selected_day"] = _day_pick
        new_collected["proposed_days"] = []  # consumato

        try:
            times_res = search_times_for_day(
                tenant=tenant,
                knowledge=knowledge,
                collected_data=new_collected,
                target_date=target_date,
            )
            slots = times_res.get("candidate_slots") or []
            if slots:
                backend_results["slot_found"] = True
                backend_results["slots_list"] = slots
                backend_results["search_criteria_label"] = _day_pick.get("label")
                backend_results["day_pick_resolved"] = True
                new_collected["last_slots"] = slots
                labels = _slot_labels(slots)
                slots_text_to_append = (
                    "\n"
                    + "\n".join(f"{i+1}. {label}" for i, label in enumerate(labels))
                    + "\n\nQuale preferisci? (puoi rispondere con il numero o con l'orario)"
                )
            else:
                backend_results["error_type"] = "no_slots_found"
        except Exception as e:
            print(f"[BACKEND ERROR] search_times_for_day: {e}")
            backend_results["error_type"] = "technical_error"

    # ------------------------------------------------------------
    # Sotto-flusso A: Ricerca disponibilità
    # - senza giorno specifico → menu dei primi giorni liberi
    # - con giorno/periodo specifico → slot diretti (comportamento classico)
    # ------------------------------------------------------------
    elif action_requested == "SEARCH_SLOTS":
        if _preferences_are_open(parameters):
            # Nessuna preferenza di giorno: ricerca ampia (orizzonte tenant,
            # tipicamente 30 giorni) e prime disponibilità cronologiche.
            parameters = dict(parameters or {})
            # Forza ricerca senza vincoli temporali stretti
            parameters["period"] = None
            parameters["weekday"] = None
            parameters["week_part"] = None
            parameters["date_from"] = None
            parameters["date_to"] = None
            # ignore_preferences lato collected
            prefs = dict(new_collected.get("preferences") or {})
            prefs["ignore_preferences"] = True
            new_collected["preferences"] = prefs
            new_collected, slots_text_to_append = _resolve_search_slots(
                tenant, knowledge, parameters, new_collected, previous_last_slots, backend_results
            )
            if backend_results.get("slot_found"):
                backend_results["search_criteria_label"] = None  # intro fissa sotto
                backend_results["open_search"] = True
                # Sostituisci l'intro del testo slot con la frase richiesta
                labels = _slot_labels(new_collected.get("last_slots") or [])
                if labels:
                    slots_text_to_append = (
                        "\n"
                        + "\n".join(f"{i+1}. {label}" for i, label in enumerate(labels))
                        + "\n\nQuale preferisci? (puoi rispondere con il numero o con l'orario)"
                    )
        else:
            # Comportamento classico: ricerca slot con criteri specifici
            new_collected, slots_text_to_append = _resolve_search_slots(
                tenant, knowledge, parameters, new_collected, previous_last_slots, backend_results
            )

    # Sotto-flusso B: Prenotazione Deterministica e Transazione (Step 4)
    elif action_requested == "CONFIRM_BOOKING":
        all_slots_in_memory = (new_collected.get("last_slots") or []) + (new_collected.get("historical_slots") or [])
        # Slot già scelto in un turno precedente (manca solo il nome):
        # è contesto valido anche senza last_slots ancora in lista.
        has_chosen_slot = bool(
            new_collected.get("pending_confirmation_slot")
            or new_collected.get("selected_slot")
        )

        if not all_slots_in_memory and not has_chosen_slot:
            # Se ci sono ancora proposed_days non risolti, non è
            # "no context": il cliente ha forse risposto in modo
            # non riconoscibile al menu giorni.
            if new_collected.get("proposed_days"):
                backend_results["error_type"] = "day_choice_unclear"
            else:
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

            # Nome intestatario in arrivo: usa slot in pending o selected.
            if (
                parameters.get("person_name") or new_collected.get("person_name")
            ):
                resolved_slot = (
                    pending
                    or new_collected.get("selected_slot")
                )
                new_collected["pending_slot_number"] = None
                new_collected["pending_exact_time"] = None
                new_collected["awaiting_person_name"] = False

            elif (
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
                    # Se exact_time è solo il numero dello slot trasformato
                    # in orario fantasma (es. slot 3 → "03:00"), ignoralo:
                    # il cliente ha scelto il numero, non un orario.
                    phantom_from_index = False
                    if wanted and slot_number is not None:
                        try:
                            wh, wm = wanted.split(":")
                            if int(wh) == int(slot_number) and int(wm) == 0:
                                phantom_from_index = True
                        except (TypeError, ValueError):
                            pass

                    if wanted and candidate.get("time") != wanted and not phantom_from_index:
                        mismatch_slot = candidate
                    else:
                        resolved_slot = candidate
                elif exact_time is None:
                    # "slot_number" non è un indice valido tra quelli
                    # proposti (es. il cliente ha scritto "10" intendendo
                    # le 10:00, non "opzione 10", e non c'era nessun altro
                    # indizio testuale di orario da riportare come
                    # exact_time separato). Lo accettiamo SOLO se combacia
                    # esattamente con l'orario di uno slot realmente
                    # proposto: se non c'è corrispondenza, l'ambiguità non
                    # si risolve da sola e si ricade nel messaggio
                    # "non trovato" più sotto.
                    as_time = _normalize_time_str(slot_number)
                    if as_time:
                        for slot in all_slots_in_memory:
                            if slot.get("time") == as_time:
                                resolved_slot = slot
                                break

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

                # Non richiedere di nuovo la conferma breve se è già
                # arrivato il nome intestatario (passo successivo).
                _need_short_confirm = (
                    parameters.get("confirmation") != "yes"
                    and parameters.get("slot_number") is not None
                    and not parameters.get("person_name")
                    and not new_collected.get("person_name")
                )
                if _need_short_confirm:
                    new_collected["pending_confirmation_slot"] = resolved_slot
                    backend_results["error_type"] = "slot_choice_confirm"
                    backend_results["confirm_slot_number"] = parameters.get("slot_number")
                    backend_results["confirm_slot_time"] = (resolved_slot.get("time") or "")[:5]
                    backend_results["confirm_slot_label"] = _slot_labels([resolved_slot])[0]

                if not _need_short_confirm:
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

                            # Se stiamo spostando un appuntamento esistente,
                            # il nuovo è già scritto con successo: SOLO ORA
                            # cancelliamo il vecchio (soft-delete, status
                            # "cancelled", storico mantenuto). Se qualcosa va
                            # storto prima di questo punto, il vecchio
                            # appuntamento non viene mai toccato.
                            modifying = new_collected.get("modifying_appointment")
                            if modifying:
                                try:
                                    appointment_repo.cancel_appointment(tenant["id"], modifying["id"])
                                    backend_results["cancelled_old_appointment_label"] = _appointment_label(modifying)
                                except Exception as e:
                                    print(f"[BACKEND ERROR] Nuovo appuntamento confermato, ma cancellazione del vecchio ({modifying.get('id')}) fallita: {e}")
                                    backend_results["old_appointment_cancel_failed"] = True

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
                                new_collected["pending_confirmation_slot"] = resolved_slot
                                new_collected["selected_slot"] = resolved_slot
                                new_collected["awaiting_person_name"] = True
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

    # Sotto-flusso B2: Modifica di un appuntamento esistente.
    # Riusa integralmente la ricerca/scelta/conferma già costruita per
    # una prenotazione nuova (Sotto-flusso A e B): la "modifica" vive
    # solo nel campo "modifying_appointment" di collected_data, letto
    # solo nell'ultimissimo istante (dopo che il NUOVO appuntamento è
    # già stato scritto con successo, si cancella il vecchio - mai il
    # contrario, così il cliente non perde mai quello che aveva).
    elif action_requested == "MODIFY_BOOKING":
        modifying = new_collected.get("modifying_appointment")

        if modifying:
            # Appuntamento da spostare già identificato e confermato in
            # un turno precedente: da qui è identico a una prenotazione
            # nuova.
            new_collected, slots_text_to_append = _resolve_search_slots(
                tenant, knowledge, parameters, new_collected, previous_last_slots, backend_results
            )
        else:
            pending_appt = new_collected.get("pending_confirmation_appointment")
            confirmation = parameters.get("confirmation")

            if pending_appt and confirmation == "no":
                # Non è quello: passiamo al secondo candidato, se c'è.
                rejected = new_collected.get("rejected_appointment_ids") or []
                rejected.append(pending_appt.get("id"))
                new_collected["rejected_appointment_ids"] = rejected
                new_collected["pending_confirmation_appointment"] = None

                candidates = appointment_repo.list_upcoming_for_customer(
                    tenant_id=tenant["id"], customer_id=customer["id"], limit=2
                )
                next_candidate = next((a for a in candidates if a.get("id") not in rejected), None)

                if next_candidate:
                    new_collected["pending_confirmation_appointment"] = next_candidate
                    backend_results["error_type"] = "appointment_confirmation_needed"
                    backend_results["appointment_confirmation_label"] = _appointment_label(next_candidate)
                else:
                    backend_results["error_type"] = "no_more_appointments_to_propose"

            elif pending_appt:
                # Confermato: da qui in poi cerchiamo il nuovo orario
                # esattamente come per una prenotazione nuova.
                new_collected["modifying_appointment"] = pending_appt
                new_collected["pending_confirmation_appointment"] = None
                new_collected["rejected_appointment_ids"] = None

                has_new_preference = any(
                    parameters.get(k)
                    for k in ("period", "weekday", "week_part", "date_from", "time_preference", "exact_time")
                )

                if has_new_preference:
                    new_collected, slots_text_to_append = _resolve_search_slots(
                        tenant, knowledge, parameters, new_collected, previous_last_slots, backend_results
                    )
                else:
                    backend_results["error_type"] = "ask_new_time_preference"

            else:
                # Primo turno del flusso: individuiamo l'appuntamento più
                # vicino nel tempo e chiediamo conferma, uno alla volta
                # (mai una lista da disambiguare: al massimo 2 candidati).
                candidates = appointment_repo.list_upcoming_for_customer(
                    tenant_id=tenant["id"], customer_id=customer["id"], limit=2
                )

                if not candidates:
                    backend_results["error_type"] = "no_appointment_to_modify"
                else:
                    first = candidates[0]
                    new_collected["pending_confirmation_appointment"] = first
                    new_collected["rejected_appointment_ids"] = []
                    backend_results["error_type"] = "appointment_confirmation_needed"
                    backend_results["appointment_confirmation_label"] = _appointment_label(first)

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
    history_str = ""
    for m in recent[-5:]:
        role_label = "Cliente" if m.get("role") == "user" else "Assistente"
        history_str += f"- {role_label}: {m.get('content') or m.get('text', '')}\n"

    # Ogni esito di CONFIRM_BOOKING, e i casi di identificazione/errore
    # tecnico di MODIFY_BOOKING, sono già coperti da un messaggio fisso
    # in app/templates/messages.py qui sotto: sono i momenti in cui la
    # precisione dei dati (soldi/tempo delle persone) conta più del tono,
    # quindi evitiamo del tutto la scrittura libera dell'AI, invece di
    # generarla e poi scartarla. L'AI resta usata solo per l'introduzione
    # a una ricerca slot riuscita e per le chiacchiere/saluti, dove un
    # po' di variabilità naturale è un valore, non un rischio.
    _DETERMINISTIC_MODIFY_ERROR_TYPES = {
        "no_appointment_to_modify",
        "appointment_confirmation_needed",
        "no_more_appointments_to_propose",
        "ask_new_time_preference",
    }
    skip_ai_response = (
        (
            action_requested == "CONFIRM_BOOKING"
            and not backend_results.get("day_pick_resolved")
        )
        or (
            action_requested == "MODIFY_BOOKING"
            and backend_results.get("error_type") in _DETERMINISTIC_MODIFY_ERROR_TYPES
        )
        or backend_results.get("error_type") == "technical_error"
        or backend_results.get("error_type") == "slot_choice_confirm"
    )

    if skip_ai_response:
        reply_text = ""
    else:
        print("[STEP 3] Invocazione AI Redattrice con i dati reali del backend...")
        knowledge_texts = {
            "services_text": (knowledge or {}).get("services_text") or "",
            "locations_text": (knowledge or {}).get("locations_text") or "",
            "working_hours_text": (knowledge or {}).get("working_hours_text") or "",
        }
        reply_text = run_step3_response(
            message_text=combined_text,
            backend_results=backend_results,
            history_text=history_str,
            knowledge_texts=knowledge_texts,
        )

    if slots_text_to_append and backend_results.get("slot_found"):
        # Copre: SEARCH_SLOTS classico, menu giorni, scelta giorno→orari, MODIFY
        reply_text = f"{reply_text}\n{slots_text_to_append}"
    elif action_requested == "MODIFY_BOOKING" and backend_results["slot_found"] and slots_text_to_append:
        reply_text = f"{reply_text}\n{slots_text_to_append}"

    # --- JUST_TALK: dopo la risposta informativa, riproponi lo stato in sospeso ---
    # Il ramo JUST_TALK non tocca mai new_collected (stato intatto).
    # Se c'erano slot proposti, li ri-elenca in modo deterministico così
    # il cliente non deve ripescare la cronologia.
    elif action_requested == "JUST_TALK":
        # Ringraziamento / saluto di chiusura senza negoziazione attiva
        if _looks_like_thanks and not (
            new_collected.get("last_slots")
            or new_collected.get("proposed_days")
            or new_collected.get("awaiting_person_name")
        ):
            reply_text = "Prego, a presto!"
            pending_slots = []
            pending_days = []
        pending_slots = new_collected.get("last_slots") or []
        pending_days = new_collected.get("proposed_days") or []
        if pending_slots and reply_text and not _looks_like_thanks:
            labels = _slot_labels(pending_slots)
            resume_block = (
                "\n\nTornando alla prenotazione in corso, queste erano le disponibilità:\n"
                + "\n".join(f"{i+1}. {label}" for i, label in enumerate(labels))
                + "\n\nQuale preferisci? (puoi rispondere con il numero o con l'orario)"
            )
            reply_text = f"{reply_text}{resume_block}"
        elif pending_days and reply_text:
            resume_block = (
                "\n\nTornando alla prenotazione in corso, queste erano le disponibilità:\n"
                + "\n".join(f"{i}. {_format_day_fascia(d)}" for i, d in enumerate(pending_days, 1))
                + "\n\nScrivi il numero oppure il giorno che preferisci."
            )
            reply_text = f"{reply_text}{resume_block}"


        # Se eravamo in attesa del nome e abbiamo solo risposto a una info,
        # ricorda di fornire nome e cognome.
        if new_collected.get("_remind_name_after_info") and reply_text:
            reply_text = (
                reply_text.rstrip()
                + "\n\nPer confermare l'appuntamento mi serve nome e cognome dell'intestatario."
            )
            new_collected.pop("_remind_name_after_info", None)
            new_collected["awaiting_person_name"] = True

    elif backend_results.get("booking_success"):
        old_label = backend_results.get("cancelled_old_appointment_label")
        new_label = backend_results.get("confirmed_slot_label")
        reply_text = (
            tpl.booking_moved(old_label, new_label)
            if old_label
            else tpl.booking_confirmed_new(new_label)
        )
    elif backend_results.get("error_type") == "slot_choice_confirm":
        n = backend_results.get("confirm_slot_number")
        t = backend_results.get("confirm_slot_time") or ""
        reply_text = f"Confermi quindi di aver scelto lo slot n.{n} alle ore {t}?"
    elif backend_results.get("error_type") == "slot_time_mismatch":
        reply_text = tpl.slot_time_mismatch(backend_results.get("mismatch_slot_label"))
    elif backend_results.get("error_type") == "missing_data":
        reply_text = tpl.booking_missing_name(backend_results.get("failed_slot_label"))
    elif backend_results.get("error_type") == "slot_occupied":
        reply_text = tpl.booking_slot_occupied(backend_results.get("failed_slot_label"))
    elif backend_results.get("error_type") == "slot_not_found_in_memory":
        reply_text = tpl.SLOT_NOT_FOUND_IN_MEMORY
    elif backend_results.get("error_type") == "day_choice_unclear":
        # Riproponi il menu giorni in modo deterministico
        days = new_collected.get("proposed_days") or []
        if days:
            reply_text = (
                "Non ho capito quale giorno preferisci.\n"
                + _build_days_text(days).lstrip("\n")
            )
        else:
            reply_text = tpl.UNCLEAR
    elif backend_results.get("error_type") == "no_context_available":
        reply_text = tpl.CONVERSATION_EXPIRED
    elif backend_results.get("error_type") == "no_appointment_to_modify":
        reply_text = tpl.NO_APPOINTMENT_TO_MODIFY
    elif backend_results.get("error_type") == "appointment_confirmation_needed":
        reply_text = tpl.appointment_confirmation_needed(backend_results.get("appointment_confirmation_label"))
    elif backend_results.get("error_type") == "no_more_appointments_to_propose":
        reply_text = tpl.NO_MORE_APPOINTMENTS_TO_PROPOSE
    elif backend_results.get("error_type") == "ask_new_time_preference":
        reply_text = tpl.ASK_NEW_TIME_PREFERENCE
    elif backend_results.get("error_type") == "technical_error":
        reply_text = tpl.TECHNICAL_ERROR

    # Il saluto iniziale ("Buongiorno"/"Buon pomeriggio"/"Buonasera") è
    # calcolato qui dal backend in base all'ora locale reale del tenant,
    # non lasciato all'AI, e antepposto SOLO al primo messaggio di una
    # conversazione nuova (o resettata da un saluto del cliente).
    # Saluto solo al primo messaggio utile, e SOLO se non l'abbiamo già
    # messo nell'ack ("Buongiorno! Un attimo, verifico…").
    if is_conversation_start and reply_text and not _sent_greeting_ack:
        greeting = _time_of_day_greeting(tenant.get("timezone"))
        reply_text = f"{greeting}! {reply_text}"

    # ------------------------------------------------------------
    # STEP 5: CONSOLIDAMENTO E STRUTTURAZIONE INVIO FINALE
    # ------------------------------------------------------------
    print("[STEP 5] Salvataggio finale del DB e invio su WhatsApp Cloud API...")
    # Il workflow riflette lo stato reale: "booking" quando c'è una
    # negoziazione viva di qualunque tipo (proposta di slot in attesa di
    # scelta, dato mancante, chiarimento di coerenza, identificazione o
    # ricerca nel flusso di modifica), "idle" in ogni altro caso. Prima
    # veniva forzato a "idle" ogni volta che l'azione non era SEARCH_SLOTS,
    # il che avrebbe esposto anche CONFIRM_BOOKING/MODIFY_BOOKING a metà
    # negoziazione al reset-su-saluto.
    has_live_negotiation = bool(
        new_collected.get("last_slots")
        or new_collected.get("proposed_days")
        or new_collected.get("pending_confirmation_slot")
        or new_collected.get("selected_slot")
        or new_collected.get("awaiting_person_name")
        or new_collected.get("pending_slot_number")
        or new_collected.get("pending_exact_time")
        or new_collected.get("modifying_appointment")
        or new_collected.get("pending_confirmation_appointment")
    )

    if has_live_negotiation:
        workflow_to_save = WORKFLOW_BOOKING
        if new_collected.get("proposed_days") and not new_collected.get("last_slots"):
            step_to_save = STEP_SHOWING_DAYS
        elif new_collected.get("last_slots"):
            step_to_save = STEP_SHOWING_TIMES if new_collected.get("selected_day") else STEP_SHOWING_SLOTS
        else:
            step_to_save = STEP_SHOWING_SLOTS
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
