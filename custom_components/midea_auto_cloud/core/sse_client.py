"""
美的美居 SSE 实时状态推送客户端

实现设备状态实时推送接收，替代轮询机制。
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from typing import Callable, Dict, Optional

from aiohttp import ClientSession, ClientTimeout
from Crypto.Cipher import AES

_LOGGER = logging.getLogger(__name__)

# SSE 配置
SSE_URL = "https://sse-ali.smartmidea.net/v2/sse/access"
SSE_APP_KEY = "ad0ee21d48a64bf49f4fb583ab76e799"
SSE_DECRYPT_KEY = b"a1a971846865c9a6"  # AES-128-ECB


class MideaSSEClient:
    """美的美居 SSE 客户端"""

    def __init__(
        self,
        session: ClientSession,
        access_token: str,
        uid: str,
    ):
        self._session = session
        self._access_token = access_token
        self._uid = uid
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._offset = "0"
        self._device_callbacks: Dict[str, Callable] = {}
        self._reconnect_delay = 5

    def register_device_callback(
        self, device_id: str, callback: Callable[[dict], None]
    ) -> None:
        """注册设备状态回调"""
        self._device_callbacks[str(device_id)] = callback
        _LOGGER.debug(f"SSE: Registered callback for device {device_id}")

    def unregister_device_callback(self, device_id: str) -> None:
        """取消注册设备回调"""
        self._device_callbacks.pop(str(device_id), None)

    def _generate_sign(self, query_string: str) -> str:
        """生成 SSE 请求签名"""
        sign_str = f"/v2/sse/access?{query_string}&{SSE_APP_KEY}"
        return hashlib.md5(sign_str.encode()).hexdigest()

    def _build_params(self) -> dict:
        """构建 SSE 请求参数"""
        timestamp = time.strftime("%Y%m%d%H%M%S")
        req_id = uuid.uuid4().hex
        device_id = hashlib.md5(self._uid.encode()).hexdigest()[:16]

        params_list = [
            ("reset", "2"),
            ("offset", self._offset),
            ("client_type", "1"),
            ("src_token", "1000"),
            ("req", req_id),
            ("token", self._access_token),
            ("device_id", device_id),
            ("appid", "900"),
            ("version", "11.3.10.5"),
            ("timestamp", timestamp),
        ]

        query_string = "&".join(f"{k}={v}" for k, v in params_list)
        sign = self._generate_sign(query_string)
        params_list.append(("sign", sign))

        return dict(params_list)

    def _decrypt_msg(self, encrypted_hex: str) -> Optional[bytes]:
        """解密设备状态消息"""
        try:
            if len(encrypted_hex) % 2 != 0:
                encrypted_hex += "0"

            enc_data = bytes.fromhex(encrypted_hex)
            cipher = AES.new(SSE_DECRYPT_KEY, AES.MODE_ECB)
            decrypted = cipher.decrypt(enc_data)

            # PKCS5/PKCS7 去填充
            pad = decrypted[-1]
            if 1 <= pad <= 16:
                decrypted = decrypted[:-pad]

            return decrypted
        except Exception as e:
            _LOGGER.debug(f"SSE: Decrypt failed: {e}")
            return None

    def _parse_device_status(self, bytes_list: list) -> dict:
        """解析设备状态数据

        美的设备 SSE 推送的状态数据为逗号分隔的整数列表，包含：
        - 帧头固定字段（位置 0~42）
        - TLV 属性区（位置 43+，以 0xFF 作为属性标记）
        """
        result = {"temperatures": {}, "status": {}, "raw_properties": {}}

        if len(bytes_list) < 43:
            return result

        # 解析 TLV 属性 (可靠，有明确 prop_id 和 length)
        props = {}
        i = 43
        while i < len(bytes_list) - 2:
            if bytes_list[i] == 255:
                prop_id = bytes_list[i + 1]
                length = bytes_list[i + 2]
                if i + 3 + length <= len(bytes_list):
                    value = bytes_list[i + 3 : i + 3 + length]
                    props[prop_id] = value[0] if len(value) == 1 else value
                    i += 3 + length
                    continue
            i += 1

        result["raw_properties"] = props

        _LOGGER.debug(f"SSE: TLV props found: {props}")

        # 电源状态: bytes[42] (0=关, 1=开)
        result["status"]["power"] = bytes_list[42] if len(bytes_list) > 42 else 0

        # 提取温度
        # 室内温度: TLV prop 66 第2字节 / 10 (已验证与 API temperature.room 匹配)
        if 66 in props and isinstance(props[66], list) and len(props[66]) >= 2:
            indoor = props[66][1] / 10
            if -10 <= indoor <= 55:
                result["temperatures"]["indoor"] = indoor

        # 室外温度: TLV prop 10 (value / 10)
        if 10 in props:
            outdoor = props[10] / 10
            if -40 <= outdoor <= 60:
                result["temperatures"]["outdoor"] = outdoor

        # 设定温度: TLV prop 6 / 2 - 40
        # 验证: 140→30°C, 138→29°C, 136→28°C
        if 6 in props and isinstance(props[6], int):
            target = props[6] / 2 - 40
            if 16 <= target <= 32:
                result["temperatures"]["target"] = target

        # 4. 提取运行状态
        # 运行模式: TLV prop 3 (1=fan_only, 2=cool, 3=heat, 4=auto, 6=dry)
        mode_map = {1: "fan_only", 2: "cool", 3: "heat", 4: "auto", 6: "dry"}
        if 3 in props:
            result["status"]["mode"] = mode_map.get(props[3], None)

        # 风速: TLV prop 2 第4字节 (1-7档, 102=auto)
        if 2 in props and isinstance(props[2], list) and len(props[2]) >= 4:
            fan_speed = props[2][3]
            if fan_speed == 102:
                result["status"]["fan"] = "auto"
            elif 1 <= fan_speed <= 7:
                result["status"]["fan"] = str(fan_speed)

        return result

    def _parse_sse_message(self, line: str) -> Optional[dict]:
        """解析 SSE 消息"""
        if not line.startswith("data:"):
            return None

        try:
            data = json.loads(line[5:].strip())
            event_type = data.get("event_type")

            result = {
                "id": data.get("id"),
                "timestamp": data.get("timestamp"),
                "event_type": event_type,
            }

            if event_type == 1:
                # 心跳
                result["type"] = "heartbeat"
            elif event_type == 2:
                # 业务消息
                result["type"] = "business"
                inner = json.loads(data.get("data", "{}"))
                message = inner.get("message", "")

                # 解析消息格式: pushType;uid;payload;time
                parts = message.split(";")
                if len(parts) >= 3:
                    result["push_type"] = parts[0]
                    try:
                        result["payload"] = json.loads(parts[2])
                    except:
                        result["payload"] = parts[2]

            return result
        except json.JSONDecodeError:
            return None

    async def _handle_status_report(self, payload: dict) -> None:
        """处理设备状态报告"""
        device_id = str(payload.get("applianceId", ""))
        encrypted_msg = payload.get("msg", "")

        if not device_id or not encrypted_msg:
            return

        # 解密
        decrypted = self._decrypt_msg(encrypted_msg)
        if not decrypted:
            return

        try:
            text = decrypted.decode("utf-8")
            if "," not in text:
                return

            bytes_list = [int(x.strip()) for x in text.split(",") if x.strip()]

            # 日志记录原始字节（前50字节，用于调试解析问题）
            _LOGGER.debug(
                f"SSE: Device {device_id} raw bytes[0:50]={bytes_list[:50]} "
                f"(total={len(bytes_list)})"
            )

            status = self._parse_device_status(bytes_list)

            # 转换为 HA 兼容格式
            ha_status = self._convert_to_ha_format(status)

            if not ha_status:
                return

            _LOGGER.debug(f"SSE: Device {device_id} update -> {ha_status}")

            # 分发到注册的回调
            if device_id in self._device_callbacks:
                try:
                    self._device_callbacks[device_id](ha_status)
                except Exception as e:
                    import traceback
                    _LOGGER.error(f"SSE: Callback FAILED for {device_id}: {e}")
                    _LOGGER.debug(f"SSE: {traceback.format_exc()}")
            else:
                _LOGGER.debug(f"SSE: No callback for device {device_id}")
        except Exception as e:
            _LOGGER.debug(f"SSE: Parse status failed for {device_id}: {e}")

    def _convert_to_ha_format(self, status: dict) -> dict:
        """转换为 Home Assistant 属性格式

        同时写入 T0x21（中央空调）和 T0xAC（挂机/柜机 Lua）两套 key，
        确保不同设备类型的实体都能读到 SSE 推送的最新值。
        """
        ha_attrs = {}
        temps = status.get("temperatures", {})
        state = status.get("status", {})

        # ── 室内温度 ──
        if "indoor" in temps:
            indoor = temps["indoor"]
            ha_attrs["current_temperature"] = indoor
            ha_attrs["room_temp"] = indoor              # T0x21 default
            ha_attrs["room_temperature"] = indoor        # T0x21 sn8
            ha_attrs["indoor_temperature"] = indoor      # T0xAC
            ha_attrs["indoor_temp"] = indoor

        # ── 室外温度 ──
        if "outdoor" in temps:
            outdoor = temps["outdoor"]
            ha_attrs["outdoor_temperature"] = outdoor
            ha_attrs["outside_temperature"] = outdoor
            ha_attrs["outdoor_temp"] = outdoor

        # ── 设定温度 ──
        if "target" in temps:
            target = temps["target"]
            ha_attrs["target_temperature"] = target
            ha_attrs["cool_temp_set"] = target           # T0x21
            ha_attrs["heat_temp_set"] = target           # T0x21
            # T0xAC 用 [temperature, small_temperature] 组合读取设定温度
            ha_attrs["temperature"] = int(target)
            ha_attrs["small_temperature"] = round(target - int(target), 1)

        # ── 电源 ──
        power_on = state.get("power", 0) == 1
        ha_attrs["power"] = "on" if power_on else "off"

        # ── 运行模式 ──
        # 仅在 TLV 中实际找到 prop 3 时才输出 mode/run_mode
        # 未找到时不输出，避免用错误值覆盖 API 轮询的正确值
        mode_str = state.get("mode")  # None when prop 3 not found
        if mode_str is not None:
            # T0x21: run_mode 数值字符串 (0=off 1=fan 2=cool 3=heat 4=auto 5=dry)
            run_mode_map = {"fan_only": "1", "cool": "2", "heat": "3", "auto": "4", "dry": "5"}
            ha_attrs["run_mode"] = "0" if not power_on else run_mode_map.get(mode_str, "0")

            # T0xAC: mode 使用 Lua API 名称 ("fan" 而非 HA 的 "fan_only")
            lua_mode_map = {"cool": "cool", "fan_only": "fan", "heat": "heat", "auto": "auto", "dry": "dry"}
            ha_attrs["mode"] = lua_mode_map.get(mode_str, mode_str) if power_on else "off"

        # ── 风速 ──
        if "fan" in state:
            fan_val = state["fan"]
            # T0x21: fan_speed 字符串 "1"~"7", "8"=auto
            ha_attrs["fan_speed"] = "8" if fan_val == "auto" else fan_val

        return ha_attrs

    async def _sse_loop(self) -> None:
        """SSE 主循环"""
        headers = {
            "User-Agent": "okhttp/4.9.3",
            "Accept": "text/event-stream",
            "accesstoken": self._access_token,
            "Cache-Control": "no-cache",
        }

        while self._running:
            try:
                params = self._build_params()
                _LOGGER.debug(f"SSE: Connecting to {SSE_URL}...")

                timeout = ClientTimeout(total=None, sock_read=120)
                async with self._session.get(
                    SSE_URL, params=params, headers=headers, timeout=timeout
                ) as response:
                    if response.status != 200:
                        _LOGGER.warning(f"SSE: Connection failed: HTTP {response.status}")
                        await asyncio.sleep(self._reconnect_delay)
                        continue

                    _LOGGER.info("SSE: Connected successfully")

                    async for line in response.content:
                        if not self._running:
                            break

                        line_str = line.decode("utf-8").strip()
                        if not line_str:
                            continue

                        msg = self._parse_sse_message(line_str)
                        if not msg:
                            continue

                        # 更新 offset
                        if msg.get("id"):
                            self._offset = str(msg["id"])

                        # 处理消息
                        if msg.get("type") == "heartbeat":
                            _LOGGER.debug("SSE: Heartbeat received")
                        elif msg.get("type") == "business":
                            push_type = msg.get("push_type", "")
                            payload = msg.get("payload", {})

                            if "status/report" in push_type:
                                await self._handle_status_report(payload)
                            elif "online/status/on" in push_type:
                                device_id = str(payload.get("applianceId", ""))
                                if device_id in self._device_callbacks:
                                    self._device_callbacks[device_id]({"online": True})
                            elif "online/status/off" in push_type:
                                device_id = str(payload.get("applianceId", ""))
                                if device_id in self._device_callbacks:
                                    self._device_callbacks[device_id]({"online": False})

            except asyncio.CancelledError:
                _LOGGER.debug("SSE: Loop cancelled")
                break
            except Exception as e:
                import traceback
                _LOGGER.debug(f"SSE: Connection error: {e}")
                _LOGGER.debug(f"SSE: Traceback: {traceback.format_exc()}")
                if self._running:
                    await asyncio.sleep(self._reconnect_delay)

    async def start(self) -> None:
        """启动 SSE 监听"""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._sse_loop())
        _LOGGER.info("SSE: Listener task started")

    async def stop(self) -> None:
        """停止 SSE 监听"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        _LOGGER.info("SSE: Listener stopped")

    @property
    def is_running(self) -> bool:
        """返回 SSE 是否正在运行"""
        return self._running
