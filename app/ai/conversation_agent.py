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
Specialità: {self.specialty or "Consulenza professionale"}

ORARI:
{self._format_working_hours()}

SERVIZI:
{self._format_services()}

SEDI:
{self._format_locations()}

=== DATA E ORA ===
Oggi è {today_str}, ora {time_str}

=== SLOT MOSTRATI ===
{slots_text}

=== REGOLE ===
1. Sii naturale, cordiale e professionale.
2. Aggiorna lo stato JSON con le informazioni raccolte.
3. Se l'utente corregge un dato, SOVRASCRIVILO.
4. Prima di finalizzare, chiedi SEMPRE conferma esplicita.
5. Se l'utente saluta dopo conferma, chiudi la conversazione.

=== STRUTTURA STATO ===
{{
  "service": "nome servizio o null",
  "person_name": "nome cliente o null",
  "preferences": {{
    "date": "YYYY-MM-DD o null",
    "time": "HH:MM o null",
    "period": "today|tomorrow|this_week|next_week o null",
    "time_preference": "morning|afternoon|evening o null"
  }},
  "selected_slot": {{
    "datetime": "ISO datetime",
    "date": "YYYY-MM-DD",
    "time": "HH:MM",
    "label": "descrizione"
  }} o null,
  "step": "greeting|collecting_info|showing_slots|confirming|completed|no_slots",
  "slots_shown": [],
  "conversation_ended": false
}}

=== AZIONI ===
- "search_availability": quando hai servizio + data/ora per cercare slot
- "create_booking": quando l'utente ha confermato tutti i dati
- "request_human": quando l'utente chiede un operatore

=== CORREZIONI ORARIO ===
Esempio: utente dice "Il 4 alle 15:30" poi "Alle 15 sarebbe meglio"
- Trova lo slot alle 15:00 in slots_shown
- SOSTITUISCI selected_slot con quello
- Chiedi conferma: "Quindi confermi Lunedì 7 alle 15:00?"

=== SALUTI FINALI ===
Dopo conferma, utente dice "Ok grazie"
- conversation_ended = true
- Rispondi con saluto e riepilogo

Rispondi SEMPRE in italiano.
Restituisci SEMPRE JSON con: reply, state, action, done, need_confirmation.
"""

    def _build_user_prompt(self, context: dict) -> str:
        state = context.get("state", {})
        history = context.get("recent_messages", [])
        current_message = context.get("message", "")
        slots_found = context.get("slots_found", False)

        history_text = ""
        for msg in history[-6:]:
            role = "Cliente" if msg.get("role") == "user" else "Assistente"
            content = msg.get("content", "")
            history_text += f"{role}: {content}\n"

        status_text = ""
        if slots_found:
            status_text = "\n⚠️ ATTENZIONE: Sono stati appena trovati nuovi slot. Mostrali all'utente e chiedi quale preferisce.\n"

        return f"""{status_text}
=== STATO ATTUALE ===
{json.dumps(state, indent=2, ensure_ascii=False)}

=== STORIA RECENTE ===
{history_text}

=== MESSAGGIO ATTUALE ===
{current_message}

=== ISTRUZIONI ===
Analizza il messaggio, aggiorna lo stato se necessario, e rispondi.
Se l'utente ha scelto uno slot, imposta selected_slot.
Se l'utente ha confermato, imposta action = "create_booking".
Se l'utente ha chiesto un operatore, imposta action = "request_human".
Se il messaggio è un saluto dopo conferma, conversation_ended = true.

Restituisci SOLO il JSON richiesto.
"""

    def _format_working_hours(self) -> str:
        hours = self.knowledge.get("working_hours", [])
        if not hours:
            return "Lun-Ven 9:00-18:00"
        days = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
        lines = []
        for h in hours:
            dow = int(h.get("day_of_week", 0))
            start = h.get("start_time", "")[:5]
            end = h.get("end_time", "")[:5]
            lines.append(f"{days[dow-1]}: {start}-{end}")
        return "\n".join(lines) if lines else "Orari non specificati"

    def _format_services(self) -> str:
        services = self.knowledge.get("services", [])
        if not services:
            return "Consulenza professionale"
        return "\n".join(f"- {s.get('name')}" for s in services)

    def _format_locations(self) -> str:
        locations = self.knowledge.get("locations", [])
        if not locations:
            return "Sede principale"
        return "\n".join(f"- {l.get('name')}" for l in locations)

    def _format_slots(self, slots: list) -> str:
        if not slots:
            return "Nessuno slot disponibile al momento."
        lines = []
        for i, slot in enumerate(slots, 1):
            label = slot.get("label", f"Slot {i}")
            lines.append(f"{i}. {label}")
        return "\n".join(lines)
