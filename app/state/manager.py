"""
Gestore dello stato della conversazione.
"""

from copy import deepcopy
from typing import Optional


class StateManager:
    """
    Gestisce lo stato della conversazione in modo strutturato.
    """
    
    @staticmethod
    def initial_state() -> dict:
        """Stato iniziale di una nuova conversazione."""
        return {
            "service": None,
            "person_name": None,
            "preferences": {
                "date": None,
                "time": None,
                "period": None,
                "time_preference": None,
            },
            "selected_slot": None,
            "step": "greeting",
            "slots_shown": [],
            "conversation_ended": False,
        }
    
    @staticmethod
    def merge(existing: dict, updates: dict) -> dict:
        """
        Merge profondo tra stato esistente e aggiornamenti.
        Sovrascrive i valori, non li accumula.
        """
        result = deepcopy(existing)
        
        for key, value in updates.items():
            if value is None:
                continue
            if isinstance(value, dict) and key in result and isinstance(result[key], dict):
                result[key] = StateManager.merge(result[key], value)
            else:
                result[key] = value
        
        return result
    
    @staticmethod
    def is_ready_for_search(state: dict) -> bool:
        """Verifica se abbiamo abbastanza info per cercare slot."""
        return bool(
            state.get("service") and (
                state.get("preferences", {}).get("date") or
                state.get("preferences", {}).get("period") or
                state.get("preferences", {}).get("time_preference")
            )
        )
    
    @staticmethod
    def is_ready_for_booking(state: dict) -> bool:
        """Verifica se abbiamo tutto per prenotare."""
        return bool(
            state.get("service") and
            state.get("person_name") and
            state.get("selected_slot")
        )
    
    @staticmethod
    def extract_collected_data(state: dict) -> dict:
        """
        Estrae i dati raccolti dallo stato per compatibilità con il vecchio engine.
        """
        return {
            "service": state.get("service"),
            "person_name": state.get("person_name"),
            "preferences": state.get("preferences", {}),
            "selected_slot": state.get("selected_slot"),
        }