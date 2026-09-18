"""
Repository tenant.
- Ricerca per numero WhatsApp filtrata a database (JSON)
- Knowledge: locations, working_hours, services, exceptions, holidays
"""

from __future__ import annotations

from app.supabase_client import get_supabase
from app.repositories.customer import normalize_phone


DAY_NAMES = {
    1: "Lunedì",
    2: "Martedì",
    3: "Mercoledì",
    4: "Giovedì",
    5: "Venerdì",
    6: "Sabato",
    7: "Domenica",
}


def get_tenant_by_whatsapp_number(phone_number: str) -> dict | None:
    """
    Trova il tenant dal numero WhatsApp business.
    Filtra a database su info->>'whatsapp_number' (normalizzato).
    Accetta sia cifre pure sia con +39.
    """
    phone = normalize_phone(phone_number)
    if not phone:
        return None

    sb = get_supabase()

    # Varianti possibili salvate in onboarding
    candidates = [
        phone,
        f"+{phone}",
        f"+39{phone}" if not phone.startswith("39") else f"+{phone}",
        phone[2:] if phone.startswith("39") and len(phone) > 10 else None,
    ]
    candidates = [c for c in candidates if c]

    for candidate in candidates:
        result = (
            sb.table("tenants")
            .select("*")
            .eq("info->>whatsapp_number", candidate)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]

    return None


def get_tenant(tenant_id: str) -> dict | None:
    if not tenant_id:
        return None
    sb = get_supabase()
    result = (
        sb.table("tenants")
        .select("*")
        .eq("id", tenant_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def get_locations(tenant_id: str, active_only: bool = True) -> list[dict]:
    if not tenant_id:
        return []
    sb = get_supabase()
    q = (
        sb.table("locations")
        .select("*")
        .eq("tenant_id", tenant_id)
        .order("sort_order")
    )
    if active_only:
        q = q.eq("active", True)
    result = q.execute()
    return result.data or []


def get_working_hours(tenant_id: str) -> list[dict]:
    """Fasce orarie con eventuale location annidata."""
    if not tenant_id:
        return []
    sb = get_supabase()
    result = (
        sb.table("working_hours")
        .select("*, locations(id, name, city, address)")
        .eq("tenant_id", tenant_id)
        .eq("active", True)
        .order("day_of_week")
        .order("start_time")
        .execute()
    )
    return result.data or []


def get_services(tenant_id: str) -> list[dict]:
    if not tenant_id:
        return []
    sb = get_supabase()
    result = (
        sb.table("services")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("active", True)
        .order("sort_order")
        .execute()
    )
    return result.data or []


def get_exceptions(tenant_id: str) -> list[dict]:
    if not tenant_id:
        return []
    sb = get_supabase()
    result = (
        sb.table("calendar_exceptions")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("active", True)
        .order("date")
        .execute()
    )
    return result.data or []


def get_enabled_holidays(tenant_id: str, country: str = "IT") -> list[dict]:
    """Festività abilitate per il tenant (default: tutte quelle IT)."""
    if not tenant_id:
        return []
    sb = get_supabase()

    holidays_res = (
        sb.table("holidays")
        .select("*")
        .eq("country", country)
        .order("date")
        .execute()
    )
    holidays = holidays_res.data or []

    th_res = (
        sb.table("tenant_holidays")
        .select("holiday_id, enabled")
        .eq("tenant_id", tenant_id)
        .execute()
    )
    th_map = {row["holiday_id"]: row["enabled"] for row in (th_res.data or [])}

    # Se non ci sono override, tutte abilitate
    result = []
    for h in holidays:
        enabled = th_map.get(h["id"], True)
        if enabled:
            result.append(h)
    return result


def format_working_hours_text(working_hours: list[dict], locations: list[dict] | None = None) -> str:
    """
    Genera testo leggibile degli orari per AI / risposte WhatsApp.
    Esempio:
      Lunedì: 09:00–13:00 · 14:00–18:00 (Milano Centro)
      Martedì: 09:00–13:00 (Roma Eur)
    """
    loc_map = {}
    if locations:
        for loc in locations:
            loc_map[loc["id"]] = loc.get("name") or ""

    by_day: dict[int, list] = {}
    for wh in working_hours:
        d = wh.get("day_of_week")
        if d is None:
            continue
        by_day.setdefault(d, []).append(wh)

    lines = []
    for day in range(1, 8):
        name = DAY_NAMES.get(day, str(day))
        slots = by_day.get(day) or []
        if not slots:
            lines.append(f"{name}: chiuso")
            continue
        parts = []
        for s in slots:
            start = str(s.get("start_time") or "")[:5]
            end = str(s.get("end_time") or "")[:5]
            loc_name = ""
            loc = s.get("locations")
            if isinstance(loc, dict):
                loc_name = loc.get("name") or ""
            elif s.get("location_id") and s["location_id"] in loc_map:
                loc_name = loc_map[s["location_id"]]
            if loc_name:
                parts.append(f"{start}–{end} ({loc_name})")
            else:
                parts.append(f"{start}–{end}")
        lines.append(f"{name}: {' · '.join(parts)}")
    return "\n".join(lines)


def format_services_text(services: list[dict]) -> str:
    if not services:
        return "Nessun servizio configurato."
    lines = []
    for s in services:
        name = s.get("name") or "Servizio"
        duration = s.get("duration_minutes") or 30
        price = s.get("price")
        desc = s.get("description") or ""
        part = f"• {name} ({duration} min)"
        if price is not None:
            part += f" – €{price}"
        if desc:
            part += f"\n  {desc}"
        lines.append(part)
    return "\n".join(lines)


def format_locations_text(locations: list[dict]) -> str:
    if not locations:
        return "Nessuna sede configurata."
    lines = []
    for loc in locations:
        name = loc.get("name") or "Sede"
        city = loc.get("city") or ""
        address = loc.get("address") or ""
        line = f"• {name}"
        if city:
            line += f" – {city}"
        if address:
            line += f"\n  {address}"
        lines.append(line)
    return "\n".join(lines)


def get_tenant_knowledge(tenant_id: str) -> dict:
    """
    Knowledge completa da passare al context / AI / n8n.
    """
    locations = get_locations(tenant_id)
    working_hours = get_working_hours(tenant_id)
    services = get_services(tenant_id)
    exceptions = get_exceptions(tenant_id)
    holidays = get_enabled_holidays(tenant_id)

    return {
        "locations": locations,
        "working_hours": working_hours,
        "services": services,
        "exceptions": exceptions,
        "holidays": holidays,
        "working_hours_text": format_working_hours_text(working_hours, locations),
        "services_text": format_services_text(services),
        "locations_text": format_locations_text(locations),
    }
