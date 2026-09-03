import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.ai.intent_parser import (
    run_step1_analysis,
    run_step3_response,
)
from app.booking.engine import (
    create_booking,
    search_availability,
)
from app.config import Config
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


async def process_messages(
    messages: list[dict],
):
    if not messages:
        return

    last = messages[-1]

    phone = last["from"]
    business_phone = last["to"]

    combined_text = "\n".join(
        message["message"].strip()
        for message in messages
        if message.get("message")
    )

    print(
        f"=== ENGINE PROCESS "
        f"{len(messages)} MSG DA {phone} ==="
    )

    tenant = get_tenant_by_whatsapp_number(
        business_phone
    )

    if not tenant:
        return

    tenant_id = tenant["id"]

    customer = get_or_create_customer(
        tenant_id,
        phone,
    )

    conversation, expired = (
        get_or_create_conversation(
            tenant["id"],
            customer["id"],
            phone,
        )
    )

    recent = (
        conversation.get("recent_messages")
        or []
    )

    for message in messages:
        recent = append_message(
            conversation["id"],
            role="user",
            content=message["message"],
            current_messages=recent,
        )

    conversation["recent_messages"] = recent

    if expired:
        wa_info = tenant.get("info") or {}

        await send_whatsapp_message(
            phone,
            tpl.CONVERSATION_EXPIRED,
            wa_info.get("access_token")
            or Config.WHATSAPP_TOKEN,
            wa_info.get("phone_number_id")
            or Config.WHATSAPP_PHONE_NUMBER_ID,
        )

        append_message(
            conversation["id"],
            role="assistant",
            content=tpl.CONVERSATION_EXPIRED,
            current_messages=conversation.get(
                "recent_messages"
            ),
        )

        return

    knowledge = get_tenant_knowledge(
        tenant_id
    )

    context = build_context(
        tenant=tenant,
        customer=customer,
        conversation=conversation,
        message={
            "message": combined_text,
            "message_id": last.get(
                "message_id"
            ),
            "received_at": last.get(
                "received_at"
            ),
        },
        knowledge=knowledge,
    )

    # ========================================================
    # STEP 1 - AI ANALISTA
    # ========================================================

    print(
        "[STEP 1] Esecuzione AI Analista..."
    )

    step1_result = run_step1_analysis(
        message_text=combined_text,
        full_context_dict=context,
    )

    action_requested = (
        step1_result.get(
            "action_requested",
            "JUST_TALK",
        )
    )

    parameters = (
        step1_result.get("parameters")
        or {}
    )

    collected = (
        conversation.get("collected_data")
        or {}
    )

    new_collected = dict(collected)

    backend_results = {
        "action_executed": action_requested,
        "slot_found": False,
        "slots_list": [],
        "historical_slots_proposti_prima": (
            new_collected.get(
                "historical_slots"
            )
            or []
        ),
        "booking_success": False,
        "is_studio_closed": False,
        "is_studio_full": False,
        "error_type": None,
    }

    slots_text_to_append = ""

    # ========================================================
    # STEP 2 & 4 - BACKEND DETERMINISTICO
    # ========================================================

    print(
        f"[STEP 2/4] Elaborazione backend "
        f"per: {action_requested}"
    )

    if action_requested == "SEARCH_SLOTS":
        historical_backup = (
            new_collected.get(
                "historical_slots"
            )
            or []
        )

        current_service = (
            parameters.get("service")
            or new_collected.get("service")
        )

        # Pialliamo i residui radice.
        new_collected = {
            "service": current_service,
            "historical_slots": historical_backup,
            "last_slots": [],
            "preferences": {
                "date_from": parameters.get(
                    "date_from"
                ),
                "date_to": parameters.get(
                    "date_to"
                ),
                "time_preference": parameters.get(
                    "time_preference"
                ),
                "exact_time": parameters.get(
                    "exact_time"
                ),
                "date": None,
                "period": None,
                "weekday": None,
                "ignore_preferences": None,
            },
        }

        collected = new_collected.copy()

        try:
            booking_res = search_availability(
                tenant=tenant,
                knowledge=knowledge,
                collected_data=new_collected,
            )

            slots = (
                booking_res.get(
                    "candidate_slots"
                )
                or []
            )

            result = (
                booking_res.get("result")
                or {}
            )

            backend_results[
                "is_studio_closed"
            ] = result.get(
                "is_studio_closed",
                False,
            )

            backend_results[
                "is_studio_full"
            ] = result.get(
                "is_studio_full",
                False,
            )

            if slots:
                backend_results[
                    "slot_found"
                ] = True

                backend_results[
                    "slots_list"
                ] = slots

                new_collected[
                    "last_slots"
                ] = slots

                labels = _slot_labels(
                    slots
                )

                slots_text_to_append = (
                    "\n"
                    + "\n".join(
                        f"{index + 1}. {label}"
                        for index, label
                        in enumerate(labels)
                    )
                    + "\n\nQuale preferisci? "
                    "(puoi rispondere con il "
                    "numero o con l'orario)"
                )

            else:
                backend_results[
                    "error_type"
                ] = "no_slots_found"

                if result.get(
                    "search_was_narrow"
                ):
                    backend_results[
                        "error_type"
                    ] = "no_slots_narrow"

        except Exception as exc:
            print(
                "[BACKEND ERROR] "
                "Errore in search_availability: "
                f"{exc}"
            )

            backend_results[
                "error_type"
            ] = "technical_error"

    elif action_requested == "CONFIRM_BOOKING":
        slot_number = parameters.get(
            "slot_number"
        )

        exact_time = parameters.get(
            "exact_time"
        )

        resolved_slot = None

        all_slots_in_memory = (
            new_collected.get(
                "last_slots"
            )
            or []
        ) + (
            new_collected.get(
                "historical_slots"
            )
            or []
        )

        # ----------------------------------------------------
        # Risoluzione tramite numero dello slot.
        # ----------------------------------------------------

        if slot_number is not None:
            try:
                index = int(slot_number) - 1

                last_slots = (
                    new_collected.get(
                        "last_slots"
                    )
                    or []
                )

                if (
                    0 <= index < len(last_slots)
                ):
                    resolved_slot = (
                        last_slots[index]
                    )

            except (
                TypeError,
                ValueError,
            ):
                pass

        # ----------------------------------------------------
        # Risoluzione tramite orario esatto.
        # ----------------------------------------------------

        elif exact_time:
            wanted = str(
                exact_time
            ).strip()

            for slot in all_slots_in_memory:
                if (
                    slot.get("time")
                    == wanted
                    or wanted
                    in slot.get("label", "")
                ):
                    resolved_slot = slot
                    break

        # ----------------------------------------------------
        # Se abbiamo trovato lo slot, lo salviamo.
        # ----------------------------------------------------

        if resolved_slot:
            new_collected[
                "selected_slot"
            ] = resolved_slot

            if parameters.get(
                "person_name"
            ):
                new_collected[
                    "person_name"
                ] = parameters.get(
                    "person_name"
                )

            try:
                booking_res = create_booking(
                    tenant=tenant,
                    knowledge=knowledge,
                    collected_data=new_collected,
                    customer=customer,
                    phone_number=phone,
                )

                if booking_res.get(
                    "result",
                    {},
                ).get(
                    "success"
                ):
                    backend_results[
                        "booking_success"
                    ] = True

                    new_collected = {}

                else:
                    backend_results[
                        "error_type"
                    ] = "slot_occupied"

            except Exception as exc:
                print(
                    "[BACKEND ERROR] "
                    "Errore in create_booking: "
                    f"{exc}"
                )

                backend_results[
                    "error_type"
                ] = "technical_error"

        else:
            backend_results[
                "error_type"
            ] = "slot_not_found_in_memory"

    # ========================================================
    # JUST TALK / ALTRI CASI
    # ========================================================

    else:
        if parameters.get("service"):
            new_collected[
                "service"
            ] = parameters.get(
                "service"
            )

        if parameters.get("person_name"):
            new_collected[
                "person_name"
            ] = parameters.get(
                "person_name"
            )

        lowered_text = combined_text.lower()

        if (
            "lascia stare" in lowered_text
            or "annull" in lowered_text
            or "basta" in lowered_text
            or "grazie" in lowered_text
        ):
            new_collected = {}

            backend_results[
                "action_executed"
            ] = "RESET_COMPLETED"

    # ========================================================
    # CONSOLIDAMENTO DEGLI SLOT STORICI
    # ========================================================

    if (
        new_collected
        and collected.get("last_slots")
    ):
        historical = (
            new_collected.get(
                "historical_slots"
            )
            or []
        )

        for old_slot in collected[
            "last_slots"
        ]:
            if old_slot not in historical:
                historical.append(
                    old_slot
                )

        new_collected[
            "historical_slots"
        ] = historical[-15:]

    # ========================================================
    # STEP 3 - AI REDATTRICE
    # ========================================================

    print(
        "[STEP 3] Invocazione AI Redattrice "
        "con i dati reali del backend..."
    )

    history_str = ""

    for message in recent[-5:]:
        role_label = (
            "Cliente"
            if message.get("role") == "user"
            else "Assistente"
        )

        history_str += (
            f"- {role_label}: "
            f"{message.get('content') "
            f"or message.get('text', '')}\n"
        )

    reply_text = run_step3_response(
        message_text=combined_text,
        backend_results=backend_results,
        history_text=history_str,
    )

    if (
        action_requested == "SEARCH_SLOTS"
        and backend_results["slot_found"]
    ):
        reply_text = (
            f"{reply_text}\n"
            f"{slots_text_to_append}"
        )

    elif (
        action_requested == "CONFIRM_BOOKING"
        and backend_results["error_type"]
        == "slot_not_found_in_memory"
    ):
        reply_text = (
            "Scusami, non sono riuscito "
            "a trovare lo slot richiesto. "
            "Potresti indicarmi il numero "
            "esatto tra quelli proposti sopra?"
        )

    # ========================================================
    # STEP 5 - SALUTO FINALE / CONSOLIDAMENTO
    # ========================================================

    print(
        "[STEP 5] Salvataggio finale del DB "
        "e invio su WhatsApp Cloud API..."
    )

    update_conversation(
        conversation["id"],
        collected_data=new_collected,
        workflow="idle",
        step="none",
    )

    if reply_text:
        wa_info = tenant.get("info") or {}

        await send_whatsapp_message(
            phone,
            reply_text,
            wa_info.get("access_token")
            or Config.WHATSAPP_TOKEN,
            wa_info.get("phone_number_id")
            or Config.WHATSAPP_PHONE_NUMBER_ID,
        )

        append_message(
            conversation["id"],
            role="assistant",
            content=reply_text,
            current_messages=conversation.get(
                "recent_messages"
            ),
        )

    print(
        "=== [LOOP CHIUSO FELICEMENTE] ==="
    )
