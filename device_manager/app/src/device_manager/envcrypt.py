"""Verschlüsselte Secrets in der .env (Windows DPAPI).

Werte mit dem Präfix ``dpapi:`` werden beim Laden der Einstellungen entschlüsselt. Der Schlüssel wird von
Windows verwaltet und ist an das Benutzerkonto auf diesem Rechner gebunden – es liegt kein Schlüssel neben
der Datei. Eine kopierte .env ist auf anderen Rechnern bzw. für andere Benutzer wertlos.

Verschlüsseln:  python -m device_manager.envcrypt
"""

from __future__ import annotations

import base64
import ctypes
import getpass
import sys
from typing import Any

PREFIX = "dpapi:"
# Bindet die Werte zusätzlich an diese Anwendung (andere DPAPI-Nutzer können sie nicht ohne Weiteres lesen)
_ENTROPY = b"shelly-manager/v1"  # bewusst alter Name: bestehende dpapi:-Werte bleiben gültig
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class SecretError(ValueError):
    """Secret konnte nicht ver- oder entschlüsselt werden (ohne den Wert preiszugeben)."""


def is_supported() -> bool:
    return sys.platform == "win32"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _dpapi(data: bytes, *, encrypt: bool) -> bytes:
    if not is_supported():
        raise SecretError("dpapi:-Werte werden nur unter Windows unterstützt")
    windll: Any = getattr(ctypes, "windll")  # noqa: B009 - nur unter Windows vorhanden
    crypt32, kernel32 = windll.crypt32, windll.kernel32
    data_in, _keep_in = _blob(data)
    entropy, _keep_entropy = _blob(_ENTROPY)
    data_out = _Blob()
    if encrypt:
        ok = crypt32.CryptProtectData(
            ctypes.byref(data_in),
            "device-manager",
            ctypes.byref(entropy),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(data_out),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(data_in),
            None,
            ctypes.byref(entropy),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(data_out),
        )
    if not ok:
        action = "Verschlüsseln" if encrypt else "Entschlüsseln"
        raise SecretError(
            f"{action} mit DPAPI fehlgeschlagen (Windows-Fehler {getattr(ctypes, 'GetLastError')()}) – "  # noqa: B009
            "wurde der Wert mit einem anderen Benutzer/Rechner verschlüsselt?"
        )
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        kernel32.LocalFree(data_out.pbData)


def protect(plaintext: str) -> str:
    """Klartext -> 'dpapi:<base64>'."""
    return PREFIX + base64.b64encode(_dpapi(plaintext.encode("utf-8"), encrypt=True)).decode("ascii")


def unprotect(token: str) -> str:
    """'dpapi:<base64>' -> Klartext."""
    if not token.startswith(PREFIX):
        raise SecretError("Wert hat kein dpapi:-Präfix")
    try:
        raw = base64.b64decode(token[len(PREFIX) :], validate=True)
    except ValueError as exc:
        raise SecretError("dpapi:-Wert ist kein gültiges Base64") from exc
    return _dpapi(raw, encrypt=False).decode("utf-8")


def resolve(value: str) -> str:
    """Entschlüsselt dpapi:-Werte, alle anderen Werte bleiben unverändert."""
    return unprotect(value) if value.startswith(PREFIX) else value


def main(argv: list[str] | None = None) -> int:
    """Fragt ein Secret verdeckt ab und gibt die Zeile für die .env aus."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m device_manager.envcrypt",
        description="Secret für die .env mit Windows DPAPI verschlüsseln (nur für diesen Benutzer/Rechner).",
    )
    parser.add_argument("name", nargs="?", help="Variablenname, z. B. DM_SHELLY_PASSWORD")
    args = parser.parse_args(argv)
    if not is_supported():
        print("DPAPI ist nur unter Windows verfügbar.", file=sys.stderr)
        return 1
    first = getpass.getpass("Wert: ")
    if not first:
        print("Leerer Wert – abgebrochen.", file=sys.stderr)
        return 1
    if getpass.getpass("Wiederholen: ") != first:
        print("Eingaben stimmen nicht überein – abgebrochen.", file=sys.stderr)
        return 1
    token = protect(first)
    print(f"{args.name}={token}" if args.name else token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
