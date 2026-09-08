"""
Workflow SPOSTAMENTO appuntamento.

Chiamato dal router in main quando action_requested == MODIFY_BOOKING.
"""

from __future__ import annotations

import re
from typing import Any, Callable


def _normalize_hhmm(value: Any) -> str | None:
    """Normalizza un orario appuntamento o testo a HH:MM."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    text = text.replace(".", ":")
    m = re.match(r"^(\d{1,2}):(\d{2})", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
    if re.fullmatch(r"\d{1,2}", text):
        h = int(text)
        if 0 <= h <= 23:
            return f"{h:02d}:00"
    return None


def _extract_times_from_message(msg: str) -> list[str]:
    """Estrae orari citati nel messaggio (17, 17:00, alle 17, delle 17 e 30)."""
    t = (msg or "").strip().lower()
    if not t:
        return []
    found: list[str] = []

    def _add(hhmm: str) -> None:
        if hhmm and hhmm not in found:
            found.append(hhmm)

    for m in re.finditer(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", t):
        _add(f"{int(m.group(1)):02d}:{m.group(2)}")

    for m in re.finditer(r"\b([01]?\d|2[0-3])\s*e\s*([0-5]\d)\b", t):
        _add(f"{int(m.group(1)):02d}:{m.group(2)}")

    for m in re.finditer(
        r"\b(?:alle|delle|ore)\s*([01]?\d|2[0-3])(?:[:.]([0-5]\d))?\b", t
    ):
        h = int(m.group(1))
        mi = m.group(2) or "00"
        _add(f"{h:02d}:{mi}")

    # Ore isolate (es. "delle 17") escludendo cifre già parte di HH:MM
    covered = set()
    for m in re.finditer(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", t):
        for i in range(m.start(), m.end()):
            covered.add(i)
    for m in re.finditer(r"\b([01]?\d|2[0-3])\s*e\s*([0-5]\d)\b", t):
        for i in range(m.start(), m.end()):
            covered.add(i)

    for m in re.finditer(r"\b([01]?\d|2[0-3])\b", t):
        if any(i in covered for i in range(m.start(), m.end())):
            continue
        h = int(m.group(1))
        if h == 0:
            continue
        span_before = t[max(0, m.start() - 12) : m.start()]
        if re.search(r"(slot|opzione|numero)\s*$", span_before):
            continue
        _add(f"{h:02d}:00")

    return found


def _appointment_time(appt: dict) -> str | None:
    return _normalize_hhmm(
        appt.get("appointment_time")
        or appt.get("time")
        or appt.get("start_time")
    )


def _match_appointments_by_message(
    candidates: list[dict],
    msg: str,
    parameters: dict | None = None,
) -> list[dict]:
    """Filtra candidati in base all'orario citato nel messaggio."""
    if not candidates:
        return []

    times = _extract_times_from_message(msg)
    if parameters:
        et = _normalize_hhmm(parameters.get("exact_time"))
        if et and et not in times:
            times.append(et)

    if not times:
        return []

    matched = []
    for appt in candidates:
        at = _appointment_time(appt)
        if not at:
            continue
        for tm in times:
            if at == tm:
                matched.append(appt)
                break
            # "alle 17" (17:00) → accetta qualsiasi slot in quell'ora (17:00, 17:30)
            if tm.endswith(":00") and at[:2] == tm[:2]:
                matched.append(appt)
                break

    seen = set()
    out = []
    for a in matched:
        aid = a.get("id")
        if aid in seen:
            continue
        seen.add(aid)
        out.append(a)
    return out


def _msg_has_destination_preference(msg: str, is_pure_yes, is_pure_no) -> bool:
    """True se indica DOVE spostare, non solo quale appuntamento."""
    low = (msg or "").lower()
    if is_pure_yes(msg) or is_pure_no(msg):
        return False
    return any(
        p in low
        for p in (
            "luned", "marted", "mercoled", "gioved", "venerd", "sabat", "domenic",
            "settimana", "prossim", "altro giorno", "altra data",
            "più tardi", "piu tardi", "tra una", "tra un ",
            "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
            "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
            "sposta a", "spostalo a", "spostare a", "mettere a", "mettilo a",
        )
    )


