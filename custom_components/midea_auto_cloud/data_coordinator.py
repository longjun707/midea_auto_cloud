"""Data coordinator for Midea Auto Cloud integration."""

import logging
import traceback
from datetime import datetime, timedelta
from typing import NamedTuple

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .core.device import MiedaDevice
from .core.logger import MideaLogger

_LOGGER = logging.getLogger(__name__)


class MideaDeviceData(NamedTuple):
    """Data structure for Midea device state."""
    attributes: dict
    available: bool
    connected: bool


class MideaDataUpdateCoordinator(DataUpdateCoordinator[MideaDeviceData]):
    """Data update coordinator for Midea devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        device: MiedaDevice,
        cloud=None,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{device.device_name} ({device.device_id})",
            update_method=self.poll_device_state,
            update_interval=timedelta(seconds=90),
            always_update=True,
        )
        self.device = device
        self.state_update_muted: CALLBACK_TYPE | None = None
        self._device_id = device.device_id
        self._cloud = cloud

    async def _async_setup(self) -> None:
        """Set up the coordinator."""
        # Immediate first refresh to avoid waiting for the interval
        self.data = await self.poll_device_state()
        
        # Register for device updates
        self.device.register_update(self._device_update_callback)

    def mute_state_update_for_a_while(self) -> None:
        """Mute subscription for a while to avoid state bouncing.
        
        NOTE: Disabled - SSE real-time updates should take precedence.
        """
        pass  # Disabled to allow SSE updates to always propagate

    def _device_update_callback(self, status: dict) -> None:
        """Callback for device status updates (from polling)."""
        if self.state_update_muted:
            return
        
        # Update device attributes (allow new keys to be added)
        for key, value in status.items():
            self.device.attributes[key] = value
        
        # Update coordinator data
        self.async_set_updated_data(
            MideaDeviceData(
                attributes=self.device.attributes,
                available=self.device.connected,
                connected=self.device.connected,
            )
        )

    # SSE 温度值合理范围（收紧设定温度范围以过滤解析错误）
    _TEMP_RANGES = {
        "target_temperature": (15.0, 35.0),
        "cool_temp_set": (15.0, 35.0),
        "heat_temp_set": (15.0, 35.0),
        "temperature": (15.0, 35.0),
        "current_temperature": (-10.0, 55.0),
        "room_temp": (-10.0, 55.0),
        "room_temperature": (-10.0, 55.0),
        "indoor_temperature": (-10.0, 55.0),
        "indoor_temp": (-10.0, 55.0),
        "outdoor_temperature": (-30.0, 55.0),
        "outside_temperature": (-30.0, 55.0),
        "outdoor_temp": (-30.0, 55.0),
    }

    def _validate_sse_status(self, status: dict) -> dict:
        """Validate SSE status data, filtering out obviously wrong temperature values."""
        validated = {}
        for key, value in status.items():
            if key in self._TEMP_RANGES and isinstance(value, (int, float)):
                lo, hi = self._TEMP_RANGES[key]
                if not (lo <= value <= hi):
                    _LOGGER.debug(
                        f"SSE CB: Device {self._device_id} rejected {key}={value} "
                        f"(out of range {lo}~{hi})"
                    )
                    continue
            validated[key] = value
        return validated

    def _sse_update_callback(self, status: dict) -> None:
        """Callback for SSE real-time status updates."""
        if self.state_update_muted:
            return

        # Validate temperature values before applying
        status = self._validate_sse_status(status)
        if not status:
            return

        # 1. Write flat keys
        # Lua codec devices store nested dicts like {"temperature": {"room": 14.2}},
        # but SSE sends flat keys like {"temperature": 20}.
        # Overwriting the dict with a scalar would break dot-notation entity reads.
        skipped = []
        for key, value in status.items():
            existing = self.device.attributes.get(key)
            if isinstance(existing, dict) and not isinstance(value, dict):
                # Skip: don't overwrite nested dict with flat value;
                # _update_nested_attrs() will handle updating the sub-keys.
                skipped.append(key)
                continue
            elif isinstance(value, dict) and isinstance(existing, dict):
                existing.update(value)
            else:
                self.device.attributes[key] = value
        if skipped:
            _LOGGER.debug(f"SSE CB: Device {self._device_id} skipped dict keys: {skipped}")

        # 2. Update nested dict structures (used by Lua codec devices)
        # Lua codec stores attributes like {"temperature": {"room": 14.2, "current": 25.0}}
        # Entity config reads via dot notation e.g. "temperature.room"
        # SSE data has flat keys, so we must also update nested dicts
        self._update_nested_attrs(status)

        self.async_set_updated_data(
            MideaDeviceData(
                attributes=self.device.attributes,
                available=True,
                connected=True,
            )
        )

    def _update_nested_attrs(self, status: dict) -> None:
        """Update nested dict attributes from SSE flat data.

        Maps SSE flat keys to nested dict structures that Lua codec devices use.
        Lua codec devices store e.g.:
          temperature: {room: "23.2", current: "22.5"}
          mode: {current: "heat"}
          wind_speed: {level: "7"}
        Entity config reads via dot notation e.g. "temperature.room".

        Also writes dot-notation flat keys (e.g. "temperature.room") as a fallback
        for when nested dicts don't exist (Lua polling never succeeded).
        """
        attrs = self.device.attributes

        # Extract SSE values
        indoor = status.get("room_temp") or status.get("room_temperature") or status.get("indoor_temperature")
        outdoor = status.get("outdoor_temperature") or status.get("outside_temperature")
        target = status.get("cool_temp_set") or status.get("target_temperature")
        mode = status.get("mode")           # Lua mode string: "heat"/"cool"/"fan"/...
        power = status.get("power")         # "on"/"off" from SSE
        fan_speed = status.get("fan_speed")  # "1"~"8" from SSE

        # Write flat keys for hvac_modes matching (T0xAC config checks "power" and "mode")
        # These must be written even if nested dicts exist, because _dict_get_selected
        # checks top-level keys like attrs["mode"], not nested attrs["mode"]["current"]
        if mode is not None:
            attrs["mode"] = mode  # Overwrite even if nested dict exists
        if power is not None:
            attrs["power"] = power  # Overwrite even if nested dict exists

        # Write dot-notation flat keys as fallback for when nested dicts don't exist.
        # Entity config uses dot-notation keys like "temperature.room", "mode.current".
        # _get_nested_value falls back to flat key lookup when navigation fails.
        if indoor is not None:
            attrs["temperature.room"] = indoor
        if outdoor is not None:
            attrs["temperature.outside"] = outdoor
        if target is not None:
            attrs["temperature.current"] = target
        if mode is not None:
            attrs["mode.current"] = mode
        if fan_speed is not None:
            try:
                attrs["wind_speed.level"] = int(fan_speed)
            except (ValueError, TypeError):
                attrs["wind_speed.level"] = fan_speed

        # Scan existing nested dicts and update matching sub-keys
        for key, val in attrs.items():
            if not isinstance(val, dict):
                continue

            updated = False
            for sub_key in list(val.keys()):
                # Indoor temperature: only in temperature-related dicts
                if indoor is not None and sub_key in ("room", "indoor", "indoor_temp"):
                    if "temp" in key.lower() or key == "temperature":
                        val[sub_key] = indoor
                        updated = True
                # Outdoor temperature: only in temperature-related dicts
                elif outdoor is not None and sub_key in ("outside", "outdoor", "outdoor_temp"):
                    if "temp" in key.lower() or key == "temperature":
                        val[sub_key] = outdoor
                        updated = True
                # Target temperature: sub-keys like "current" in temperature dict
                elif target is not None and sub_key in ("current", "target", "set"):
                    if "temp" in key.lower() or key == "temperature":
                        val[sub_key] = target
                        updated = True
                # Mode: update mode.current
                elif mode is not None and key == "mode" and sub_key == "current":
                    val[sub_key] = mode
                    updated = True
                # Wind speed: update wind_speed.level
                elif fan_speed is not None and key == "wind_speed" and sub_key == "level":
                    val[sub_key] = fan_speed
                    updated = True

            if updated:
                _LOGGER.debug(f"SSE CB: Device {self._device_id} updated nested dict '{key}' -> {val}")

    async def poll_device_state(self) -> MideaDeviceData:
        """Poll device state."""
        if self.state_update_muted:
            if self.data is not None:
                return self.data
            return MideaDeviceData(
                attributes=self.device.attributes,
                available=self.device.connected,
                connected=self.device.connected,
            )

        try:
            # 检查是否为中央空调设备（T0x21）
            if self.device.device_type == 0x21:
                await self._poll_central_ac_state()
            else:
                await self.device.refresh_status()

            # 返回当前状态（DataUpdateCoordinator 会自动将返回值设为 data）
            return MideaDeviceData(
                attributes=self.device.attributes,
                available=self.device.connected,
                connected=self.device.connected,
            )
        except Exception as e:
            _LOGGER.error(f"Error polling device state: {e}")
            return MideaDeviceData(
                attributes=self.device.attributes,
                available=False,
                connected=False,
            )
    
    async def _poll_central_ac_state(self) -> None:
        """轮询中央空调状态"""
        try:
            cloud = self._cloud
            if cloud and hasattr(cloud, "get_central_ac_status"):
                status_data = await cloud.get_central_ac_status([self._device_id])
                if status_data and "appliances" in status_data:
                    # 找到对应的设备数据并更新到设备属性中
                    for appliance in status_data["appliances"]:
                        if appliance.get("type") == "0x21" and "extraData" in appliance:
                            extra_data = appliance["extraData"]
                            if "attr" in extra_data:
                                if "nodeid" in extra_data["attr"]:
                                    self.device.attributes["nodeid"] = extra_data["attr"]["nodeid"]
                                if "masterId" in extra_data["attr"]:
                                    self.device.attributes["masterId"] = extra_data["attr"]["masterId"]
                                if "modelid" in extra_data["attr"]:
                                    self.device.attributes["modelid"] = extra_data["attr"]["modelid"]
                                if "idType" in extra_data["attr"]:
                                    self.device.attributes["idType"] = extra_data["attr"]["idType"]

                                if "state" in extra_data["attr"] and "condition_attribute" in extra_data["attr"]["state"]:
                                    state = extra_data["attr"]["state"]
                                    condition = state["condition_attribute"]
                                    # 将状态数据更新到设备属性中
                                    for key, value in condition.items():
                                        # 尝试将数字字符串转换为数字
                                        if key.find("temp") > -1 and isinstance(value, str):
                                            try:
                                                # 尝试转换为整数
                                                if '.' not in value:
                                                    self.device.attributes[key] = int(value)
                                                else:
                                                    # 尝试转换为浮点数
                                                    self.device.attributes[key] = float(value)
                                            except (ValueError, TypeError):
                                                # 如果转换失败，保持原值
                                                self.device.attributes[key] = value
                                        else:
                                            self.device.attributes[key] = value

                                if "endlist" in extra_data["attr"]:
                                    endlist = extra_data["attr"]["endlist"]
                                    # endlist是一个数组，包含多个endpoint对象
                                    if isinstance(endlist, list):
                                        for endpoint in endlist:
                                            if "event" in endpoint:
                                                event = endpoint["event"]
                                                endpoint_id = endpoint.get("endpoint", 1)
                                                endpoint_name = endpoint.get("name", f"按键{endpoint_id}")
                                                
                                                # 为每个endpoint创建独立的状态属性
                                                for key, value in event.items():
                                                    # 创建带endpoint标识的属性名
                                                    attr_key = f"endpoint_{endpoint_id}_{key}"
                                                    attr_name_key = f"endpoint_{endpoint_id}_name"
                                                    
                                                    # 保存endpoint名称
                                                    self.device.attributes[attr_name_key] = endpoint_name
                                                    self.device.attributes[attr_key] = value
                                                
                                                # 同时保持原有的属性名（用于兼容性）
                                                for key, value in event.items():
                                                    # 尝试将数字字符串转换为数字
                                                    self.device.attributes[key] = value

                                break
        except Exception as e:
            MideaLogger.debug(f"Error polling central AC state: {e}")

    async def async_set_attribute(self, attribute: str, value) -> None:
        """Set a device attribute."""
        attributes = {}
        attributes[attribute] = value
        await self.async_set_attributes(attributes)

    async def async_set_attributes(self, attributes: dict) -> None:
        """Set multiple device attributes."""
        # 云端控制：构造 control 与 status（携带当前状态作为上下文）
        for c in self.device._calculate_set:
            lvalue = c.get("lvalue")
            rvalue = c.get("rvalue")
            if lvalue and rvalue:
                calculate = False
                for s, v in attributes.items():
                    if rvalue.find(f"[{s}]") >= 0:
                        calculate = True
                        break
                if calculate:
                    calculate_str1 = \
                        (f"{lvalue.replace('[', 'attributes[').replace("]", "\"]")} = "
                         f"{rvalue.replace('[', 'attributes[').replace(']', "\"]")}") \
                            .replace("[", "[\"")
                    try:
                        exec(calculate_str1)
                    except Exception as e:
                        MideaLogger.debug(
                            f"Calculation Error: {lvalue} = {rvalue}, calculate_str1: {calculate_str1}, {traceback.format_exc()}",
                            self._device_id
                        )
        await self.device.set_attributes(attributes)
        self.device.attributes.update(attributes)
        self.mute_state_update_for_a_while()
        self.async_update_listeners()

    async def async_send_command(self, cmd_type: int, cmd_body: str) -> None:
        """Send a command to the device."""
        try:
            cmd_body_bytes = bytearray.fromhex(cmd_body)
            await self.device.send_command(cmd_type, cmd_body_bytes)
        except ValueError as e:
            _LOGGER.error(f"Invalid command body: {e}")
            raise

    async def async_send_central_ac_control(self, control: dict) -> bool:
        """发送中央空调控制命令"""
        try:
            cloud = self._cloud
            if cloud and hasattr(cloud, "send_central_ac_control"):
                # 从设备属性中获取nodeid
                masterid = self.device.attributes.get("masterId")
                nodeid = self.device.attributes.get("nodeid")
                modelid = self.device.attributes.get("modelid")
                raw_idtype = self.device.attributes.get("idType")
                try:
                    idtype = int(raw_idtype) if raw_idtype is not None else 0
                except (ValueError, TypeError):
                    idtype = 0

                if not nodeid:
                    MideaLogger.warning(f"No nodeid found for central AC device {self._device_id}")
                    return False
                
                # 构建完整的控制命令，包含centralized中的所有字段
                full_control = self._build_full_central_ac_control(control)
                MideaLogger.debug(f"Sending control to {self.device.device_name}: {full_control}")
                success = await cloud.send_central_ac_control(
                    masterid,
                    nodeid,
                    modelid,
                    idtype,
                    full_control
                )

                if success:
                    # 更新本地状态
                    self.device.attributes.update(control)
                    self.mute_state_update_for_a_while()
                    self.async_update_listeners()
                    return True
                else:
                    MideaLogger.debug(f"Failed to send control to {self.device.device_name}")
                    return False
            else:
                MideaLogger.debug("Cloud service not available for central AC control")
                return False
        except Exception as e:
            MideaLogger.debug(f"Error sending control to {self.device.device_name}: {e}")
            return False

    async def async_send_switch_control(self, control: dict) -> bool:
        """发送开关控制命令（subtype为00000000的设备）"""
        try:
            cloud = self._cloud
            if cloud and hasattr(cloud, "send_switch_control"):
                # 获取设备ID和nodeId
                masterid = str(self.device.attributes.get("masterId"))
                nodeid = str(self.device.attributes.get("nodeid"))
                
                if not nodeid:
                    MideaLogger.warning(f"No nodeid found for switch device {self._device_id}")
                    return False
                
                # 根据控制命令确定endPoint和attribute值
                end_point = control.get("endpoint", 1)  # 从control中获取endpoint，默认1
                attribute = 0  # 默认attribute
                
                # 根据control内容设置attribute值
                if "run_mode" in control:
                    if control["run_mode"] == "1":
                        attribute = 1  # 开启
                    else:
                        attribute = 0  # 关闭
                
                # 构建控制数据
                switch_control = {
                    "endPoint": end_point,
                    "attribute": attribute
                }
                
                MideaLogger.debug(f"Sending switch control to {self.device.device_name}: {switch_control}")
                success = await cloud.send_switch_control(masterid, nodeid, switch_control)
                
                if success:
                    # 更新本地状态 - 使用类似poll_central的解析方法
                    await self._update_switch_status_from_control(control)
                    self.mute_state_update_for_a_while()
                    self.async_update_listeners()
                    return True
                else:
                    MideaLogger.debug(f"Failed to send switch control to {self.device.device_name}")
                    return False
            else:
                MideaLogger.debug("Cloud service not available for switch control")
                return False
        except Exception as e:
            MideaLogger.debug(f"Error sending switch control to {self.device.device_name}: {e}")
            return False

    async def _update_switch_status_from_control(self, control: dict) -> None:
        """根据控制命令更新开关状态，参照poll_central的解析方法"""
        try:
            # 获取endpoint ID
            endpoint_id = control.get("endpoint", 1)
            run_mode = control.get("run_mode", "0")
            
            # 模拟endlist数据结构来更新状态
            # 根据run_mode设置OnOff状态
            onoff_value = "1" if run_mode == "1" else "0"
            
            # 更新endpoint特定的状态属性
            attr_key = f"endpoint_{endpoint_id}_OnOff"
            self.device.attributes[attr_key] = onoff_value
            
            # 同时更新兼容性属性
            self.device.attributes["OnOff"] = onoff_value
            
            MideaLogger.debug(f"Updated switch status for endpoint {endpoint_id}: OnOff={onoff_value}")
            
        except Exception as e:
            MideaLogger.debug(f"Error updating switch status from control: {e}")

    def _build_full_central_ac_control(self, new_control: dict) -> dict:
        """构建完整控制命令"""
        full_control = {}
        full_control["run_mode"] = self.device.attributes.get("run_mode")
        full_control["cooling_temp"] = str(self.device.attributes.get("cool_temp_set") or 26.0)
        full_control["heating_temp"] = str(self.device.attributes.get("heat_temp_set") or 20.0)
        full_control["fan_speed"] = self.device.attributes.get("fan_speed")
        swing_mode = self.device.attributes.get("is_swing")
        is_elec_heat = self.device.attributes.get("is_elec_heat")

        if swing_mode == "1":
            # 开启摆风：如果当前有电辅热(2)，则设为6(电辅热+摆风)，否则设为4(摆风)
            if is_elec_heat == "1":
                new_extflag = "6"  # 电辅热+摆风
            else:
                new_extflag = "4"  # 仅摆风
        else:
            # 关闭摆风：如果当前是6(电辅热+摆风)，则设为2(仅电辅热)，否则设为0(关闭)
            if is_elec_heat == "1":
                new_extflag = "2"  # 仅电辅热
            else:
                new_extflag = "0"  # 关闭

        full_control["extflag"] = new_extflag

        # 然后用新的控制值覆盖
        full_control.update(new_control)
        return full_control
