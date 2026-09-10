"""
Route web: registrazione, login, onboarding, API di salvataggio.
"""

from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, Field
from typing import Optional

from app.web.auth import (
    register_user,
    login_user,
    get_current_user,
    require_user,
    logout,
    _set_auth_cookies,
    _clear_auth_cookies,
)
from app.repositories.onboarding import (
    get_tenant_by_owner,
    get_full_config,
    update_tenant,
    replace_locations,
    replace_working_hours,
    replace_services,
    replace_exceptions,
    set_tenant_holidays,
    get_holidays,
)
from app.repositories import appointment as appointment_repo
from app.repositories.customer import (
    search_customers,
    get_or_create_customer,
    update_customer_name,
    normalize_phone,
    list_customers,
    get_customer,
    create_customer_manual,
    update_customer,
    count_new_customers,
)
from app.repositories.tenant import get_tenant_knowledge
from app.booking.engine import search_availability

router = APIRouter(tags=["web"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Workaround Python 3.14 / Jinja2 cache key bug
templates.env.cache = None


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class AuthBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)


class TenantData(BaseModel):
    business_name: str
    specialty: Optional[str] = None
    assistant_name: Optional[str] = "Assistente"
    timezone: Optional[str] = "Europe/Rome"
    language: Optional[str] = "it"
    min_lead_hours: Optional[int] = 2
    max_appointments_per_day: Optional[int] = 12
    slot_search_days: Optional[int] = 30
    phone: Optional[str] = None          # senza +39
    whatsapp_number: Optional[str] = None  # senza +39
    email: Optional[str] = None
    phone_number_id: Optional[str] = None
    access_token: Optional[str] = None
    google_calendar_id: Optional[str] = None
    info: Optional[dict] = None


class LocationIn(BaseModel):
    id: Optional[str] = None
    name: str
    city: Optional[str] = ""
    address: Optional[str] = ""
    active: bool = True
    sort_order: int = 0


class SlotIn(BaseModel):
    location_id: str
    day_of_week: int
    start_time: str
    end_time: str
    active: bool = True


class ServiceIn(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = ""
    duration_minutes: int = 30
    buffer_before: int = 0
    buffer_after: int = 5
    price: Optional[float] = None
    active: bool = True
    sort_order: int = 0


class ExceptionIn(BaseModel):
    date: str
    type: str = "closed"
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    reason: Optional[str] = ""


class HolidayState(BaseModel):
    holiday_id: str
    enabled: bool = True


class AppointmentCreateIn(BaseModel):
    type: str = Field(default="booking")  # "booking" | "block"
    location_id: Optional[str] = None
    service_id: Optional[str] = None
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    date: str
    time: str
    duration_minutes: Optional[int] = None
    notes: Optional[str] = None


class AppointmentUpdateIn(BaseModel):
    date: Optional[str] = None
    time: Optional[str] = None
    duration_minutes: Optional[int] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    location_id: Optional[str] = None


class FullConfigIn(BaseModel):
    tenant: TenantData
    locations: list[LocationIn] = []
    working_hours: list[SlotIn] = []
    services: list[ServiceIn] = []
    exceptions: list[ExceptionIn] = []
    holidays: list[HolidayState] = []
    mark_completed: bool = False


# ---------------------------------------------------------------------------
# Pagine HTML
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = get_current_user(request)
    if user:
        tenant = get_tenant_by_owner(user["id"])
        if tenant and tenant.get("onboarding_completed"):
            return RedirectResponse("/dashboard", status_code=302)
        return RedirectResponse("/onboarding", status_code=302)
    return RedirectResponse("/login", status_code=302)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = get_current_user(request)
    if user:
        tenant = get_tenant_by_owner(user["id"])
        if tenant and tenant.get("onboarding_completed"):
            return RedirectResponse("/dashboard", status_code=302)
        return RedirectResponse("/onboarding", status_code=302)
    return templates.TemplateResponse(
        request=request, name="login.html", context={"error": None}
    )


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    user = get_current_user(request)
    if user:
        tenant = get_tenant_by_owner(user["id"])
        if tenant and tenant.get("onboarding_completed"):
            return RedirectResponse("/dashboard", status_code=302)
        return RedirectResponse("/onboarding", status_code=302)
    return templates.TemplateResponse(
        request=request, name="register.html", context={"error": None}
    )


@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    tenant = get_tenant_by_owner(user["id"])
    return templates.TemplateResponse(
        request=request, name="onboarding.html", context={"active_page": "impostazioni", "tenant": tenant}
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant or not tenant.get("onboarding_completed"):
        return RedirectResponse("/onboarding", status_code=302)
    return templates.TemplateResponse(
        request=request, name="dashboard.html", context={"tenant": tenant, "active_page": "dashboard"}
    )


@router.get("/clienti", response_class=HTMLResponse)
async def clienti_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant or not tenant.get("onboarding_completed"):
        return RedirectResponse("/onboarding", status_code=302)
    return templates.TemplateResponse(
        request=request, name="clienti.html", context={"tenant": tenant, "active_page": "clienti"}
    )


@router.get("/statistiche", response_class=HTMLResponse)
async def statistiche_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant or not tenant.get("onboarding_completed"):
        return RedirectResponse("/onboarding", status_code=302)
    return templates.TemplateResponse(
        request=request, name="statistiche.html", context={"tenant": tenant, "active_page": "statistiche"}
    )


@router.get("/logout")
async def logout_route(response: Response):
    logout(response)
    return RedirectResponse("/login", status_code=302)


# ---------------------------------------------------------------------------
# API Auth
# ---------------------------------------------------------------------------

@router.post("/api/auth/register")
async def api_register(body: AuthBody, response: Response):
    result = register_user(body.email, body.password)
    session = result.get("session") or {}
    if session.get("access_token") and session.get("refresh_token"):
        _set_auth_cookies(response, session["access_token"], session["refresh_token"])
    return {
        "ok": True,
        "user": result["user"],
        "tenant_id": result["tenant"]["id"],
        "onboarding_completed": result["tenant"].get("onboarding_completed", False),
    }


@router.post("/api/auth/login")
async def api_login(body: AuthBody, response: Response):
    result = login_user(body.email, body.password)
    session = result.get("session") or {}
    if session.get("access_token") and session.get("refresh_token"):
        _set_auth_cookies(response, session["access_token"], session["refresh_token"])
    return {
        "ok": True,
        "user": result["user"],
        "tenant_id": result["tenant"]["id"],
        "onboarding_completed": result["tenant"].get("onboarding_completed", False),
    }


@router.post("/api/auth/logout")
async def api_logout(response: Response):
    logout(response)
    return {"ok": True}


@router.get("/api/auth/me")
async def api_me(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Non autenticato")
    tenant = get_tenant_by_owner(user["id"])
    return {
        "user": user,
        "tenant": {
            "id": tenant["id"] if tenant else None,
            "business_name": tenant.get("business_name") if tenant else None,
            "onboarding_completed": tenant.get("onboarding_completed", False) if tenant else False,
        },
    }


# ---------------------------------------------------------------------------
# API Config (onboarding)
# ---------------------------------------------------------------------------

@router.get("/api/config")
async def api_get_config(request: Request):
    user = require_user(request)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Studio non trovato")
    config = get_full_config(tenant["id"])
    return config


@router.put("/api/config")
async def api_save_config(body: FullConfigIn, request: Request):
    user = require_user(request)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Studio non trovato")

    tenant_id = tenant["id"]
    t = body.tenant

    # Costruisci info jsonb
    info = dict(tenant.get("info") or {})
    if t.phone is not None:
        # salviamo solo le cifre; il +39 lo aggiungiamo in lettura se serve
        info["phone"] = t.phone.strip().replace(" ", "").replace("+39", "")
    if t.whatsapp_number is not None:
        wa = t.whatsapp_number.strip().replace(" ", "").replace("+39", "")
        info["whatsapp_number"] = wa
    if t.email is not None:
        info["email"] = t.email
    if t.phone_number_id is not None:
        info["phone_number_id"] = (t.phone_number_id or "").strip()
    if t.access_token is not None:
        # non sovrascrivere con stringa vuota se già presente (evita cancellazioni accidentali)
        tok = (t.access_token or "").strip()
        if tok:
            info["access_token"] = tok
    if t.google_calendar_id is not None:
        gcal = (t.google_calendar_id or "").strip()
        if gcal:
            info["google_calendar_id"] = gcal

    update_payload = {
        "business_name": t.business_name,
        "specialty": t.specialty,
        "assistant_name": t.assistant_name or "Assistente",
        "timezone": t.timezone or "Europe/Rome",
        "language": t.language or "it",
        "min_lead_hours": t.min_lead_hours if t.min_lead_hours is not None else 2,
        "max_appointments_per_day": t.max_appointments_per_day if t.max_appointments_per_day is not None else 12,
        "slot_search_days": t.slot_search_days if t.slot_search_days is not None else 30,
        "info": info,
    }
    if t.google_calendar_id is not None and (t.google_calendar_id or "").strip():
        update_payload["google_calendar_id"] = t.google_calendar_id.strip()
    if body.mark_completed:
        update_payload["onboarding_completed"] = True

    update_tenant(tenant_id, update_payload)

    # Locations
    locs = replace_locations(tenant_id, [loc.model_dump() for loc in body.locations])

    # Mappa id temporanei → id reali (se il frontend ha inviato id fake)
    # Per semplicità assumiamo che il frontend invii gli id reali dopo il primo salvataggio.
    # Working hours
    replace_working_hours(tenant_id, [s.model_dump() for s in body.working_hours])

    # Services
    replace_services(tenant_id, [s.model_dump() for s in body.services])

    # Exceptions
    replace_exceptions(tenant_id, [e.model_dump() for e in body.exceptions])

    # Holidays
    if body.holidays:
        set_tenant_holidays(tenant_id, [h.model_dump() for h in body.holidays])

    return {"ok": True, "tenant_id": tenant_id}


@router.get("/api/holidays")
async def api_holidays():
    return get_holidays("IT")


# ---------------------------------------------------------------------------
# Agenda – pagina
# ---------------------------------------------------------------------------

@router.get("/agenda", response_class=HTMLResponse)
async def agenda_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant or not tenant.get("onboarding_completed"):
        return RedirectResponse("/onboarding", status_code=302)
    return templates.TemplateResponse(
        request=request, name="agenda.html", context={"tenant": tenant, "active_page": "agenda"}
    )


# ---------------------------------------------------------------------------
# Agenda – helper interni
# ---------------------------------------------------------------------------

def _service_block_minutes(tenant_id: str, service_id: str | None) -> int:
    """Durata totale (servizio + buffer) da riservare per una prenotazione manuale."""
    if not service_id:
        return 30
    knowledge = get_tenant_knowledge(tenant_id)
    for s in knowledge.get("services") or []:
        if s.get("id") == service_id:
            return (s.get("duration_minutes") or 30) + (s.get("buffer_before") or 0) + (s.get("buffer_after") or 5)
    return 30


def _appointment_to_event(row: dict, tz_name: str) -> dict:
    tz = ZoneInfo(tz_name or "Europe/Rome")
    d = str(row.get("appointment_date"))[:10]
    t = str(row.get("appointment_time"))[:5]
    y, mo, da = (int(p) for p in d.split("-"))
    h, mi = (int(p) for p in t.split(":"))
    start = datetime(y, mo, da, h, mi, tzinfo=tz)
    end = start + timedelta(minutes=row.get("duration_minutes") or 30)

    customer = row.get("customers") or {}
    service = row.get("services") or {}
    location = row.get("locations") or {}
    source = row.get("source") or "whatsapp"

    if source == "block":
        title = row.get("notes") or "Bloccato"
    else:
        cust_name = customer.get("full_name") or row.get("phone_number") or "Cliente"
        svc_name = service.get("name") or row.get("service") or ""
        title = f"{cust_name} – {svc_name}" if svc_name else cust_name

    return {
        "id": row["id"],
        "title": title,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "extendedProps": {
            "type": "block" if source == "block" else "booking",
            "source": source,
            "status": row.get("status"),
            "service_id": row.get("service_id"),
            "service_name": service.get("name") or row.get("service"),
            "customer_id": row.get("customer_id"),
            "customer_name": customer.get("full_name"),
            "customer_phone": row.get("phone_number") or customer.get("phone_number"),
            "location_id": row.get("location_id"),
            "location_name": location.get("name"),
            "notes": row.get("notes"),
        },
    }


# ---------------------------------------------------------------------------
# Agenda – API
# ---------------------------------------------------------------------------

@router.get("/api/agenda/events")
async def api_agenda_events(request: Request, date_from: str, date_to: str, location_id: Optional[str] = None):
    user = require_user(request)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Studio non trovato")
    rows = appointment_repo.list_in_range(
        tenant["id"], date_from, date_to, location_id=location_id, include_cancelled=False
    )
    tz_name = tenant.get("timezone") or "Europe/Rome"
    return [_appointment_to_event(r, tz_name) for r in rows]


@router.get("/api/agenda/availability")
async def api_agenda_availability(
    request: Request,
    date: str,
    service_id: Optional[str] = None,
    location_id: Optional[str] = None,
):
    """
    Riusa lo stesso motore del bot WhatsApp per proporre slot liberi
    coerenti (rispetta buffer, orari, eccezioni, festività, impegni già
    presenti) quando lo staff crea un appuntamento a mano dall'Agenda.
    """
    user = require_user(request)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Studio non trovato")

    knowledge = get_tenant_knowledge(tenant["id"])
    service_name = None
    if service_id:
        for s in knowledge.get("services") or []:
            if s.get("id") == service_id:
                service_name = s.get("name")
                break

    collected_data = {
        "service": service_name,
        "location_id": location_id,
        "preferences": {"date": date},
    }
    booking = search_availability(tenant, knowledge, collected_data)
    return booking


@router.get("/api/agenda/appointments/{appointment_id}")
async def api_agenda_get_appointment(appointment_id: str, request: Request):
    user = require_user(request)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Studio non trovato")
    row = appointment_repo.get_appointment(tenant["id"], appointment_id)
    if not row:
        raise HTTPException(status_code=404, detail="Appuntamento non trovato")
    return _appointment_to_event(row, tenant.get("timezone") or "Europe/Rome")


@router.post("/api/agenda/appointments")
async def api_agenda_create_appointment(body: AppointmentCreateIn, request: Request):
    user = require_user(request)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Studio non trovato")
    tenant_id = tenant["id"]

    is_block = body.type == "block"

    customer_id = body.customer_id
    phone_number = None
    service_name = None

    if not is_block:
        if not customer_id and body.customer_phone:
            customer = get_or_create_customer(tenant_id, body.customer_phone)
            customer_id = customer["id"]
            if body.customer_name:
                update_customer_name(tenant_id, customer_id, body.customer_name)
        if customer_id:
            phone_number = normalize_phone(body.customer_phone) if body.customer_phone else None

        if body.service_id:
            knowledge = get_tenant_knowledge(tenant_id)
            for s in knowledge.get("services") or []:
                if s.get("id") == body.service_id:
                    service_name = s.get("name")
                    break

    duration = body.duration_minutes or (
        30 if is_block else _service_block_minutes(tenant_id, body.service_id)
    )

    try:
        row = appointment_repo.create_appointment(
            tenant_id=tenant_id,
            appointment_date=body.date,
            appointment_time=body.time,
            duration_minutes=duration,
            source="block" if is_block else "manual",
            customer_id=None if is_block else customer_id,
            phone_number=None if is_block else phone_number,
            service=None if is_block else service_name,
            service_id=None if is_block else body.service_id,
            location_id=body.location_id,
            notes=body.notes,
            status="confirmed",
            created_by=user["id"],
        )
    except Exception as e:
        if appointment_repo.is_overlap_error(e):
            raise HTTPException(status_code=409, detail="slot_conflict")
        raise HTTPException(status_code=500, detail=str(e))

    full = appointment_repo.get_appointment(tenant_id, row["id"])
    return _appointment_to_event(full, tenant.get("timezone") or "Europe/Rome")


@router.patch("/api/agenda/appointments/{appointment_id}")
async def api_agenda_update_appointment(appointment_id: str, body: AppointmentUpdateIn, request: Request):
    user = require_user(request)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Studio non trovato")

    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="Nessun campo da aggiornare")

    try:
        updated = appointment_repo.update_appointment(tenant["id"], appointment_id, **fields)
    except Exception as e:
        if appointment_repo.is_overlap_error(e):
            raise HTTPException(status_code=409, detail="slot_conflict")
        raise HTTPException(status_code=500, detail=str(e))

    if not updated:
        raise HTTPException(status_code=404, detail="Appuntamento non trovato")

    full = appointment_repo.get_appointment(tenant["id"], appointment_id)
    return _appointment_to_event(full, tenant.get("timezone") or "Europe/Rome")


@router.delete("/api/agenda/appointments/{appointment_id}")
async def api_agenda_delete_appointment(appointment_id: str, request: Request):
    user = require_user(request)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Studio non trovato")
    updated = appointment_repo.cancel_appointment(tenant["id"], appointment_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Appuntamento non trovato")
    return {"ok": True}


@router.get("/api/agenda/customers/search")
async def api_agenda_customers_search(request: Request, q: str = ""):
    user = require_user(request)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Studio non trovato")
    return search_customers(tenant["id"], q)


# ---------------------------------------------------------------------------
# Modelli – Clienti
# ---------------------------------------------------------------------------

class CustomerIn(BaseModel):
    full_name: str
    phone_number: str
    email: Optional[str] = None


class CustomerUpdateIn(BaseModel):
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    email: Optional[str] = None


# ---------------------------------------------------------------------------
# API – Dashboard
# ---------------------------------------------------------------------------

def _appointment_status_label(status: str) -> str:
    return {
        "confirmed": "Confermato",
        "completed": "Completato",
        "cancelled": "Cancellato",
        "no_show": "No-show",
    }.get(status, status)


@router.get("/api/dashboard/summary")
async def api_dashboard_summary(request: Request, month: Optional[str] = None):
    """
    Riepilogo per la home: appuntamenti di oggi (con dettaglio per la
    tabella), nuovi clienti nel mese corrente, fatturato stimato del mese
    (somma dei prezzi dei servizi confermati/completati) e le date del
    mese richiesto ("YYYY-MM") che hanno almeno un appuntamento, per i
    pallini del mini-calendario.
    """
    user = require_user(request)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Studio non trovato")
    tenant_id = tenant["id"]
    tz = ZoneInfo(tenant.get("timezone") or "Europe/Rome")
    today = datetime.now(tz).date()

    # Appuntamenti di oggi (tutti gli status, per il riepilogo in tabella)
    today_rows = appointment_repo.list_in_range(
        tenant_id, today.isoformat(), today.isoformat(), include_cancelled=True
    )
    today_appointments = []
    for r in sorted(today_rows, key=lambda x: str(x.get("appointment_time"))):
        customer = r.get("customers") or {}
        service = r.get("services") or {}
        t = str(r.get("appointment_time"))[:5]
        dur = r.get("duration_minutes") or 30
        h, mi = (int(p) for p in t.split(":"))
        end_h, end_mi = divmod(h * 60 + mi + dur, 60)
        today_appointments.append({
            "id": r["id"],
            "time_range": f"{t} – {end_h % 24:02d}:{end_mi:02d}",
            "customer_name": customer.get("full_name"),
            "customer_phone": r.get("phone_number") or customer.get("phone_number"),
            "service_name": service.get("name") or r.get("service"),
            "status": r.get("status"),
        })

    # Mese di riferimento per fatturato / nuovi clienti / puntini calendario
    if month:
        y, m = (int(p) for p in month.split("-"))
    else:
        y, m = today.year, today.month
    month_start = f"{y:04d}-{m:02d}-01"
    next_m_y, next_m_m = (y + 1, 1) if m == 12 else (y, m + 1)
    month_end = f"{next_m_y:04d}-{next_m_m:02d}-01"

    month_rows = appointment_repo.list_for_period(tenant_id, month_start, month_end, include_cancelled=True)
    revenue_month = sum(
        (r.get("services") or {}).get("price") or 0
        for r in month_rows
        if r.get("status") in ("confirmed", "completed")
    )
    appointment_dates = sorted({
        str(r["appointment_date"])[:10] for r in month_rows if r.get("status") != "cancelled"
    })

    new_clients_month = count_new_customers(tenant_id, month_start)

    return {
        "appointments_today": len(today_appointments),
        "appointments_today_delta": None,  # richiederebbe uno storico che non teniamo ancora
        "new_clients_month": new_clients_month,
        "revenue_month": round(revenue_month, 2),
        "today_appointments": today_appointments,
        "appointment_dates": appointment_dates,
    }


# ---------------------------------------------------------------------------
# API – Clienti
# ---------------------------------------------------------------------------

@router.get("/api/clients")
async def api_clients_list(request: Request, q: str = "", limit: int = 20, offset: int = 0):
    user = require_user(request)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Studio non trovato")
    limit = max(1, min(limit, 100))
    return list_customers(tenant["id"], search=q, limit=limit, offset=max(0, offset))


@router.post("/api/clients")
async def api_clients_create(body: CustomerIn, request: Request):
    user = require_user(request)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Studio non trovato")
    return create_customer_manual(tenant["id"], body.full_name, body.phone_number, body.email)


@router.get("/api/clients/{customer_id}")
async def api_clients_get(customer_id: str, request: Request):
    user = require_user(request)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Studio non trovato")
    customer = get_customer(tenant["id"], customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Cliente non trovato")
    return customer


@router.patch("/api/clients/{customer_id}")
async def api_clients_update(customer_id: str, body: CustomerUpdateIn, request: Request):
    user = require_user(request)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Studio non trovato")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    updated = update_customer(tenant["id"], customer_id, **fields)
    if not updated:
        raise HTTPException(status_code=404, detail="Cliente non trovato")
    return updated


# ---------------------------------------------------------------------------
# API – Statistiche
# ---------------------------------------------------------------------------

@router.get("/api/stats/summary")
async def api_stats_summary(request: Request, date_from: str, date_to: str):
    user = require_user(request)
    tenant = get_tenant_by_owner(user["id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Studio non trovato")

    rows = appointment_repo.list_for_period(tenant["id"], date_from, date_to, include_cancelled=True)

    total = len(rows)
    cancelled = sum(1 for r in rows if r.get("status") == "cancelled")
    completed = sum(1 for r in rows if r.get("status") == "completed")
    no_show = sum(1 for r in rows if r.get("status") == "no_show")
    confirmed = sum(1 for r in rows if r.get("status") == "confirmed")

    by_day: dict[str, int] = {}
    by_service: dict[str, int] = {}
    by_hour: dict[str, int] = {}
    revenue = 0.0

    for r in rows:
        if r.get("status") == "cancelled":
            continue
        d = str(r["appointment_date"])[:10]
        by_day[d] = by_day.get(d, 0) + 1

        service = r.get("services") or {}
        name = service.get("name") or r.get("service") or "Altro"
        by_service[name] = by_service.get(name, 0) + 1

        h = str(r.get("appointment_time"))[:2]
        by_hour[h] = by_hour.get(h, 0) + 1

        if r.get("status") in ("confirmed", "completed"):
            revenue += service.get("price") or 0

    return {
        "total": total,
        "cancelled": cancelled,
        "completed": completed,
        "no_show": no_show,
        "confirmed": confirmed,
        "cancellation_rate": round(cancelled / total * 100, 1) if total else 0,
        "revenue": round(revenue, 2),
        "by_day": sorted(by_day.items()),
        "by_service": sorted(by_service.items(), key=lambda x: -x[1]),
        "by_hour": sorted(by_hour.items()),
    }
