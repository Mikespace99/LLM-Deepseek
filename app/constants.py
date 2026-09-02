# ============================================================
# WORKFLOWS
# ============================================================
WORKFLOW_IDLE = "idle"
WORKFLOW_BOOKING = "booking"
WORKFLOW_RESCHEDULE = "reschedule"
WORKFLOW_CANCEL = "cancel"
WORKFLOW_INFO = "info"
WORKFLOW_REQUEST_HUMAN = "request_human"

# ============================================================
# STEPS (principalmente per booking)
# ============================================================
STEP_NONE = "none"
STEP_ASKING_SERVICE = "asking_service"
STEP_ASKING_DATE = "asking_date"
STEP_ASKING_TIME = "asking_time"
STEP_SHOWING_SLOTS = "showing_slots"
STEP_CONFIRMING_SLOT = "confirming_slot"
STEP_ASKING_PERSON_NAME = "asking_person_name"
STEP_CONFIRMING = "confirming"
STEP_COMPLETED = "completed"

# ============================================================
# INTENT riconosciuti da AI#1
# ============================================================
INTENT_GREETING = "greeting"
INTENT_BOOK = "book_appointment"
INTENT_RESCHEDULE = "reschedule_appointment"
INTENT_CANCEL = "cancel_appointment"
INTENT_INFO = "get_info"
INTENT_SLOT_SELECTION = "slot_selection"
INTENT_CONFIRM = "confirm"
INTENT_AFFIRM = "affirm"
INTENT_DENY = "deny"
INTENT_REQUEST_HUMAN = "request_human"
INTENT_ABANDON = "abandon"
INTENT_UNCLEAR = "unclear"
INTENT_CHANGE_AVAILABILITY = "change_availability"

# ============================================================
# Mapping intent → workflow
# ============================================================
INTENT_TO_WORKFLOW = {
    INTENT_BOOK: WORKFLOW_BOOKING,
    INTENT_RESCHEDULE: WORKFLOW_RESCHEDULE,
    INTENT_CANCEL: WORKFLOW_CANCEL,
    INTENT_INFO: WORKFLOW_INFO,
    INTENT_REQUEST_HUMAN: WORKFLOW_REQUEST_HUMAN,
}

# Intent considerati "laterali" (non cambiano il workflow attivo)
LATERAL_INTENTS = {
    INTENT_INFO,
}

# Intent che chiudono / abbandonano il flusso corrente
ABANDON_INTENTS = {
    INTENT_ABANDON,
}

# ============================================================
# Azioni esplicite verso n8n (per il workflow "booking")
# ============================================================
N8N_ACTION_SEARCH_AVAILABILITY = "search_availability"
N8N_ACTION_CREATE_BOOKING = "create_booking"
