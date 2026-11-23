"""F7 – Assistent „Neues Gerät komplett“:
übernehmen → reservieren → neue IP → Firmware → Standardeinstellungen → Zertifikat."""

from __future__ import annotations

from dataclasses import dataclass

from ..context import Context
from ..jobs import Progress
from .adopt import adopt
from .certificate import CertificateResult, install_certificate
from .common import noop_progress, step, validate_name
from .defaults import DefaultsResult, apply_defaults
from .firmware import FirmwareResult, update_firmware
from .reservation import ReservationResult, apply_reservation, reserve


@dataclass(frozen=True)
class ProvisionResult:
    reservation: ReservationResult
    firmware: FirmwareResult | None
    certificate: CertificateResult | None
    defaults: DefaultsResult | None = None


async def provision(
    ctx: Context,
    host: str,
    name: str,
    *,
    ip: str | None = None,
    stage: str | None = None,
    update: bool = True,
    defaults: bool = True,
    certificate: bool = True,
    progress: Progress = noop_progress,
) -> ProvisionResult:
    """`name` ist der Gerätename (Groß-/Kleinschreibung bleibt erhalten); der Hostname ist der Name klein."""
    name = validate_name(name)
    with step(f"Neues Gerät {name}"):
        progress("Schritt 1/5: Gerät übernehmen")
        with step("1/5 Übernehmen"):
            device = await adopt(ctx, host, progress)
        assert device.mac

        progress("Schritt 2/5: IP-Reservierung")
        with step("2/5 IP-Reservierung"):
            res = await reserve(ctx, device.mac, name, ip, progress)
            await apply_reservation(ctx, host, res, progress)

        device = ctx.store.get_device(device.key) or device

        fw = None
        if update:
            progress("Schritt 3/5: Firmware")
            with step("3/5 Firmware"):
                fw = await update_firmware(ctx, res.ip, stage, progress, device)
        settings = None
        if defaults:
            progress("Schritt 4/5: Standardeinstellungen")
            with step("4/5 Standardeinstellungen"):
                device = ctx.store.get_device(device.key) or device
                settings = await apply_defaults(ctx, res.ip, name, device, progress)
        cert = None
        if certificate:
            progress("Schritt 5/5: Zertifikat")
            with step("5/5 Zertifikat"):
                cert = await install_certificate(ctx, res.ip, name, progress, device)
        progress("Gerät vollständig eingerichtet")
        return ProvisionResult(res, fw, cert, settings)
