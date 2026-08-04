"""
Wraps MAVSDK-python for one vehicle. This is the ONLY module that talks to
the flight controller. It exposes async telemetry subscriptions and HOLD /
RTL / offboard-formation command methods, and knows nothing about election
or term logic -- the coordinator passes it a `term` for gating, it does not
compute or check terms itself.

Requires: pip install mavsdk (MAVSDK-python 3.5.13 per system spec)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from mavsdk import System
from mavsdk.offboard import PositionNedYaw, OffboardError

log = logging.getLogger("mavsdk_interface")


@dataclass
class VehicleTelemetryState:
    battery_pct: float
    fc_link_ok: bool           # False if telemetry stream itself has gone stale
    px4_failsafe_active: bool
    armed: bool
    flight_mode: str


class MavsdkInterface:
    """
    One instance per vehicle, running on that vehicle's companion RPi.
    `system_address` is the MAVSDK connection string, e.g. "serial:///dev/ttyAMA0:921600"
    for the UART link specified in the system context.
    """

    # If we haven't received a fresh telemetry update in this long, treat the
    # FC link itself as severed even though the Pi/process is alive -- this is
    # the "zombie leader" case from 01_DESIGN.md section 2.
    FC_LINK_STALE_S = 1.0

    def __init__(self, system_address: str = "serial:///dev/ttyAMA0:921600"):
        self.system_address = system_address
        self.drone: Optional[System] = None
        self._last_telem_mono: float = 0.0
        self._latest: Optional[VehicleTelemetryState] = None
        self._offboard_active = False

    async def connect(self) -> None:
        self.drone = System()
        await self.drone.connect(system_address=self.system_address)
        log.info("waiting for FC connection...")
        async for state in self.drone.core.connection_state():
            if state.is_connected:
                log.info("FC connected on %s", self.system_address)
                break
        asyncio.get_running_loop().create_task(self._telemetry_loop())

    async def _telemetry_loop(self) -> None:
        """
        Fans in battery / flight-mode / failsafe telemetry into one
        VehicleTelemetryState. Runs forever; health_monitor.py polls
        `self.latest()` rather than subscribing to each stream itself, so
        MAVSDK-specific stream wiring stays entirely inside this module.
        """
        assert self.drone is not None

        async def battery_task():
            async for battery in self.drone.telemetry.battery():
                self._merge(battery_pct=battery.remaining_percent * 100.0)

        async def mode_task():
            async for mode in self.drone.telemetry.flight_mode():
                is_failsafe = str(mode).upper() in (
                    "RETURN_TO_LAUNCH", "LAND", "TERMINATION",
                )
                self._merge(flight_mode=str(mode), px4_failsafe_active=is_failsafe)

        async def armed_task():
            async for armed in self.drone.telemetry.armed():
                self._merge(armed=armed)

        await asyncio.gather(battery_task(), mode_task(), armed_task())

    def _merge(self, **kwargs) -> None:
        import time
        prev = self._latest
        self._latest = VehicleTelemetryState(
            battery_pct=kwargs.get("battery_pct", prev.battery_pct if prev else 100.0),
            fc_link_ok=True,
            px4_failsafe_active=kwargs.get(
                "px4_failsafe_active", prev.px4_failsafe_active if prev else False
            ),
            armed=kwargs.get("armed", prev.armed if prev else False),
            flight_mode=kwargs.get("flight_mode", prev.flight_mode if prev else "UNKNOWN"),
        )
        self._last_telem_mono = time.monotonic()

    def latest(self) -> Optional[VehicleTelemetryState]:
        """
        Returns the most recent telemetry snapshot, with fc_link_ok forced
        False if the stream has gone stale -- this is the concrete mechanism
        for detecting "Pi alive, FC link dead" without needing a dedicated
        MAVSDK API for it.
        """
        import time
        if self._latest is None:
            return None
        stale = (time.monotonic() - self._last_telem_mono) > self.FC_LINK_STALE_S
        if stale:
            self._latest.fc_link_ok = False
        return self._latest

    # ------------------------------------------------------------------
    # Flight control actions. All gated by term at the CALLER (coordinator)
    # level -- this module just executes what it's told.
    # ------------------------------------------------------------------
    async def hold_position(self) -> None:
        """Commanded the instant leader loss is suspected -- see state HOLD_SUSPECT."""
        assert self.drone is not None
        if self._offboard_active:
            try:
                await self.drone.offboard.stop()
            except OffboardError:
                pass
            self._offboard_active = False
        await self.drone.action.hold()
        log.info("HOLD commanded")

    async def resume_formation_command(self, north_m: float, east_m: float,
                                        down_m: float, yaw_deg: float) -> None:
        """
        Issue one formation-position setpoint. Coordinator is responsible for
        checking the message's term before ever calling this -- this method
        does not know about terms.
        """
        assert self.drone is not None
        if not self._offboard_active:
            await self.drone.offboard.set_position_ned(
                PositionNedYaw(north_m, east_m, down_m, yaw_deg)
            )
            try:
                await self.drone.offboard.start()
                self._offboard_active = True
            except OffboardError as e:
                log.error("offboard start failed: %s", e)
                await self.hold_position()
                return
        await self.drone.offboard.set_position_ned(
            PositionNedYaw(north_m, east_m, down_m, yaw_deg)
        )

    async def trigger_rtl(self) -> None:
        """Native PX4 failsafe path for a failed/stepping-down vehicle."""
        assert self.drone is not None
        if self._offboard_active:
            try:
                await self.drone.offboard.stop()
            except OffboardError:
                pass
            self._offboard_active = False
        await self.drone.action.return_to_launch()
        log.info("RTL commanded")
