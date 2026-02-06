"""Support for Anova Coordinators."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any, Optional

from anova_wifi import AnovaApi, APCUpdate, APCWifiDevice

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass
class AnovaData:
    """Data for the Anova integration."""

    api_jwt: str
    coordinators: list["AnovaCoordinator"]
    api: AnovaApi


type AnovaConfigEntry = ConfigEntry[AnovaData]


def _dig(d: dict, path: list[str], default: Any = None) -> Any:
    """Safe nested getter."""
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _enrich_sensor_from_raw(sensor_obj: Any, raw: dict) -> None:
    """Attach raw WS fields to APCUpdate.sensor in-place (idempotent)."""
    # Mode (roh, 1:1 aus API)
    if getattr(sensor_obj, "mode_raw", None) is None:
        setattr(sensor_obj, "mode_raw", _dig(raw, ["payload", "state", "state", "mode"]))

    # Timer-Rohwerte
    timer = _dig(raw, ["payload", "state", "nodes", "timer"], {}) or {}
    if getattr(sensor_obj, "timer_initial", None) is None:
        setattr(sensor_obj, "timer_initial", timer.get("initial"))
    if getattr(sensor_obj, "timer_mode", None) is None:
        setattr(sensor_obj, "timer_mode", timer.get("mode"))
    if getattr(sensor_obj, "timer_started_at", None) is None:
        setattr(sensor_obj, "timer_started_at", timer.get("startedAtTimestamp"))

    # Low-Water
    loww = _dig(raw, ["payload", "state", "nodes", "lowWater"], {}) or {}
    if getattr(sensor_obj, "low_water_warning", None) is None:
        setattr(sensor_obj, "low_water_warning", loww.get("warning"))
    if getattr(sensor_obj, "low_water_empty", None) is None:
        setattr(sensor_obj, "low_water_empty", loww.get("empty"))

    # Diagnostics
    sysi = _dig(raw, ["payload", "state", "systemInfo"], {}) or {}
    if getattr(sensor_obj, "firmware_version", None) is None:
        setattr(sensor_obj, "firmware_version", sysi.get("firmwareVersion"))
    if getattr(sensor_obj, "hardware_version", None) is None:
        setattr(sensor_obj, "hardware_version", sysi.get("hardwareVersion"))
    if getattr(sensor_obj, "online", None) is None:
        setattr(sensor_obj, "online", sysi.get("online"))


class AnovaCoordinator(DataUpdateCoordinator[APCUpdate]):
    """Anova custom coordinator."""

    config_entry: AnovaConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: AnovaConfigEntry,
        anova_device: APCWifiDevice,
    ) -> None:
        """Set up Anova Coordinator."""
        super().__init__(
            hass,
            config_entry=config_entry,
            name="Anova Precision Cooker",
            logger=_LOGGER,
        )
        self.device_unique_id = anova_device.cooker_id
        self.anova_device = anova_device
        # Statt direkt async_set_updated_data → eigener Handler,
        # damit wir Raw-Werte anreichern können.
        self.anova_device.set_update_listener(self._handle_update)
        self.device_info: DeviceInfo | None = None

        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, self.device_unique_id)},
            name="Anova Precision Cooker",
            manufacturer="Anova",
            model="Precision Cooker",
        )
        self.sensor_data_set: bool = False
        
        # Timer countdown state
        self._timer_initial: int = 0  # seconds
        self._timer_started_at: datetime | None = None
        self._timer_mode: str = "idle"
        self._countdown_unsub: Any = None
        
    def get_cook_time_remaining(self) -> int | None:
        """Calculate current remaining time in seconds."""
        if self._timer_mode != "running" or not self._timer_started_at or self._timer_initial <= 0:
            return None
        elapsed = (datetime.now(timezone.utc) - self._timer_started_at).total_seconds()
        remaining = max(0, int(self._timer_initial - elapsed))
        return remaining if remaining > 0 else None
    
    @callback
    def _countdown_tick(self, _now: datetime) -> None:
        """Called every second to update countdown."""
        if self._timer_mode == "running" and self.data is not None:
            # Trigger sensor update
            self.async_set_updated_data(self.data)
    
    async def _async_start_countdown(self) -> None:
        """Start the countdown interval (must be called from event loop)."""
        if self._countdown_unsub is None:
            from datetime import timedelta
            self._countdown_unsub = async_track_time_interval(
                self.hass, self._countdown_tick, timedelta(seconds=1)
            )
            _LOGGER.debug("Started countdown timer")
    
    async def _async_stop_countdown(self) -> None:
        """Stop the countdown interval (must be called from event loop)."""
        if self._countdown_unsub is not None:
            self._countdown_unsub()
            self._countdown_unsub = None
            _LOGGER.debug("Stopped countdown timer")

    def _handle_update(self, update: APCUpdate) -> None:
        """Receive device update, enrich sensor with raw payload, propagate.
        
        NOTE: This must be a sync function because anova_wifi calls update_listeners
        without await. We schedule the async work via asyncio.create_task().
        """
        _LOGGER.debug("Anova _handle_update called! update=%r", update)
        try:
            # Roh-Payload bestmöglich beschaffen
            raw: Optional[dict] = None

            # 1) Einige anova_wifi-Versionen hängen den letzten WS-Frame ans Device
            for attr in ("last_raw_message", "last_message", "last_state", "raw_state", "raw"):
                raw = raw or getattr(self.anova_device, attr, None)

            # 2) Manche hängen Rohdaten direkt ans Update
            for attr in ("raw_message", "raw", "payload", "message"):
                cand = getattr(update, attr, None)
                if isinstance(cand, dict):
                    raw = cand
                    break

            # 3) Fallback: bekannte Struktur unter update.state_dict (wenn vorhanden)
            cand = getattr(update, "state_dict", None)
            if isinstance(cand, dict) and "payload" in cand:
                raw = cand

            if isinstance(raw, dict) and getattr(update, "sensor", None) is not None:
                # Attach the raw dict directly so sensor.py can access it via _get(d, ["raw", ...])
                setattr(update.sensor, "raw", raw)
                _enrich_sensor_from_raw(update.sensor, raw)
                
                # Extract timer state for countdown
                timer = _dig(raw, ["payload", "state", "nodes", "timer"], {}) or {}
                new_mode = timer.get("mode", "idle")
                new_initial = int(timer.get("initial", 0))
                new_started = timer.get("startedAtTimestamp")
                
                # Update timer state
                self._timer_initial = new_initial
                self._timer_mode = new_mode
                if new_started:
                    try:
                        started_str = new_started.replace("Z", "+00:00")
                        self._timer_started_at = datetime.fromisoformat(started_str)
                    except Exception:
                        self._timer_started_at = None
                else:
                    self._timer_started_at = None
                
                # Start/stop countdown based on timer mode
                if new_mode == "running" and new_initial > 0 and self._timer_started_at:
                    self.hass.loop.call_soon_threadsafe(
                        self.hass.async_create_task,
                        self._async_start_countdown()
                    )
                else:
                    self.hass.loop.call_soon_threadsafe(
                        self.hass.async_create_task,
                        self._async_stop_countdown()
                    )
            else:
                _LOGGER.debug("Anova: no raw payload available for enrichment (ok).")

        except Exception as exc:  # defensiv — Enrichment darf niemals den Update-Flow brechen
            _LOGGER.debug("Anova enrich failed: %r", exc)

        # Update weiterreichen — schedule async call from sync context (thread-safe)
        _LOGGER.debug("Anova scheduling async_set_updated_data, sensor.raw=%r", getattr(getattr(update, 'sensor', None), 'raw', 'NO_SENSOR'))
        self.hass.loop.call_soon_threadsafe(
            self.hass.async_create_task,
            self.async_set_updated_data(update)
        )
