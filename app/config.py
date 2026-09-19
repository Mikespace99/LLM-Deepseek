import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Supabase
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

    # OpenAI / AI
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    AI_MODEL_INTENT = os.getenv("AI_MODEL_INTENT", "gpt-4o-mini")
    AI_MODEL_RESPONSE = os.getenv("AI_MODEL_RESPONSE", "gpt-4o-mini")

    # WhatsApp
    WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
    WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
    # App secret Meta, usato per verificare la firma X-Hub-Signature-256
    # sui webhook in ingresso (protegge da payload falsi/iniettati).
    WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET")

    # n8n webhooks
    N8N_BOOKING_WEBHOOK = os.getenv("N8N_BOOKING_WEBHOOK")
    N8N_RESCHEDULE_WEBHOOK = os.getenv("N8N_RESCHEDULE_WEBHOOK")
    N8N_CANCEL_WEBHOOK = os.getenv("N8N_CANCEL_WEBHOOK")
    N8N_AVAILABILITY_WEBHOOK = os.getenv("N8N_AVAILABILITY_WEBHOOK")

    # Business rules
    DEFAULT_SLOT_SEARCH_DAYS = int(os.getenv("DEFAULT_SLOT_SEARCH_DAYS", "30"))
    CONVERSATION_TIMEOUT_MINUTES = int(os.getenv("CONVERSATION_TIMEOUT_MINUTES", "15"))
    MAX_RECENT_MESSAGES = int(os.getenv("MAX_RECENT_MESSAGES", "6"))

    # Debounce: secondi di attesa dopo l'ultimo messaggio prima di processare
    MESSAGE_DEBOUNCE_SECONDS = float(os.getenv("MESSAGE_DEBOUNCE_SECONDS", "10"))
