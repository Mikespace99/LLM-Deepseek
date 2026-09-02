"""
Repository appuntamenti.

Il backend Python è l'UNICO componente che legge/scrive su Supabase.
Da quando n8n/Google Calendar sono stati rimossi, questa tabella è la
sola fonte di verità sia per il motore di prenotazione WhatsApp
(app/booking/engine.py) sia per l'Agenda della dashboard
(app/web/routes.py, sezione /api/agenda).

Convenzioni:
- status: 'confirmed' | 'cancelled' | 'completed' | 'no_show'
  Solo le righe 'confirmed' contano come "occupato" per il calcolo
  della disponibilità (vedi vincolo DB appointments_no_overlap).
- source: 'whatsapp' | 'manual' | 'block'
  'block' = riga senza cliente/servizio, usata dallo staff per
  bloccare un intervallo orario dall'Agenda (es. pausa straordinaria).
"""

from __future__ import annotations

from app.supabase_client import get_supabase


_SELECT_FULL = (
    "id, tenant_id, customer_id, phone_number, service, service_id, "
    "location_id, appointment_date, appointment_time, duration_minutes, "
    "status, source, notes, google_event_id, created_at, updated_at, "
    "customers(id, full_name, phone_number), "
    "services(id, name), locations(id, name)"
)


# ============================================================
# LETTURA
# ============================================================

def list_for_period(
    tenant_id: str, date_from: str, date_to: str, include_cancelled: bool = True
) -> list[dict]:
    """
    Righe nel periodo con join a services(name, price), per il calcolo di
    fatturato stimato e distribuzioni (dashboard e statistiche). A
    differenza di list_busy_for_availability, qui includiamo per default
    anche i cancellati: alle statistiche interessa anche il tasso di
    cancellazione.
    """
    sb = get_supabase()
    q = (
        sb.table("appointments")
        .select(
            "id, appointment_date, appointment_time, duration_minutes, "
            "status, source, service_id, service, "
            "services(name, price)"
        )
        .eq("tenant_id", tenant_id)
        .gte("appointment_date", date_from)
        .lte("appointment_date", date_to)
    )
    if not include_cancelled:
        q = q.neq("status", "cancelled")
    return q.execute().data or []


def get_appointment(tenant_id: str, appointment_id: str) -> dict | None:
    sb = get_supabase()
    res = (
        sb.table("appointments")
        .select(_SELECT_FULL)
        .eq("tenant_id", tenant_id)
        .eq("id", appointment_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def list_in_range(
    tenant_id: str,
    date_from: str,
    date_to: str,
    location_id: str | None = None,
    include_cancelled: bool = False,
) -> list[dict]:
    """
    Appuntamenti/blocchi nel range [date_from, date_to] (date incluse),
    per il rendering dell'Agenda. Include tutti gli status a meno che
    include_cancelled=False (default: nasconde i cancellati).
    """
    sb = get_supabase()
    q = (
        sb.table("appointments")
        .select(_SELECT_FULL)
        .eq("tenant_id", tenant_id)
        .gte("appointment_date", date_from)
        .lte("appointment_date", date_to)
        .order("appointment_date")
        .order("appointment_time")
    )
    if location_id:
        q = q.eq("location_id", location_id)
    if not include_cancelled:
        q = q.neq("status", "cancelled")
    return q.execute().data or []


def list_busy_for_availability(tenant_id: str, date_from: str, date_to: str) -> list[dict]:
    """
    Righe 'confirmed' nel range, usate dal motore di disponibilità per
    calcolare gli slot liberi. Volutamente NON filtrate per sede: il
    calendario è unico per tenant (vedi commento nella migration 003).
    """
    sb = get_supabase()
    res = (
        sb.table("appointments")
        .select("id, appointment_date, appointment_time, duration_minutes, location_id")
        .eq("tenant_id", tenant_id)
        .eq("status", "confirmed")
        .gte("appointment_date", date_from)
        .lte("appointment_date", date_to)
        .execute()
    )
    return res.data or []


# ============================================================
# SCRITTURA
# ============================================================

def create_appointment(
    tenant_id: str,
    appointment_date: str,
    appointment_time: str,
    duration_minutes: int,
    source: str,
    customer_id: str | None = None,
    phone_number: str | None = None,
    service: str | None = None,
    service_id: str | None = None,
    location_id: str | None = None,
    notes: str | None = None,
    status: str = "confirmed",
    google_event_id: str | None = None,
    created_by: str | None = None,
) -> dict:
    """
    Crea una riga appointments. Il vincolo DB appointments_no_overlap
    fa da rete di sicurezza contro le race condition: se due richieste
    concorrenti (es. WhatsApp + dashboard) puntano allo stesso slot,
    Postgres rifiuta la seconda insert con un errore di violazione
    exclusion constraint (codice 23P01), che il chiamante deve gestire
    come "slot_conflict".
    """
    if source not in ("whatsapp", "manual", "block"):
        raise ValueError(f"source non valido: {source}")

    sb = get_supabase()
    payload = {
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "phone_number": phone_number,
        "service": service,
        "service_id": service_id,
        "location_id": location_id,
        "appointment_date": appointment_date,
        "appointment_time": appointment_time,
        "duration_minutes": duration_minutes,
        "status": status,
        "source": source,
        "notes": notes,
        "google_event_id": google_event_id,
        "created_by": created_by,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    res = sb.table("appointments").insert(payload).execute()
    return res.data[0]


def update_appointment(
    tenant_id: str,
    appointment_id: str,
    **fields,
) -> dict | None:
    """
    Aggiornamento parziale (usato per drag&drop, cambio stato, note...).
    Passa solo i campi da modificare, es:
      update_appointment(tid, aid, appointment_date="2026-08-25",
                          appointment_time="10:00")
    """
    if not fields:
        return get_appointment(tenant_id, appointment_id)

    allowed = {
        "appointment_date", "appointment_time", "duration_minutes",
        "status", "notes", "location_id", "service_id", "service",
        "customer_id", "phone_number",
    }
    payload = {k: v for k, v in fields.items() if k in allowed}

    sb = get_supabase()
    res = (
        sb.table("appointments")
        .update(payload)
        .eq("tenant_id", tenant_id)
        .eq("id", appointment_id)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def cancel_appointment(tenant_id: str, appointment_id: str) -> dict | None:
    """Soft-delete: resta a DB per le statistiche, ma libera lo slot."""
    return update_appointment(tenant_id, appointment_id, status="cancelled")


def is_overlap_error(exc: Exception) -> bool:
    """
    Riconosce l'errore Postgres di violazione dell'exclusion constraint
    appointments_no_overlap (codice 23P01), per farlo risalire come 409
    "slot_conflict" invece che come 500 generico.
    """
    msg = str(exc)
    return "23P01" in msg or "appointments_no_overlap" in msg
