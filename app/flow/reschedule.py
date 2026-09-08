"""
Workflow SPOSTAMENTO appuntamento.

Chiamato dal router in main quando action_requested == MODIFY_BOOKING.
Stesso comportamento del blocco precedente in main: nessuna feature nuova.
"""

from __future__ import annotations

from typing import Any, Callable


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

    # ----------------------------------------------------------
    # WORKFLOW SPOSTAMENTO (macchina a stati lineare)
    # 1) Individua appuntamento (1 solo → automatico; 2+ → sì/no)
    # 2) Chiedi preferenza nuovo momento (se non già nel messaggio)
    # 3) Cerca e mostra slot
    # 4) Scelta slot → CONFIRM_BOOKING (gate numerico già attivo)
    # 5) create nuovo + cancel vecchio (niente richiesta nome)
    # ----------------------------------------------------------

    def _msg_has_move_preference(msg: str) -> bool:
        low = (msg or "").lower()
        if is_pure_yes(msg) or is_pure_no(msg):
            return False
        return any(
            p in low
            for p in (
                "luned", "marted", "mercoled", "gioved", "venerd", "sabat", "domenic",
                "domani", "settimana", "mattina", "pomeriggio", "sera",
                "prossim", "tra ", "il giorno", "alle ",
                "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
                "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
                "più tardi", "piu tardi", "altro giorno", "altra data",
            )
        )

    def _activate_modifying(appt: dict) -> None:
        new_collected["modifying_appointment"] = appt
        new_collected["pending_confirmation_appointment"] = None
        new_collected["rejected_appointment_ids"] = None
        # Contesto dal vecchio appuntamento (no nuova anagrafica)
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

    def _after_identified() -> None:
        """Preferenza nel messaggio → cerca slot; altrimenti chiedi."""
        if _msg_has_move_preference(combined_text):
            new_collected, slots_text_to_append = resolve_search_slots(
                tenant, knowledge, parameters, new_collected,
                previous_last_slots, backend_results,
            )
            # nonlocal-style: update outer slots_text via backend path
            backend_results["_slots_text_holder"] = slots
        else:
            backend_results["error_type"] = "ask_new_time_preference"
            backend_results["modify_from_label"] = appointment_label(
                new_collected.get("modifying_appointment") or {}
            )
            print("[MODIFY] chiedo preferenza nuovo orario")

    modifying = new_collected.get("modifying_appointment")

    if modifying:
        # Appuntamento già individuato: preferenza o ricerca
        if _msg_has_move_preference(combined_text) or any(
            parameters.get(k)
            for k in ("period", "weekday", "week_part", "date_from", "date_to", "time_preference")
        ):
            # Evita preferenze inventate: se pure yes, non cercare
            if is_pure_yes(combined_text) or is_pure_no(combined_text):
                backend_results["error_type"] = "ask_new_time_preference"
                backend_results["modify_from_label"] = appointment_label(modifying)
            else:
                new_collected, slots_text_to_append = resolve_search_slots(
                    tenant, knowledge, parameters, new_collected,
                    previous_last_slots, backend_results,
                )
        else:
            backend_results["error_type"] = "ask_new_time_preference"
            backend_results["modify_from_label"] = appointment_label(modifying)
            print("[MODIFY] in corso, ancora senza preferenza → chiedo")

    else:
        pending_appt = new_collected.get("pending_confirmation_appointment")
        confirmation = parameters.get("confirmation")

        if pending_appt and (confirmation == "no" or is_pure_no(combined_text)):
            rejected = new_collected.get("rejected_appointment_ids") or []
            rejected.append(pending_appt.get("id"))
            new_collected["rejected_appointment_ids"] = rejected
            new_collected["pending_confirmation_appointment"] = None

            candidates = appointment_repo.list_upcoming_for_customer(
                tenant_id=tenant["id"], customer_id=customer["id"], limit=3
            )
            next_candidate = next(
                (a for a in candidates if a.get("id") not in rejected), None
            )
            if next_candidate:
                new_collected["pending_confirmation_appointment"] = next_candidate
                backend_results["error_type"] = "appointment_confirmation_needed"
                backend_results["appointment_confirmation_label"] = appointment_label(
                    next_candidate
                )
            else:
                backend_results["error_type"] = "no_more_appointments_to_propose"

        elif pending_appt and (
            confirmation == "yes" or is_pure_yes(combined_text)
        ):
            _activate_modifying(pending_appt)
            if _msg_has_move_preference(combined_text):
                new_collected, slots_text_to_append = resolve_search_slots(
                    tenant, knowledge, parameters, new_collected,
                    previous_last_slots, backend_results,
                )
            else:
                backend_results["error_type"] = "ask_new_time_preference"
                backend_results["modify_from_label"] = appointment_label(pending_appt)
                print("[MODIFY] sì su pending → chiedo preferenza")

        else:
            # Primo ingresso nel workflow spostamento
            candidates = appointment_repo.list_upcoming_for_customer(
                tenant_id=tenant["id"], customer_id=customer["id"], limit=3
            )

            if not candidates:
                backend_results["error_type"] = "no_appointment_to_modify"
            elif len(candidates) == 1:
                # Caso tipico: un solo appuntamento → nessuna domanda sì/no
                _activate_modifying(candidates[0])
                if _msg_has_move_preference(combined_text):
                    new_collected, slots_text_to_append = resolve_search_slots(
                        tenant, knowledge, parameters, new_collected,
                        previous_last_slots, backend_results,
                    )
                else:
                    backend_results["error_type"] = "ask_new_time_preference"
                    backend_results["modify_from_label"] = appointment_label(
                        candidates[0]
                    )
                    print("[MODIFY] unico appuntamento → chiedo preferenza")
            else:
                # Raro: più appuntamenti → conferma sul più vicino
                first = candidates[0]
                new_collected["pending_confirmation_appointment"] = first
                new_collected["rejected_appointment_ids"] = []
                backend_results["error_type"] = "appointment_confirmation_needed"
                backend_results["appointment_confirmation_label"] = appointment_label(
                    first
                )
                print("[MODIFY] più appuntamenti → chiedo conferma sul primo")

    return new_collected, backend_results, slots_text_to_append
