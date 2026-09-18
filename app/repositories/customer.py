"""
Repository clienti.
- Normalizza sempre il numero di telefono
- Gestisce race condition sul primo insert
"""

import re
from app.supabase_client import get_supabase


def normalize_phone(phone: str | None) -> str:
    """
    Normalizza il numero WhatsApp:
    - rimuove spazi, trattini, parentesi
    - rimuove il prefisso +
    - lascia solo cifre
    Esempio: "+39 333-1234567" → "393331234567"
    """
    if not phone:
        return ""
    # Solo cifre
    digits = re.sub(r"\D", "", str(phone))
    return digits


def search_customers(tenant_id: str, query: str, limit: int = 8) -> list[dict]:
    """
    Autocomplete per l'Agenda: cerca per nome o numero (contiene, case
    insensitive). Usato quando lo staff crea un appuntamento a mano e
    vuole riusare un cliente già esistente invece di crearne uno nuovo.
    """
    q = (query or "").strip()
    if not q:
        return []
    sb = get_supabase()
    digits = normalize_phone(q)

    by_name = (
        sb.table("customers")
        .select("id, full_name, phone_number")
        .eq("tenant_id", tenant_id)
        .ilike("full_name", f"%{q}%")
        .limit(limit)
        .execute()
    ).data or []

    by_phone = []
    if digits:
        by_phone = (
            sb.table("customers")
            .select("id, full_name, phone_number")
            .eq("tenant_id", tenant_id)
            .ilike("phone_number", f"%{digits}%")
            .limit(limit)
            .execute()
        ).data or []

    seen = set()
    merged = []
    for row in by_name + by_phone:
        if row["id"] not in seen:
            seen.add(row["id"])
            merged.append(row)
    return merged[:limit]


def update_customer_name(tenant_id: str, customer_id: str, full_name: str) -> dict | None:
    sb = get_supabase()
    res = (
        sb.table("customers")
        .update({"full_name": full_name})
        .eq("tenant_id", tenant_id)
        .eq("id", customer_id)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def list_customers(tenant_id: str, search: str = "", limit: int = 20, offset: int = 0) -> dict:
    """
    Lista clienti per la pagina /clienti, con ultimo e prossimo
    appuntamento calcolati lato Python (il client Supabase/postgrest non
    supporta aggregazioni per-riga in una singola query REST).
    """
    from datetime import date as _date

    sb = get_supabase()
    q = (
        sb.table("customers")
        .select("id, full_name, phone_number, email, created_at", count="exact")
        .eq("tenant_id", tenant_id)
    )
    search = (search or "").strip()
    if search:
        digits = normalize_phone(search)
        phone_pattern = digits or search
        q = q.or_(f"full_name.ilike.%{search}%,phone_number.ilike.%{phone_pattern}%")
    q = q.order("full_name").range(offset, offset + limit - 1)

    res = q.execute()
    customers = res.data or []
    total = res.count or 0

    ids = [c["id"] for c in customers]
    appts = []
    if ids:
        appts = (
            sb.table("appointments")
            .select("customer_id, appointment_date, appointment_time, status")
            .eq("tenant_id", tenant_id)
            .in_("customer_id", ids)
            .neq("status", "cancelled")
            .execute()
        ).data or []

    today_str = _date.today().isoformat()
    by_customer: dict[str, list[dict]] = {}
    for a in appts:
        by_customer.setdefault(a["customer_id"], []).append(a)

    for c in customers:
        rows = sorted(
            by_customer.get(c["id"], []),
            key=lambda r: (r["appointment_date"], r["appointment_time"]),
        )
        past = [r for r in rows if r["appointment_date"] <= today_str]
        future = [r for r in rows if r["appointment_date"] > today_str]
        c["last_appointment"] = past[-1]["appointment_date"] if past else None
        c["next_appointment"] = future[0]["appointment_date"] if future else None

    return {"items": customers, "total": total}


def get_customer(tenant_id: str, customer_id: str) -> dict | None:
    sb = get_supabase()
    res = (
        sb.table("customers")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("id", customer_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def create_customer_manual(
    tenant_id: str, full_name: str, phone_number: str, email: str | None = None
) -> dict:
    """Creazione esplicita dalla dashboard (a differenza di get_or_create_customer,
    usata dal bot WhatsApp, qui il nome è sempre fornito dallo staff)."""
    sb = get_supabase()
    payload = {
        "tenant_id": tenant_id,
        "full_name": full_name,
        "phone_number": normalize_phone(phone_number),
        "email": email,
    }
    payload = {k: v for k, v in payload.items() if v not in (None, "")}
    res = sb.table("customers").insert(payload).execute()
    return res.data[0]


def update_customer(tenant_id: str, customer_id: str, **fields) -> dict | None:
    allowed = {"full_name", "phone_number", "email"}
    payload = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "phone_number" in payload:
        payload["phone_number"] = normalize_phone(payload["phone_number"])
    if not payload:
        return get_customer(tenant_id, customer_id)

    sb = get_supabase()
    res = (
        sb.table("customers")
        .update(payload)
        .eq("tenant_id", tenant_id)
        .eq("id", customer_id)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def count_new_customers(tenant_id: str, date_from_iso: str) -> int:
    sb = get_supabase()
    res = (
        sb.table("customers")
        .select("id", count="exact")
        .eq("tenant_id", tenant_id)
        .gte("created_at", date_from_iso)
        .execute()
    )
    return res.count or 0


def get_or_create_customer(tenant_id: str, phone_number: str) -> dict:
    """
    Recupera o crea il cliente.
    - Normalizza sempre il numero
    - In caso di race condition sull'insert (unique violation),
      riprova con una select.
    """
    phone = normalize_phone(phone_number)
    if not phone:
        raise ValueError("phone_number vuoto dopo normalizzazione")

    sb = get_supabase()

    # 1. Cerca esistente
    result = (
        sb.table("customers")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("phone_number", phone)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]

    # 2. Prova insert
    try:
        insert = (
            sb.table("customers")
            .insert({
                "tenant_id": tenant_id,
                "phone_number": phone,
            })
            .execute()
        )
        if insert.data:
            return insert.data[0]
    except Exception as e:
        # Probabile unique violation per race condition
        print(f"[customer] Insert fallito (possibile race): {e}")

    # 3. Retry select (l'altro thread ha creato il record)
    result = (
        sb.table("customers")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("phone_number", phone)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]

    raise RuntimeError(
        f"Impossibile creare o recuperare customer tenant={tenant_id} phone={phone}"
    )
