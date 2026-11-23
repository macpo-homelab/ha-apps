"""Schritt-Protokollierung: welcher Arbeitsschritt läuft gerade, wo ist ein Fehler passiert.

Services markieren Arbeitsschritte mit `with step("Firmware prüfen"):`. Schritte lassen sich schachteln
(z. B. "Neues Gerät > IP-Reservierung > Kea neu laden"). Jeder Schritt wird mit Start, Ende und Dauer
geloggt. Bei einem Fehler merkt sich die Ausnahme den innersten Schritt (`failed_step`), damit Job-Log und
Protokoll genau sagen können, wo es passiert ist.

Der Pfad liegt in einer ContextVar – jeder Hintergrund-Job (eigener asyncio-Task) hat seinen eigenen.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

log = logging.getLogger("device_manager.steps")

SEPARATOR = " > "
_STEP_ATTR = "_sm_step_path"

_path: ContextVar[tuple[str, ...]] = ContextVar("sm_step_path", default=())
# Optionaler Empfänger für Schrittwechsel (Job-Anzeige „aktueller Schritt“)
_listener: ContextVar[Callable[[str], None] | None] = ContextVar("sm_step_listener", default=None)


def current_step() -> str:
    return SEPARATOR.join(_path.get())


def set_listener(listener: Callable[[str], None] | None) -> None:
    _listener.set(listener)


@contextmanager
def step(name: str) -> Iterator[None]:
    path = (*_path.get(), name)
    label = SEPARATOR.join(path)
    token = _path.set(path)
    listener = _listener.get()
    if listener:
        listener(label)
    log.info("Schritt START  %s", label)
    started = time.monotonic()
    try:
        yield
    except BaseException as exc:
        elapsed = time.monotonic() - started
        if failed_step(exc) is None:
            _mark(exc, label)
            log.warning("Schritt FEHLER %s (nach %.1fs): %s", label, elapsed, describe_error(exc))
        else:
            log.debug("Schritt ABBRUCH %s (nach %.1fs, Fehler in Unterschritt)", label, elapsed)
        raise
    else:
        log.info("Schritt OK     %s (%.1fs)", label, time.monotonic() - started)
    finally:
        _path.reset(token)
        if listener and _path.get():
            listener(SEPARATOR.join(_path.get()))


def _mark(exc: BaseException, label: str) -> None:
    try:
        setattr(exc, _STEP_ATTR, label)
    except (AttributeError, TypeError):  # pragma: no cover - Ausnahmen ohne __dict__
        pass


def failed_step(exc: BaseException) -> str | None:
    """Innerster Schritt, in dem die Ausnahme aufgetreten ist (oder None)."""
    value = getattr(exc, _STEP_ATTR, None)
    return value if isinstance(value, str) else None


def describe_error(exc: BaseException, *, max_depth: int = 5) -> str:
    """'Typ: Meldung <- Ursache: Typ: Meldung ...' – die Ursachenkette in einer Zeile.

    Ursachen, deren Meldung schon weiter vorne steht (z. B. httpx- und httpcore-Ausnahmen mit identischem Text),
    werden übersprungen, damit nichts doppelt erscheint.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < max_depth:
        seen.add(id(current))
        name = type(current).__name__
        message = str(current).strip()
        already_shown = any((message or name) in part for part in parts)
        if not already_shown:
            parts.append(f"{name}: {message}" if message else name)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return " <- Ursache: ".join(parts)
