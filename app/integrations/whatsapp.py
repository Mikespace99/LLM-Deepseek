"""
Client asincrono per inviare messaggi WhatsApp tramite Cloud API.
Supporta parametri multi-tenant dinamici.
"""

import logging
import httpx
from app.config import Config

logger = logging.getLogger(__name__)

# Versione Graph API stabile
GRAPH_API_VERSION = "v21.0"


async def send_whatsapp_message(
    to_phone: str, 
    text: str, 
    access_token: str | None = None, 
    phone_number_id: str | None = None
) -> dict | None:
    """
    Invia un messaggio di testo in modo ASINCRONO.
    Usa i token passati (multi-tenant) oppure fa il fallback su quelli globali.
    NON solleva eccezioni verso il chiamante.
    """
    # Usa le credenziali passate dal tenant, altrimenti fa fallback su Config globale
    token = access_token or Config.WHATSAPP_TOKEN
    phone_id = phone_number_id or Config.WHATSAPP_PHONE_NUMBER_ID

    if not token or not phone_id:
        logger.error("[whatsapp] Token o Phone Number ID mancanti (invio fallito)")
        return None

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_id}/messages"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text},
    }

    try:
        # Usiamo AsyncClient per non bloccare i worker di FastAPI
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, headers=headers, json=payload)

            print("=== WHATSAPP SEND ===")
            print(f"Status: {resp.status_code}")
            print(f"Body: {resp.text[:500]}")

            if resp.status_code >= 400:
                logger.error(f"[whatsapp] Errore Meta {resp.status_code}: {resp.text}")
                return None

            return resp.json()

    except httpx.TimeoutException as e:
        logger.error(f"[whatsapp] Timeout durante l'invio: {e}")
        return None
    except httpx.RequestError as e:
        logger.error(f"[whatsapp] Errore di rete durante l'invio: {e}")
        return None
    except Exception as e:
        logger.error(f"[whatsapp] Errore imprevisto durante l'invio: {e}")
        return None
