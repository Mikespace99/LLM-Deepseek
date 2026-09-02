"""
Agente conversazionale unificato con LLM.
Gestisce intent, stato, e risposta in un unico passaggio.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo
from openai import OpenAI

from app.config import Config

client = OpenAI(api_key=Config.OPENAI_API_KEY)


class ConversationAgent:
    def __init__(self, tenant: dict, knowledge: dict):
        self.tenant = tenant
        self.knowledge = knowledge
        self.timezone = tenant.get("timezone", "Europe/Rome")
        self.business_name = tenant.get("business_name", "Studio")
        self.specialty = tenant.get("specialty", "")

    def process(self, context: dict) -> dict:
        if context.get("slots_found"):
            search_result = context.get("search_result", {})
            slots = search_result.get("candidate_slots", [])
            state = context.get("state", {})
            state["slots_shown"] = slots
            state["step"] = "showing_slots" if slots else "no_slots"
            context["state"] = state

        if context.get("booking_result"):
            booking_result = context.get("booking_result", {})
            if booking_result.get("result", {}).get("success"):
                state = context.get("state", {})
                state["step"] = "completed"
                state["conversation_ended"] = True
                context["state"] = state

        system_prompt = self._build_system_prompt(context)
        user_prompt = self._build_user_prompt(context)

        try:
            response = client.chat.completions.create(
                model=Config.AI_MODEL_RESPONSE,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.2,
                response_format={"type": "json_object"}
            )
            raw = response.choices[0].message.content
            result = json.loads(raw)
            return {
                "reply": result.get("reply", "Non ho capito, puoi ripetere?"),
                "state": result.get("state", context.get("state", {})),
                "action": result.get("action"),
                "done": result.get("done", False),
                "need_confirmation": result.get("need_confirmation", False),
            }
        except Exception as e:
            print(f"[ConversationAgent] Errore: {e}")
            return {
                "reply": "Mi dispiace, ho avuto un problema. Puoi ripetere?",
                "state": context.get("state", {}),
                "action": None,
                "done": False,
                "need_confirmation": False,
            }

    def _build_system_prompt(self, context: dict) -> str:
        now = datetime.now(ZoneInfo(self.timezone))
        today_str = now.strftime("%A %d %B %Y")
        time_str = now.strftime("%H:%M")

        state = context.get("state", {})
        slots_shown = state.get("slots_shown", [])
        slots_text = self._format_slots(slots_shown) if slots_shown else "Nessuno slot mostrato al momento."

        return f"""Sei un assistente di prenotazione professionale.

=== DATI STUDIO ===
Nome: {self.business_name}
Specialità: {self.specialty}

=== DATA E ORA ATTUALI ===
Data: {today_str}
Ora: {time_str}
Fuso orario: {self.timezone}

=== SLOT DISPONIBILI ===
{slots_text}

=== STATO CONVERSAZIONE ===
{json.dumps(state, ensure_ascii=False)}

Rispondi sempre in JSON valido con questa struttura:
{{
    "reply": "risposta da mostrare all'utente",
    "state": {{}},
    "action": null,
    "done": false,
    "need_confirmation": false
}}

Sii professionale, chiaro e conciso.
"""
