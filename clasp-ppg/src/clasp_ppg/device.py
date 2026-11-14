from __future__ import annotations

import asyncio
import re
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

from .models import ACCFrame, Capture, PPGFrame


class SensorStatus:
    def __init__(self) -> None:
        self.connected = False
        self.streaming = False
        self.device_name: str | None = None
        self.device_address: str | None = None
        self.last_sample_at: datetime | None = None
        self.signal_quality = "NO_DATA"
        self.error: str | None = None
        self.acc_streaming = False
        self.ppg_streaming = False
        self.acc_stream_started = False
        self.ppg_stream_started = False
        self.physiological_signal_present = False
        self.last_acc_sample_at: datetime | None = None
        self.ppg_packet_count = 0
        self.ppg_sample_count = 0
        self.acc_packet_count = 0
        self.acc_sample_count = 0
        self.stream_events: list[dict[str, str]] = []
        self.ppg_packet_clock: list[tuple[int, int]] = []
        self.acc_packet_clock: list[tuple[int, int]] = []


class PolarPPG:
    """Async local sensor API. No network service or Clasp app dependency."""

    def __init__(self, device: str | None = None, sampling_rate_hz: int = 135, *,
                 stream_ppg: bool = True, stream_accelerometer: bool = True,
                 accelerometer_sampling_rate_hz: int = 52) -> None:
        if sampling_rate_hz not in (28, 44, 55, 135, 176):
            raise ValueError("unsupported Verity Sense PPG sampling rate")
        self.target = device
        self.sampling_rate_hz = sampling_rate_hz
        self.stream_ppg = stream_ppg
        if accelerometer_sampling_rate_hz not in (26, 52, 104, 208, 416):
            raise ValueError("unsupported Verity Sense accelerometer rate")
        self.stream_accelerometer = stream_accelerometer
        self.accelerometer_sampling_rate_hz = accelerometer_sampling_rate_hz
        self.status = SensorStatus()
        self._connector = None
        self._lock = Lock()
        self._active_capture: Capture | None = None
        self.disconnect_event = asyncio.Event()
        self._recent_ppg = deque(maxlen=8)
        self._active_verification = None
        self._frame_listeners = set()

    def add_frame_listener(self, listener):
        """Receive decoded PPGFrame/ACCFrame packets; returns an unsubscribe function."""
        with self._lock:
            self._frame_listeners.add(listener)
        def unsubscribe():
            with self._lock:
                self._frame_listeners.discard(listener)
        return unsubscribe

    async def connect(self) -> SensorStatus:
        from polar_ble_sdk.connector.ble_discovery import discover_polar_device
        from polar_ble_sdk.connector.stream import create_polar_connector

        device = await discover_polar_device(self.target, timeout=20.0)
        if device is None:
            self.status.error = "No Polar BLE advertisement found"
            return self.status
        name = getattr(device, "name", "") or ""
        if not any(part in name.lower() for part in ("verity", "sense", "oh1")):
            self.status.error = f"Discovered {name or 'unnamed Polar sensor'}, expected Verity Sense"
            return self.status
        self.status.device_name = name
        self.disconnect_event.clear()
        self.status.last_sample_at = None
        self.status.last_acc_sample_at = None
        self.status.physiological_signal_present = False
        self.status.error = None
        self._recent_ppg.clear()
        self.status.ppg_packet_count = self.status.ppg_sample_count = 0
        self.status.acc_packet_count = self.status.acc_sample_count = 0
        self.status.stream_events.clear()
        self.status.ppg_packet_clock.clear()
        self.status.acc_packet_clock.clear()
        self.status.device_address = getattr(device, "address", None)
        self._connector = create_polar_connector(
            device,
            ppg_callback=self._on_ppg if self.stream_ppg else None,
            acc_callback=self._on_acc if self.stream_accelerometer else None,
            sdk_mode=self.stream_ppg and self.sampling_rate_hz != 55,
            ppg_sample_rate=self.sampling_rate_hz,
            acc_sample_rate=self.accelerometer_sampling_rate_hz,
            acc_resolution=16,
            acc_range=8,
            verbose=False,
            log_callback=self._on_log,
        )
        try:
            await self._connector.start_notify()
        except Exception as exc:
            detail = str(exc)
            if "encryption is insufficient" in detail.lower() or "insufficient encryption" in detail.lower():
                self.status.error = (
                    "The sensor requires an encrypted BLE bond for its PMD stream. "
                    "Pair Polar Sense with this Mac in System Settings > Bluetooth, "
                    "close Polar Flow, then retry. " + detail
                )
            else:
                self.status.error = f"Polar BLE connection failed: {type(exc).__name__}: {detail}"
            try:
                await self._connector.stop_notify()
            except Exception:
                pass
            return self.status
        client = getattr(getattr(self._connector, "polar_device", None), "_client", None)
        self.status.connected = bool(getattr(client, "is_connected", False))
        if not self.status.connected:
            self.disconnect_event.set()
            self.status.error = "BLE link was not connected after PMD stream startup"
            return self.status
        self.sampling_rate_hz = int(getattr(self._connector, "_ppg_default_rate", self.sampling_rate_hz))
        self.accelerometer_sampling_rate_hz = 52  # SDK start_streams requests 52 Hz for Verity Sense ACC.
        errors = self._connector.stream_errors
        active = {getattr(stream, "name", str(stream)) for stream in getattr(self._connector, "_active_streams", set())}
        self.status.ppg_stream_started = self.stream_ppg and "PPG" in active and "PPG" not in errors
        self.status.acc_stream_started = self.stream_accelerometer and "ACC" in active and "ACC" not in errors
        self.status.ppg_streaming = self.stream_ppg and self.status.ppg_packet_count > 0
        self.status.acc_streaming = self.stream_accelerometer and self.status.acc_packet_count > 0
        self.status.streaming = ((not self.stream_ppg or self.status.ppg_streaming)
                                 and (not self.stream_accelerometer or self.status.acc_streaming)
                                 and (self.stream_ppg or self.stream_accelerometer))
        if self._connector.stream_errors:
            self.status.error = "; ".join(
                f"{k}: {v}" for k, v in self._connector.stream_errors.items()
            )
        # PMD start acknowledgements do not prove streaming. The callbacks below
        # promote status.streaming only after packet data actually arrives.
        requested_started = (not self.stream_ppg or self.status.ppg_stream_started) and (
            not self.stream_accelerometer or self.status.acc_stream_started
        )
        if requested_started and (self.stream_ppg or self.stream_accelerometer) and not self.status.streaming:
            self.status.error = "PMD start acknowledged; awaiting data notifications"
        return self.status

    async def disconnect(self) -> None:
        if self._connector is not None:
            await self._connector.stop_notify()
        self.status.connected = False
        self.status.streaming = False
        self.status.acc_streaming = False
        self.status.ppg_streaming = False
        self.status.acc_stream_started = False
        self.status.ppg_stream_started = False
        self.status.signal_quality = "NO_DATA"
        self.disconnect_event.set()
        self._invalidate_verification("polar_disconnected")

    async def wait_for_streaming(self, timeout_s: float = 20.0) -> bool:
        """Wait until callbacks prove that every requested PMD stream delivers data."""
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if not self.status.connected:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if self.status.streaming:
                return True
            if self.disconnect_event.is_set() or not self.status.connected:
                return False
            await asyncio.sleep(.05)
        missing = []
        if self.stream_ppg and self.status.ppg_packet_count == 0: missing.append("PPG")
        if self.stream_accelerometer and self.status.acc_packet_count == 0: missing.append("ACC")
        if missing:
            self.status.error = "No data notifications arrived for: " + ", ".join(missing)
        return self.status.streaming

    async def diagnostic_listen(self, duration_s: float = 10.0, *, query_supported_settings: bool = True) -> dict:
        """Count real SDK notifications/callbacks in memory without saving PPG data."""
        if not self.status.connected or self._connector is None:
            raise RuntimeError("BLE link is not connected")
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        before = {
            "ppg_packets": self.status.ppg_packet_count,
            "ppg_samples": self.status.ppg_sample_count,
            "acc_packets": self.status.acc_packet_count,
            "acc_samples": self.status.acc_sample_count,
            "pmd_raw_notifications": int(getattr(getattr(self._connector, "polar_device", None), "_pmd_counts", {}).get("raw", 0)),
        }
        await asyncio.sleep(duration_s)
        connector = self._connector
        polar_device = getattr(connector, "polar_device", None)
        client = getattr(polar_device, "_client", None)
        pmd_counts = getattr(polar_device, "_pmd_counts", {})
        after = {
            "ppg_packets": self.status.ppg_packet_count,
            "ppg_samples": self.status.ppg_sample_count,
            "acc_packets": self.status.acc_packet_count,
            "acc_samples": self.status.acc_sample_count,
            "pmd_raw_notifications": int(pmd_counts.get("raw", 0)),
        }
        def clock_summary(packets, rate):
            if len(packets) < 2:
                return {"packet_count": len(packets), "observed_rate_hz": None,
                        "inter_packet_missing_samples_estimate": None}
            missing = 0
            for (previous_ns, _previous_count), (current_ns, current_count) in zip(packets, packets[1:]):
                expected = round((current_ns - previous_ns) * rate / 1e9)
                if expected > current_count + max(2, round(current_count * .1)):
                    missing += expected - current_count
            elapsed = (packets[-1][0] - packets[0][0]) / 1e9
            sample_count = sum(count for _, count in packets[1:])
            return {"packet_count": len(packets), "observed_rate_hz": sample_count / elapsed if elapsed > 0 else None,
                    "inter_packet_missing_samples_estimate": missing}
        result = {
            "duration_s": duration_s,
            "connected_status": self.status.connected,
            "ble_client_connected": bool(getattr(client, "is_connected", False)),
            "requested_streams": [name for name, enabled in (("PPG", self.stream_ppg), ("ACC", self.stream_accelerometer)) if enabled],
            "sdk_active_streams": sorted(getattr(stream, "name", str(stream)) for stream in getattr(connector, "_active_streams", set())),
            "callback_counts_before": before,
            "callback_counts_after": after,
            "callback_deltas": {key: after[key] - before[key] for key in after},
            "callback_totals": {"ppg_packets": self.status.ppg_packet_count,
                "ppg_samples": self.status.ppg_sample_count, "acc_packets": self.status.acc_packet_count,
                "acc_samples": self.status.acc_sample_count},
            "ppg_clock": clock_summary(self.status.ppg_packet_clock, self.sampling_rate_hz),
            "acc_clock": clock_summary(self.status.acc_packet_clock, self.accelerometer_sampling_rate_hz),
            "callback_errors": dict(getattr(connector, "_callback_errors", {})),
            "pmd_frame_counts": dict(pmd_counts),
            "stream_errors": dict(getattr(connector, "stream_errors", {})),
            "stream_events": list(self.status.stream_events),
        }
        # Query the same SDK API used during negotiation to record device-supported settings.
        try:
            if not query_supported_settings:
                return result
            from polar_ble_sdk._pmd.constants import PmdMeasurementType
            supported = {}
            for name, enabled in (("PPG", self.stream_ppg), ("ACC", self.stream_accelerometer)):
                if not enabled:
                    continue
                settings = await polar_device.request_stream_settings(PmdMeasurementType[name])
                supported[name] = {item.type.name.lower(): list(item.values) for item in settings.settings}
            result["device_supported_settings"] = supported
        except Exception as exc:
            result["settings_query_error"] = f"{type(exc).__name__}: {exc}"
        return result

    async def capture(
        self,
        participant_id: str,
        duration_s: float,
        *,
        session_type: str = "verification",
        output_dir: str | None = None,
        abort_event: asyncio.Event | None = None,
    ) -> Capture:
        """Capture from the live stream; it never generates fallback samples."""
        if not self.status.connected or not self.status.streaming:
            raise RuntimeError("sensor is not connected with an active PPG stream")
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", participant_id):
            raise ValueError("participant_id must be 1-64 letters, digits, underscores, or hyphens")
        capture = Capture(
            participant_id=participant_id,
            session_id=uuid4().hex,
            session_type=session_type,
            started_at=datetime.now(timezone.utc),
            sampling_rate_hz=self.sampling_rate_hz,
            accelerometer_sampling_rate_hz=self.accelerometer_sampling_rate_hz,
            device_name=self.status.device_name,
            device_address=self.status.device_address,
        )
        with self._lock:
            self._active_capture = capture
        try:
            deadline = asyncio.get_running_loop().time() + duration_s
            while asyncio.get_running_loop().time() < deadline:
                if abort_event is not None and abort_event.is_set():
                    break
                if self.disconnect_event.is_set() or not self.status.connected or not self.status.streaming:
                    break
                await asyncio.sleep(min(.05, max(0, deadline - asyncio.get_running_loop().time())))
        finally:
            with self._lock:
                self._active_capture = None
            capture.ended_at = datetime.now(timezone.utc)
            capture.stream_errors = dict(self._connector.stream_errors)
            if not self.status.connected:
                capture.stream_errors["connection"] = "Polar disconnected during capture"
            if not capture.frames:
                capture.stream_errors["PPG data"] = "No PPG packet received during capture"
            if self.stream_accelerometer and not capture.acc_frames:
                capture.stream_errors["ACC data"] = "No accelerometer packet received during capture"
        if output_dir:
            from .storage import save_capture

            save_capture(capture, output_dir)
        return capture

    def _on_ppg(self, payload) -> None:
        timestamp_ns, samples = payload
        received = datetime.now(timezone.utc)
        frame = PPGFrame(int(timestamp_ns), received, tuple(tuple(map(int, row)) for row in samples))
        with self._lock:
            capture = self._active_capture
            if capture is not None:
                capture.frames.append(frame)
            listeners = tuple(self._frame_listeners)
        for listener in listeners:
            listener(frame)
        self.status.last_sample_at = received
        self.status.signal_quality = "RECEIVING"
        values = [tuple(map(int, row)) for row in samples]
        self.status.ppg_packet_count += 1
        self.status.ppg_sample_count += len(values)
        if values:
            self.status.ppg_packet_clock.append((int(timestamp_ns), len(values)))
        self._refresh_streaming_state()
        if values:
            self._recent_ppg.extend(row[2] if len(row) > 2 else row[0] for row in values[-3:])
            self.status.physiological_signal_present = self._physiology_present()
            if not self.status.physiological_signal_present:
                self._invalidate_verification("physiological_signal_lost")

    def _on_acc(self, payload) -> None:
        timestamp_ns, samples = payload
        received = datetime.now(timezone.utc)
        frame = ACCFrame(int(timestamp_ns), received, tuple(tuple(map(int, row)) for row in samples))
        with self._lock:
            capture = self._active_capture
            if capture is not None:
                capture.acc_frames.append(frame)
            listeners = tuple(self._frame_listeners)
        for listener in listeners:
            listener(frame)
        self.status.acc_packet_count += 1
        self.status.acc_sample_count += len(samples)
        if samples:
            self.status.acc_packet_clock.append((int(timestamp_ns), len(samples)))
        self._refresh_streaming_state()
        self.status.last_acc_sample_at = received

    def _refresh_streaming_state(self) -> None:
        if self.disconnect_event.is_set():
            self.status.streaming = False
            self.status.ppg_streaming = False
            self.status.acc_streaming = False
            return
        ppg_ready = not self.stream_ppg or self.status.ppg_packet_count > 0
        acc_ready = not self.stream_accelerometer or self.status.acc_packet_count > 0
        if ppg_ready and self.stream_ppg:
            self.status.ppg_streaming = True
        if acc_ready and self.stream_accelerometer:
            self.status.acc_streaming = True
        self.status.streaming = (self.stream_ppg or self.stream_accelerometer) and ppg_ready and acc_ready
        if self.status.streaming and self.status.error and ("awaiting data notifications" in self.status.error.lower() or "no data notifications arrived" in self.status.error.lower()):
            self.status.error = None

    def _physiology_present(self) -> bool:
        if len(self._recent_ppg) < 6:
            return True  # allow stream startup before enough observations
        values = list(self._recent_ppg)
        return max(values) - min(values) >= 25

    def _bind_verification(self, session) -> None:
        self._active_verification = session
        self.disconnect_event.clear()

    def _invalidate_verification(self, reason: str) -> None:
        session = self._active_verification
        if session is not None:
            session.invalidate(reason)
            self._active_verification = None

    def _on_log(self, message: str, severity: str) -> None:
        self.status.stream_events.append({"severity": severity, "message": message})
        if severity == "error":
            self.status.error = message
        normalized = message.lower()
        # The SDK intentionally reconnects before streaming and emits the
        # informational text "Disconnecting..." during that sequence. Only
        # treat explicit unexpected-link-loss notifications as disconnects.
        unexpected_link_loss = severity in {"warning", "error"} and any(
            phrase in normalized for phrase in ("link lost", "connection lost", "disconnected unexpectedly")
        )
        if unexpected_link_loss:
            self.status.connected = False
            self.status.streaming = False
            self.status.signal_quality = "NO_DATA"
            self.disconnect_event.set()
            self._invalidate_verification("polar_disconnected")
