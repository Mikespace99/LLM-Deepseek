"""
Punto di ingresso HTTP: webhook WhatsApp + health + UI web.
Tutta la logica conversazionale è in webhook_new_pipeline / orchestrator.
"""

import hashlib
import hmac
import json
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from app.config import Config
from app.message_buffer import message_buffer
from app.web.routes import router as web_router
from app.webhook import handle_whatsapp_message

app = FastAPI(
    title="AI Booking",
    version="2.0.0",
)

app.include_router(web_router)


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0.0"}


@app.get("/api/status")
def api_status():
    return {"status": "running", "message": "Backend WhatsApp AI attivo"}


# ============================================================
# WHATSAPP: verifica firma Meta
# ============================================================

def _verify_meta_signature(
    raw_body: bytes,
    signature_header: str | None,
    app_secret: str,
) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    received = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, received)


# ============================================================
# WHATSAPP: verifica webhook (GET)
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
# WHATSAPP: ricezione messaggi (POST)
# ============================================================

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    raw_body = await request.body()

    if Config.WHATSAPP_APP_SECRET:
        signature_header = request.headers.get("x-hub-signature-256")
        if not _verify_meta_signature(
            raw_body,
            signature_header,
            Config.WHATSAPP_APP_SECRET,
        ):
            print("--- WEBHOOK RIFIUTATO: firma non valida ---")
            return PlainTextResponse("Forbidden", status_code=403)

    try:
        payload = json.loads(raw_body)
        entry = payload["entry"][0]
        change = entry["changes"][0]
        value = change["value"]

        messages = value.get("messages")
        if not messages:
            return {"status": "ignored"}

        msg = messages[0]
        msg_type = msg.get("type")
        if msg_type not in ("text", "interactive"):
            return {"status": "ignored"}

        if msg_type == "text":
            text_body = msg["text"]["body"]
        else:
            # Click su bottone o riga di lista → stesso trattamento del testo
            interactive = msg.get("interactive", {})
            reply = (
                interactive.get("button_reply")
                or interactive.get("list_reply")
                or {}
            )
            text_body = reply.get("title", "")
            if not text_body:
                return {"status": "ignored"}

        metadata = value.get("metadata", {})
        timestamp = msg.get("timestamp")
        received_at = (
            datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()
            if timestamp
            else datetime.now(timezone.utc).isoformat()
        )

        message_data = {
            "to": metadata.get("display_phone_number"),
            "from": msg.get("from"),
            "message": text_body,
            "message_id": msg.get("id"),
            "received_at": received_at,
        }

        await message_buffer.add_message(
            message_data["from"],
            message_data,
            process_messages,
        )
        return {"status": "accepted"}

    except Exception as exc:
        print(f"[WEBHOOK ERROR] {exc}")
        return {"status": "error"}


# ============================================================
# PIPELINE: solo nuova
# ============================================================

async def process_messages(messages: list[dict]):
    if not messages:
        return

    last = messages[-1]
    phone = last["from"]
    business_phone = last["to"]

    combined_text = "\n".join(
        m["message"].strip() for m in messages if m.get("message")
    )
    print(f"=== PROCESS {len(messages)} MSG da {phone} ===")

    await handle_whatsapp_message(
        phone,
        business_phone,
        combined_text,
    )
