"""
Modelli tipizzati del ConversationContext.

Questo modulo e' NUOVO e isolato: non sostituisce (ancora) `builder.py`,
che continua a produrre il dict "legacy" usato oggi da main.py.

Verra' collegato al resto del sistema solo nei prossimi step, quando
Router e Business Logic saranno pronti a consumarlo. Fino ad allora
puo' essere importato e testato in autonomia senza alcun rischio per
il flusso esistente.
"""

from __future__ import annotations

from datetime import date, datetime, time
from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ============================================================
# ENUM
# ============================================================

class Intent(str, Enum):
    BOOK = "BOOK"
    RESCHEDULE = "RESCHEDULE"
    CANCEL = "CANCEL"
    CHECK_APPOINTMENT = "CHECK_APPOINTMENT"
    ASK_INFORMATION = "ASK_INFORMATION"

    PROVIDE_DATA = "PROVIDE_DATA"
    SELECT_SLOT = "SELECT_SLOT"
    CONFIRM = "CONFIRM"
    REJECT = "REJECT"
    CHANGE_PREFERENCE = "CHANGE_PREFERENCE"

    GREETING = "GREETING"
    THANKS = "THANKS"
    UNKNOWN = "UNKNOWN"


class ConversationStatus(str, Enum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    WAITING = "WAITING"
    EXPIRED = "EXPIRED"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"
    ESCALATED = "ESCALATED"


class OperationType(str, Enum):
    NONE = "NONE"
    CREATE = "CREATE"
    RESCHEDULE = "RESCHEDULE"
    CANCEL = "CANCEL"
    CHECK = "CHECK"
    MODIFY = "MODIFY"


class ConversationStep(str, Enum):
    IDLE = "IDLE"

    IDENTIFY_PROFESSIONAL = "IDENTIFY_PROFESSIONAL"
    IDENTIFY_APPOINTMENT = "IDENTIFY_APPOINTMENT"

    SEARCH_AVAILABILITY = "SEARCH_AVAILABILITY"
    WAITING_FOR_SLOT = "WAITING_FOR_SLOT"

    COLLECTING_CUSTOMER_DATA = "COLLECTING_CUSTOMER_DATA"

    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"

    EXECUTING = "EXECUTING"

    COMPLETED = "COMPLETED"


class PendingAction(str, Enum):
    NONE = "NONE"
    SELECT_PROFESSIONAL = "SELECT_PROFESSIONAL"
    SELECT_APPOINTMENT = "SELECT_APPOINTMENT"
    SELECT_SLOT = "SELECT_SLOT"

    PROVIDE_NAME = "PROVIDE_NAME"
    PROVIDE_PHONE = "PROVIDE_PHONE"
    PROVIDE_EMAIL = "PROVIDE_EMAIL"

    PROVIDE_DATE = "PROVIDE_DATE"
    PROVIDE_TIME = "PROVIDE_TIME"

    CONFIRM = "CONFIRM"


class BookingStatus(str, Enum):
    NOT_CREATED = "NOT_CREATED"
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    RESCHEDULED = "RESCHEDULED"
    FAILED = "FAILED"


class ResponseType(str, Enum):
    GREETING = "GREETING"
    ASK_CLARIFICATION = "ASK_CLARIFICATION"

    ASK_PROFESSIONAL = "ASK_PROFESSIONAL"
    ASK_APPOINTMENT = "ASK_APPOINTMENT"

    SHOW_AVAILABILITY = "SHOW_AVAILABILITY"
    ASK_SLOT = "ASK_SLOT"

    ASK_CUSTOMER_DATA = "ASK_CUSTOMER_DATA"
    ASK_CONFIRMATION = "ASK_CONFIRMATION"
    ASK_SEARCH_PREFERENCE = "ASK_SEARCH_PREFERENCE"
    SHOW_WEEK_OVERVIEW = "SHOW_WEEK_OVERVIEW"
    CONFIRM_APPOINTMENT_TARGET = "CONFIRM_APPOINTMENT_TARGET"

    INFORMATION = "INFORMATION"

    BOOKING_CONFIRMED = "BOOKING_CONFIRMED"
    RESCHEDULE_CONFIRMED = "RESCHEDULE_CONFIRMED"
    CANCELLATION_CONFIRMED = "CANCELLATION_CONFIRMED"

    APPOINTMENT_DETAILS = "APPOINTMENT_DETAILS"

    ERROR = "ERROR"
    GOODBYE = "GOODBYE"


# ============================================================
# VALORE CONTESTUALE (chi l'ha fornito, se confermato)
# ============================================================

class ContextValue(BaseModel, Generic[T]):
    value: T | None = None
    source: str | None = None
    confirmed: bool = False


# ============================================================
# ENTITA' DI DOMINIO
# ============================================================

class Customer(BaseModel):
    id: str | None = None

    full_name: ContextValue[str] = Field(default_factory=ContextValue)
    phone: ContextValue[str] = Field(default_factory=ContextValue)
    email: ContextValue[str] = Field(default_factory=ContextValue)


class Professional(BaseModel):
    id: str | None = None
    name: str | None = None
    location_id: str | None = None


class Appointment(BaseModel):
    id: str

    date: date
    time: time

    professional_id: str | None = None
    professional_name: str | None = None

    status: BookingStatus


class SearchCriteria(BaseModel):
    date_from: date | None = None
    date_to: date | None = None

    preferred_date: date | None = None

    time_from: time | None = None
    time_to: time | None = None

    preferred_time: time | None = None

    period: str | None = None
    week_part: str | None = None

    preferred_weekday: str | None = None
    time_preference: str | None = None

    excluded_dates: list[date] = Field(default_factory=list)
    excluded_slots: list[str] = Field(default_factory=list)


class AvailableSlot(BaseModel):
    id: str
    date: date
    time: time


class OfferedSlot(BaseModel):
    option: int
    slot: AvailableSlot


class Operation(BaseModel):
    type: OperationType = OperationType.NONE
    status: str | None = None
    target_appointment_id: str | None = None


class Confirmation(BaseModel):
    required: bool = False
    status: str | None = None
    confirmation_type: str | None = None
    payload: dict = Field(default_factory=dict)


class Booking(BaseModel):
    id: str | None = None
    status: BookingStatus = BookingStatus.NOT_CREATED
    slot_id: str | None = None


class TimeoutInfo(BaseModel):
    last_activity_at: datetime | None = None
    expires_at: datetime | None = None


class Conversation(BaseModel):
    id: str

    status: ConversationStatus = ConversationStatus.NEW

    current_intent: Intent = Intent.UNKNOWN
    current_operation: OperationType = OperationType.NONE
    current_step: ConversationStep = ConversationStep.IDLE

    pending_action: PendingAction = PendingAction.NONE

    timeout: TimeoutInfo = Field(default_factory=TimeoutInfo)


class Message(BaseModel):
    role: str
    text: str
    timestamp: datetime


class ConversationMemory(BaseModel):
    recent_messages: list[Message] = Field(default_factory=list)
    important_events: list[str] = Field(default_factory=list)


# ============================================================
# CONTEXT COMPLETO
# ============================================================

class ConversationContext(BaseModel):
    version: int = 1

    conversation: Conversation

    customer: Customer = Field(default_factory=Customer)

    service: ContextValue[str] = Field(default_factory=ContextValue)

    professional: Professional | None = None

    appointments: list[Appointment] = Field(default_factory=list)
    selected_appointment_id: str | None = None

    search: SearchCriteria = Field(default_factory=SearchCriteria)

    offered_slots: list[OfferedSlot] = Field(default_factory=list)

    operation: Operation = Field(default_factory=Operation)

    confirmation: Confirmation = Field(default_factory=Confirmation)

    booking: Booking = Field(default_factory=Booking)

    memory: ConversationMemory = Field(default_factory=ConversationMemory)

    confidence: float | None = None

    escalation_required: bool = False


# ============================================================
# CONTRATTI AI
# ============================================================

class AI1Result(BaseModel):
    """Output di AI#1 (Interpreter): interpreta, non decide."""

    intent: Intent

    entities: dict = Field(default_factory=dict)
    context_updates: dict = Field(default_factory=dict)

    confidence: float

    needs_clarification: bool = False
    clarification_reason: str | None = None


class SystemResult(BaseModel):
    """
    Schema unico e normalizzato che TUTTI i moduli di business logic
    (booking, reschedule, cancel, info) devono restituire.

    Questo e' cio' che rende AI#2 semplice: un solo schema da leggere,
    indipendentemente da quale dominio ha prodotto il risultato.
    """

    success: bool
    error_code: str | None = None
    data: dict = Field(default_factory=dict)


class AI2Result(BaseModel):
    """Output di AI#2 (Responder): comunica, non decide."""

    response_type: ResponseType
    message: str
    requires_user_response: bool = False
