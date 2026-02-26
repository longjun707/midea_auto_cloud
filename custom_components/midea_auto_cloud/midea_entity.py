"""Base entity class for Midea Auto Cloud integration."""

from __future__ import annotations

import logging
from enum import IntEnum
from typing import Any

from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.core import callback

from .const import DOMAIN
from .core.logger import MideaLogger
from .data_coordinator import MideaDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

class Rationale(IntEnum):
    EQUALLY = 0
    GREATER = 1
    LESS = 2

class MideaEntity(CoordinatorEntity[MideaDataUpdateCoordinator], Entity):
    """Base class for Midea entities."""

    def __init__(
        self,
        coordinator: MideaDataUpdateCoordinator,
        device_id: int,
        device_name: str,
        device_type: str,
        sn: str,
        sn8: str,
        model: str,
        entity_key: str,
        *,
        device: Any | None = None,
        manufacturer: str | None = None,
        rationale: list | None = None,
        config: dict | None = None,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._device_name = device_name
        self._device_type = device_type
        self._entity_key = entity_key
        self._sn = sn
        self._sn8 = sn8
        self._model = model
        # Legacy/extended fields
        self._device = device
        self._config = config or {}
        self._rationale = rationale
        if (self._config.get("rationale")) is not None:
            self._rationale = self._config.get("rationale")
        if self._rationale is None:
            self._rationale = ["off", "on"]
        
        # Display and identification
        self._attr_has_entity_name = True
        # Prefer legacy unique_id scheme if device object is available (device_id based)
        if self._device is not None:
            self._attr_unique_id = f"{DOMAIN}.{self._device_id}_{self._entity_key}"
            self.entity_id_base = f"midea_{self._device_id}"
            manu = "Midea" if manufacturer is None else manufacturer
            self.manufacturer = manu
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, str(self._device_id))},
                model=self._model,
                serial_number=sn,
                manufacturer=manu,
                name=device_name,
            )
            # Presentation attributes from config
            self._attr_native_unit_of_measurement = self._config.get("unit_of_measurement")
            self._attr_device_class = self._config.get("device_class")
            self._attr_state_class = self._config.get("state_class")
            self._attr_icon = self._config.get("icon")
            # Prefer translated name; allow explicit override via config.name
            self._attr_translation_key = self._config.get("translation_key") or self._entity_key
            name_cfg = self._config.get("name")
            if name_cfg is not None:
                self._attr_name = f"{name_cfg}"
            self.entity_id = self._attr_unique_id
            # Register device updates for HA state refresh
            try:
                self._device.register_update(self.update_state)  # type: ignore[attr-defined]
            except Exception as e:
                _LOGGER.debug("Failed to register device update callback: %s", e)
        else:
            # Fallback to sn8-based unique id/device info
            self._attr_unique_id = f"{sn8}_{self.entity_id_suffix}"
            self.entity_id_base = f"midea_{sn8.lower()}"
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, sn8)},
                model=model,
                serial_number=sn,
                manufacturer="Midea",
                name=device_name,
            )
        
        # Debounced command publishing
        self._debounced_publish_command = Debouncer(
            hass=self.coordinator.hass,
            logger=_LOGGER,
            cooldown=2,
            immediate=True,
            background=True,
            function=self._publish_command,
        )
        
        if self.coordinator.config_entry:
            self.coordinator.config_entry.async_on_unload(
                self._debounced_publish_command.async_shutdown
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def entity_id_suffix(self) -> str:
        """Return the suffix for entity ID."""
        return "base"

    @property
    def device_attributes(self) -> dict:
        """Return device attributes."""
        return self.coordinator.data.attributes if self.coordinator.data else {}

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if self.coordinator.data:
            return self.coordinator.data.available
        else:
            return False

    async def _publish_command(self) -> None:
        """Publish commands to the device."""
        # This will be implemented by subclasses
        pass

    # ===== Unified helpers migrated from legacy entity base =====
    def _get_nested_value(self, attribute_key: str | None) -> Any:
        """Get nested value from device attributes using dot notation.
        
        Supports both flat and nested attribute access.
        Examples: 'power', 'eco.status', 'temperature.room'
        
        When dot-notation navigation fails (e.g. parent key is None or not a dict),
        falls back to looking up the full dot-notation string as a flat key.
        This handles the case where Lua polling never succeeded to create nested dicts
        but the preset step created flat keys like 'temperature.room'.
        """
        if attribute_key is None:
            return None
        
        # Handle nested attributes with dot notation
        if '.' in attribute_key:
            keys = attribute_key.split('.')
            value = self.device_attributes
            try:
                for key in keys:
                    if isinstance(value, dict):
                        value = value.get(key)
                    else:
                        # Dot navigation failed; try flat key fallback
                        return self.device_attributes.get(attribute_key)
                if value is not None:
                    return value
                # Dot navigation returned None; try flat key fallback
                return self.device_attributes.get(attribute_key)
            except (KeyError, TypeError):
                return self.device_attributes.get(attribute_key)
        else:
            # Handle flat attributes
            value = self.device_attributes.get(attribute_key)
            # Auto-resolve Lua API nested dicts:
            # Lua codec returns e.g. mode={"current":"heat","supported":"..."}
            # but T0xAC entity config checks flat key  mode=="heat".
            # Auto-resolve to the "current" sub-key so the match works.
            if isinstance(value, dict) and "current" in value:
                return value["current"]
            return value

    def _get_status_on_off(self, attribute_key: str | None) -> bool:
        """Return boolean value from device attributes for given key.

        Accepts common truthy representations: True/1/"on"/"true".
        Supports nested attributes with dot notation.
        """
        result = False
        if attribute_key is None:
            return result
        status = self._get_nested_value(attribute_key)
        if status is not None:
            try:
                result = bool(self._rationale.index(status))
            except ValueError:
                if isinstance(status, int) or status in ['0', '1']:
                    if int(status) == 0:
                        result = False
                    else:
                        result = True
                else:
                    MideaLogger.warning(f"The value of attribute {attribute_key} ('{status}') "
                                      f"is not in rationale {self._rationale}")
                return result
        return result

    def _set_nested_value(self, attribute_key: str, value: Any) -> None:
        """Set nested value in device attributes using dot notation.
        
        Supports both flat and nested attribute setting.
        Examples: 'power', 'eco.status', 'temperature.room'
        """
        if attribute_key is None:
            return
        
        # Handle nested attributes with dot notation
        if '.' in attribute_key:
            keys = attribute_key.split('.')
            current_dict = self.device_attributes
            
            # Navigate to the parent dictionary
            for key in keys[:-1]:
                if key not in current_dict:
                    current_dict[key] = {}
                current_dict = current_dict[key]
            
            # Set the final value
            current_dict[keys[-1]] = value
        else:
            # Handle flat attributes
            self.device_attributes[attribute_key] = value

    async def _async_set_status_on_off(self, attribute_key: str | None, turn_on: bool) -> None:
        """Set boolean attribute via coordinator, no-op if key is None."""
        if attribute_key is None:
            return
        await self.async_set_attribute(attribute_key, self._rationale[int(turn_on)])

    @staticmethod
    def _values_equal(state_value: Any, config_value: Any) -> bool:
        """Compare state value with config value, with type coercion.

        Device attributes are often strings (from Lua codec or SSE), while mapping
        configs may use ints/floats. Example: wind_speed.level can be '3' vs 3.
        """
        if state_value == config_value:
            return True
        try:
            return float(state_value) == float(config_value)
        except (ValueError, TypeError):
            return False

    def _list_get_selected(self, key_of_list: list, rationale: Rationale = Rationale.EQUALLY):
        for index in range(0, len(key_of_list)):
            match = True
            for attr, value in key_of_list[index].items():
                state_value = self._get_nested_value(attr)
                if state_value is None:
                    match = False
                    break
                if rationale is Rationale.EQUALLY and not self._values_equal(state_value, value):
                    match = False
                    break
                if rationale is Rationale.GREATER:
                    try:
                        if float(state_value) < float(value):
                            match = False
                            break
                    except (ValueError, TypeError):
                        match = False
                        break
                if rationale is Rationale.LESS:
                    try:
                        if float(state_value) > float(value):
                            match = False
                            break
                    except (ValueError, TypeError):
                        match = False
                        break
            if match:
                return index
        return None

    def _dict_get_selected(self, key_of_dict: dict, rationale: Rationale = Rationale.EQUALLY):
        for mode, status in key_of_dict.items():
            match = True
            for attr, value in status.items():
                state_value = self._get_nested_value(attr)
                if state_value is None:
                    match = False
                    break
                if rationale is Rationale.EQUALLY and not self._values_equal(state_value, value):
                    match = False
                    break
                if rationale is Rationale.GREATER:
                    try:
                        if float(state_value) < float(value):
                            match = False
                            break
                    except (ValueError, TypeError):
                        match = False
                        break
                if rationale is Rationale.LESS:
                    try:
                        if float(state_value) > float(value):
                            match = False
                            break
                    except (ValueError, TypeError):
                        match = False
                        break
            if match:
                return mode
        return None

    async def publish_command_from_current_state(self) -> None:
        """Publish commands to the device from current state."""
        self.coordinator.mute_state_update_for_a_while()
        self.coordinator.async_update_listeners()
        await self._debounced_publish_command.async_call()

    async def async_set_attribute(self, attribute: str, value: Any) -> None:
        """Set a device attribute."""
        await self.coordinator.async_set_attribute(attribute, value)

    async def async_set_attributes(self, attributes: dict) -> None:
        """Set multiple device attributes."""
        await self.coordinator.async_set_attributes(attributes)

    async def async_send_command(self, cmd_type: int, cmd_body: str) -> None:
        """Send a command to the device."""
        await self.coordinator.async_send_command(cmd_type, cmd_body)
