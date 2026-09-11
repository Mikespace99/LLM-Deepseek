"""
Repository per onboarding e configurazione studio.
Tutte le operazioni usano il service_role key (bypass RLS).
"""

from __future__ import annotations
from datetime import datetime, timezone
from app.supabase_client import get_supabase


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# TENANT
# ---------------------------------------------------------------------------

def get_tenant_by_owner(owner_id: str) -> dict | None:
    if not owner_id:
        return None
    sb = get_supabase()
    res = (
        sb.table("tenants")
        .select("*")
        .eq("owner_id", owner_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def get_tenant_by_id(tenant_id: str) -> dict | None:
    if not tenant_id:
        return None
    sb = get_supabase()
    res = sb.table("tenants").select("*").eq("id", tenant_id).limit(1).execute()
    return res.data[0] if res.data else None


def create_tenant_for_owner(owner_id: str, email: str) -> dict:
    """Crea un tenant minimo collegato all'utente Auth."""
    sb = get_supabase()
    payload = {
        "owner_id": owner_id,
        "business_name": "Il mio studio",
        "assistant_name": "Assistente",
        "timezone": "Europe/Rome",
        "language": "it",
        "info": {"email": email},
        "onboarding_completed": False,
    }
    res = sb.table("tenants").insert(payload).execute()
    return res.data[0]


def update_tenant(tenant_id: str, data: dict) -> dict:
    sb = get_supabase()
    data = {**data, "updated_at": _now()}
    res = (
        sb.table("tenants")
        .update(data)
        .eq("id", tenant_id)
        .execute()
    )
    return res.data[0] if res.data else {}


# ---------------------------------------------------------------------------
# LOCATIONS
# ---------------------------------------------------------------------------

def get_locations(tenant_id: str) -> list[dict]:
    sb = get_supabase()
    res = (
        sb.table("locations")
        .select("*")
        .eq("tenant_id", tenant_id)
        .order("sort_order")
        .execute()
    )
    return res.data or []


def replace_locations(tenant_id: str, locations: list[dict]) -> list[dict]:
    """
    Sostituisce tutte le sedi del tenant.
    locations: [{id?, name, city, address, active, sort_order}]
    """
    sb = get_supabase()
    # Cancella le esistenti (cascade su working_hours se location_id è NOT NULL)
    # Per sicurezza: prima azzeriamo location_id nelle working_hours di questo tenant
    # poi cancelliamo le locations.
    existing = get_locations(tenant_id)
    existing_ids = {loc["id"] for loc in existing}

    incoming_ids = {loc["id"] for loc in locations if loc.get("id")}
    to_delete = existing_ids - incoming_ids

    if to_delete:
        # Rimuovi working_hours legate alle sedi da cancellare
        for lid in to_delete:
            sb.table("working_hours").delete().eq("location_id", lid).execute()
        sb.table("locations").delete().in_("id", list(to_delete)).execute()

    result = []
    for i, loc in enumerate(locations):
        payload = {
            "tenant_id": tenant_id,
            "name": loc.get("name") or "Sede",
            "city": loc.get("city") or "",
            "address": loc.get("address") or "",
            "active": loc.get("active", True),
            "sort_order": loc.get("sort_order", i),
            "updated_at": _now(),
        }
        if loc.get("id") and loc["id"] in existing_ids:
            res = (
                sb.table("locations")
                .update(payload)
                .eq("id", loc["id"])
                .execute()
            )
            result.append(res.data[0])
        else:
            res = sb.table("locations").insert(payload).execute()
            result.append(res.data[0])
    return result


# ---------------------------------------------------------------------------
# WORKING HOURS
# ---------------------------------------------------------------------------

def get_working_hours_full(tenant_id: str) -> list[dict]:
    sb = get_supabase()
    res = (
        sb.table("working_hours")
        .select("*, locations(name)")
        .eq("tenant_id", tenant_id)
        .order("day_of_week")
        .order("start_time")
        .execute()
    )
    return res.data or []


def replace_working_hours(tenant_id: str, slots: list[dict]) -> list[dict]:
    """
    slots: [{location_id, day_of_week, start_time, end_time, active}]
    Sostituisce tutte le fasce del tenant.
    """
    sb = get_supabase()
    sb.table("working_hours").delete().eq("tenant_id", tenant_id).execute()

    if not slots:
        return []

    rows = []
    for s in slots:
        if not s.get("location_id") or not s.get("start_time") or not s.get("end_time"):
            continue
        rows.append({
            "tenant_id": tenant_id,
            "location_id": s["location_id"],
            "day_of_week": int(s["day_of_week"]),
            "start_time": s["start_time"],
            "end_time": s["end_time"],
            "active": s.get("active", True),
        })

    if not rows:
        return []

    res = sb.table("working_hours").insert(rows).execute()
    return res.data or []


# ---------------------------------------------------------------------------
# SERVICES
# ---------------------------------------------------------------------------

def get_services_full(tenant_id: str) -> list[dict]:
    sb = get_supabase()
    res = (
        sb.table("services")
        .select("*")
        .eq("tenant_id", tenant_id)
        .order("sort_order")
        .execute()
    )
    return res.data or []


def replace_services(tenant_id: str, services: list[dict]) -> list[dict]:
    sb = get_supabase()
    existing = get_services_full(tenant_id)
    existing_ids = {s["id"] for s in existing}
    incoming_ids = {s["id"] for s in services if s.get("id")}
    to_delete = existing_ids - incoming_ids

    if to_delete:
        sb.table("services").delete().in_("id", list(to_delete)).execute()

    result = []
    for i, svc in enumerate(services):
        payload = {
            "tenant_id": tenant_id,
            "name": svc.get("name") or "Servizio",
            "description": svc.get("description") or "",
            "duration_minutes": int(svc.get("duration_minutes") or svc.get("duration") or 30),
            "buffer_before": int(svc.get("buffer_before") or 0),
            "buffer_after": int(svc.get("buffer_after") or 5),
            "price": svc.get("price"),
            "active": svc.get("active", True),
            "sort_order": svc.get("sort_order", i),
            "updated_at": _now(),
        }
        if svc.get("id") and svc["id"] in existing_ids:
            res = sb.table("services").update(payload).eq("id", svc["id"]).execute()
            result.append(res.data[0])
        else:
            res = sb.table("services").insert(payload).execute()
            result.append(res.data[0])
    return result


# ---------------------------------------------------------------------------
# EXCEPTIONS
# ---------------------------------------------------------------------------

def get_exceptions(tenant_id: str) -> list[dict]:
    sb = get_supabase()
    res = (
        sb.table("calendar_exceptions")
        .select("*")
        .eq("tenant_id", tenant_id)
        .order("date")
        .execute()
    )
    return res.data or []


def replace_exceptions(tenant_id: str, exceptions: list[dict]) -> list[dict]:
    sb = get_supabase()
    sb.table("calendar_exceptions").delete().eq("tenant_id", tenant_id).execute()

    rows = []
    for ex in exceptions:
        if not ex.get("date"):
            continue
        rows.append({
            "tenant_id": tenant_id,
            "date": ex["date"],
            "type": ex.get("type") or "closed",
            "start_time": ex.get("start_time") or ex.get("start") or None,
            "end_time": ex.get("end_time") or ex.get("end") or None,
            "reason": ex.get("reason") or "",
            "active": True,
        })
    if not rows:
        return []
    res = sb.table("calendar_exceptions").insert(rows).execute()
    return res.data or []


# ---------------------------------------------------------------------------
# HOLIDAYS
# ---------------------------------------------------------------------------

def get_holidays(country: str = "IT") -> list[dict]:
    sb = get_supabase()
    res = (
        sb.table("holidays")
        .select("*")
        .eq("country", country)
        .order("date")
        .execute()
    )
    return res.data or []


def get_tenant_holidays(tenant_id: str) -> list[dict]:
    sb = get_supabase()
    res = (
        sb.table("tenant_holidays")
        .select("*, holidays(*)")
        .eq("tenant_id", tenant_id)
        .execute()
    )
    return res.data or []


def set_tenant_holidays(tenant_id: str, holiday_states: list[dict]) -> None:
    """
    holiday_states: [{holiday_id, enabled}]
    """
    sb = get_supabase()
    sb.table("tenant_holidays").delete().eq("tenant_id", tenant_id).execute()
    rows = [
        {
            "tenant_id": tenant_id,
            "holiday_id": h["holiday_id"],
            "enabled": h.get("enabled", True),
        }
        for h in holiday_states
        if h.get("holiday_id")
    ]
    if rows:
        sb.table("tenant_holidays").insert(rows).execute()


# ---------------------------------------------------------------------------
# FULL CONFIG (per la pagina di onboarding)
# ---------------------------------------------------------------------------

def get_full_config(tenant_id: str) -> dict:
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        return {}
    return {
        "tenant": tenant,
        "locations": get_locations(tenant_id),
        "working_hours": get_working_hours_full(tenant_id),
        "services": get_services_full(tenant_id),
        "exceptions": get_exceptions(tenant_id),
        "holidays": get_holidays("IT"),
        "tenant_holidays": get_tenant_holidays(tenant_id),
    }
