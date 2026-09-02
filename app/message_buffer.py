"""
Debounce + Lock per messaggi WhatsApp.

- Raggruppa messaggi ravvicinati dello stesso numero (debounce)
- Evita elaborazioni parallele sullo stesso numero (lock)

NB: usa asyncio.create_task/asyncio.sleep, non threading.Timer.
Il debounce precedente girava su un thread separato: su alcuni hosting
PaaS (Render incluso, in certe configurazioni) i thread di background
non sono garantiti restare attivi/schedulati fuori dal ciclo di
richiesta HTTP, quindi il flush poteva non scattare mai. Con asyncio
il timer vive nello stesso event loop di FastAPI/uvicorn, che sappiamo
per certo essere attivo (è quello che ha appena gestito la POST).
"""

import asyncio
from collections import defaultdict
from typing import Awaitable, Callable

from app.config import Config


# Secondi di attesa dopo l'ultimo messaggio prima di processare
DEBOUNCE_SECONDS = float(
    getattr(Config, "MESSAGE_DEBOUNCE_SECONDS", 10)
)

# Massimo messaggi tenuti in coda per un numero
MAX_BUFFERED_MESSAGES = 6

# Attesa breve prima di ritentare se un flush trova già un'elaborazione
# in corso per lo stesso numero (evita elaborazioni parallele)
RETRY_SECONDS = 2.0


class MessageBuffer:
    def __init__(self):
        self._buffers: dict[str, list[dict]] = defaultdict(list)
        self._tasks: dict[str, asyncio.Task] = {}
        self._processing: set[str] = set()
        self._lock = asyncio.Lock()

    async def add_message(
        self,
        phone: str,
        message: dict,
        process_fn: Callable[[list[dict]], Awaitable[None]],
    ) -> None:
        """
        Aggiunge un messaggio al buffer del numero.
        Se è il primo, avvia il timer.
        Se ne arrivano altri, resetta il timer (cancella e ricrea il task).
        Quando il timer scade → processa tutto insieme.

        process_fn deve essere una funzione ASYNC (coroutine).
        """
        async with self._lock:
            buf = self._buffers[phone]
            buf.append(message)

            if len(buf) > MAX_BUFFERED_MESSAGES:
                self._buffers[phone] = buf[-MAX_BUFFERED_MESSAGES:]

            old_task = self._tasks.get(phone)
            if old_task and not old_task.done():
                old_task.cancel()

            self._tasks[phone] = asyncio.create_task(
                self._schedule_flush(phone, process_fn, DEBOUNCE_SECONDS)
            )

            print(f"[buffer] {phone}: {len(self._buffers[phone])} msg in coda "
                  f"(attendo {DEBOUNCE_SECONDS}s)")

    async def _schedule_flush(self, phone: str, process_fn: Callable, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            # È arrivato un nuovo messaggio nel frattempo: il timer
            # precedente viene semplicemente abbandonato, ne è già
            # partito uno nuovo da add_message().
            return
        await self._flush(phone, process_fn)

    async def _flush(self, phone: str, process_fn: Callable) -> None:
        """Chiamato allo scadere del timer. Processa i messaggi accumulati."""
        async with self._lock:
            messages = self._buffers.pop(phone, [])
            self._tasks.pop(phone, None)

            if not messages:
                return

            if phone in self._processing:
                print(f"[buffer] {phone}: già in processing, re-accodo {len(messages)} msg")
                self._buffers[phone] = messages
                self._tasks[phone] = asyncio.create_task(
                    self._schedule_flush(phone, process_fn, RETRY_SECONDS)
                )
                return

            self._processing.add(phone)

        try:
            print(f"[buffer] {phone}: processo {len(messages)} messaggi insieme")
            await process_fn(messages)
        except Exception as e:
            print(f"[buffer] Errore processing {phone}: {e}")
        finally:
            async with self._lock:
                self._processing.discard(phone)

                # Se nel frattempo sono arrivati altri messaggi, avvia un nuovo ciclo
                if self._buffers.get(phone):
                    self._tasks[phone] = asyncio.create_task(
                        self._schedule_flush(phone, process_fn, DEBOUNCE_SECONDS)
                    )


# Istanza globale (una per processo)
message_buffer = MessageBuffer()