def handle_reschedule(
    *,
    tenant: dict,
    customer: dict | None,
    knowledge: dict,
    parameters: dict,
    new_collected: dict,
    previous_last_slots: list,
    backend_results: dict,
    combined_text: str,
    appointment_repo: Any,
    resolve_search_slots: Callable,
    appointment_label: Callable,
    is_pure_yes: Callable[[str], bool],
    is_pure_no: Callable[[str], bool],
) -> tuple[dict, dict, str]:
    """
    Esegue un passo del workflow spostamento.

    Ritorna: (new_collected, backend_results, slots_text_to_append)
    """
    slots_text_to_append = ""
    parameters = parameters or {}

    def _activate_modifying(appt: dict) -> None:
        new_collected["modifying_appointment"] = appt
        new_collected["pending_confirmation_appointment"] = None
        new_collected["rejected_appointment_ids"] = None
        new_collected["reschedule_candidates"] = None
        if appt.get("service") and not new_collected.get("service"):
            new_collected["service"] = appt.get("service")
        _pname = (
            appt.get("person_name")
            or appt.get("customer_name")
            or appt.get("client_name")
            or (customer or {}).get("name")
            or (customer or {}).get("full_name")
        )
        if _pname:
            new_collected["person_name"] = str(_pname).strip()
        new_collected["awaiting_person_name"] = False
        print(f"[MODIFY] appuntamento attivo: {appointment_label(appt)}")

    def _after_identified(appt: dict) -> None:
        nonlocal slots_text_to_append, new_collected
        if _msg_has_destination_preference(combined_text, is_pure_yes, is_pure_no):
            new_collected, slots_text_to_append = resolve_search_slots(
                tenant,
                knowledge,
                parameters,
                new_collected,
                previous_last_slots,
                backend_results,
            )
        else:
            backend_results["error_type"] = "ask_new_time_preference"
            backend_results["modify_from_label"] = appointment_label(appt)
            print("[MODIFY] identificato → chiedo preferenza destinazione")

    def _propose_confirmation(appt: dict, rejected: list | None = None) -> None:
        new_collected["pending_confirmation_appointment"] = appt
        new_collected["rejected_appointment_ids"] = list(rejected or [])
        backend_results["error_type"] = "appointment_confirmation_needed"
        backend_results["appointment_confirmation_label"] = appointment_label(appt)

    def _list_choices(candidates: list[dict]) -> None:
        lines = []
        for i, a in enumerate(candidates[:5], 1):
            lines.append(f"{i}. {appointment_label(a)}")
        backend_results["error_type"] = "appointment_choice_needed"
        backend_results["appointment_choice_text"] = (
            "Hai più di un appuntamento. Quale vuoi spostare?\n"
            + "\n".join(lines)
            + "\n\nRispondi con il numero oppure con l'orario (es. alle 17)."
        )
        new_collected["reschedule_candidates"] = candidates[:5]
        print("[MODIFY] elenco scelta appuntamento")

    modifying = new_collected.get("modifying_appointment")

    if modifying:
        if _msg_has_destination_preference(
            combined_text, is_pure_yes, is_pure_no
        ) or any(
            parameters.get(k)
            for k in (
                "period",
                "weekday",
                "week_part",
                "date_from",
                "date_to",
                "time_preference",
            )
        ):
            if is_pure_yes(combined_text) or is_pure_no(combined_text):
                backend_results["error_type"] = "ask_new_time_preference"
                backend_results["modify_from_label"] = appointment_label(modifying)
            else:
                new_collected, slots_text_to_append = resolve_search_slots(
                    tenant,
                    knowledge,
                    parameters,
                    new_collected,
                    previous_last_slots,
                    backend_results,
                )
        else:
            backend_results["error_type"] = "ask_new_time_preference"
            backend_results["modify_from_label"] = appointment_label(modifying)
            print("[MODIFY] in corso, senza destinazione → chiedo")

    else:
        pending_appt = new_collected.get("pending_confirmation_appointment")
        confirmation = parameters.get("confirmation")
        candidates_cached = new_collected.get("reschedule_candidates") or []

        # Scelta da elenco numerato o orario
        if candidates_cached:
            m_num = re.fullmatch(r"\s*([1-9]\d?)\s*", (combined_text or "").strip())
            if m_num:
                idx = int(m_num.group(1)) - 1
                if 0 <= idx < len(candidates_cached):
                    chosen = candidates_cached[idx]
                    _activate_modifying(chosen)
                    _after_identified(chosen)
                    return new_collected, backend_results, slots_text_to_append

            matched = _match_appointments_by_message(
                candidates_cached, combined_text, parameters
            )
            if len(matched) == 1:
                _activate_modifying(matched[0])
                _after_identified(matched[0])
                return new_collected, backend_results, slots_text_to_append

        if pending_appt and (confirmation == "no" or is_pure_no(combined_text)):
            rejected = new_collected.get("rejected_appointment_ids") or []
            rejected.append(pending_appt.get("id"))
            new_collected["rejected_appointment_ids"] = rejected
            new_collected["pending_confirmation_appointment"] = None

            candidates = appointment_repo.list_upcoming_for_customer(
                tenant_id=tenant["id"],
                customer_id=customer["id"],
                limit=5,
            )
            remaining = [a for a in candidates if a.get("id") not in rejected]

            matched = _match_appointments_by_message(
                remaining, combined_text, parameters
            )
            if len(matched) == 1:
                _activate_modifying(matched[0])
                _after_identified(matched[0])
            elif len(remaining) == 1:
                _activate_modifying(remaining[0])
                _after_identified(remaining[0])
            elif len(remaining) > 1:
                _list_choices(remaining)
            else:
                backend_results["error_type"] = "no_more_appointments_to_propose"

        elif pending_appt and (confirmation == "yes" or is_pure_yes(combined_text)):
            _activate_modifying(pending_appt)
            _after_identified(pending_appt)

        elif pending_appt:
            candidates = appointment_repo.list_upcoming_for_customer(
                tenant_id=tenant["id"],
                customer_id=customer["id"],
                limit=5,
            )
            matched = _match_appointments_by_message(
                candidates, combined_text, parameters
            )
            if len(matched) == 1:
                _activate_modifying(matched[0])
                _after_identified(matched[0])
            elif len(matched) > 1:
                _list_choices(matched)
            else:
                _propose_confirmation(
                    pending_appt,
                    new_collected.get("rejected_appointment_ids") or [],
                )

        else:
            candidates = appointment_repo.list_upcoming_for_customer(
                tenant_id=tenant["id"],
                customer_id=customer["id"],
                limit=5,
            )

            if not candidates:
                backend_results["error_type"] = "no_appointment_to_modify"
            elif len(candidates) == 1:
                _activate_modifying(candidates[0])
                _after_identified(candidates[0])
            else:
                matched = _match_appointments_by_message(
                    candidates, combined_text, parameters
                )
                if len(matched) == 1:
                    _activate_modifying(matched[0])
                    _after_identified(matched[0])
                    print("[MODIFY] match orario dal messaggio")
                elif len(matched) > 1:
                    _list_choices(matched)
                else:
                    _list_choices(candidates)

    return new_collected, backend_results, slots_text_to_append
