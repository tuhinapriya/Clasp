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
        self.physiological_signal_present = False
        self.last_acc_sample_at: datetime | None = None


class PolarPPG:
    """Async local sensor API. No network service or Clasp app dependency."""

    def __init__(self, device: str | None = None, sampling_rate_hz: int = 135, *,
                 stream_accelerometer: bool = True, accelerometer_sampling_rate_hz: int = 52) -> None:
        if sampling_rate_hz not in (28, 44, 55, 135, 176):
            raise ValueError("unsupported Verity Sense PPG sampling rate")
        self.target = device
        self.sampling_rate_hz = sampling_rate_hz
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
        self.status.device_address = getattr(device, "address", None)
        self._connector = create_polar_connector(
            device,
            ppg_callback=self._on_ppg,
            acc_callback=self._on_acc if self.stream_accelerometer else None,
            sdk_mode=self.sampling_rate_hz != 55,
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
        self.status.connected = True
        self.sampling_rate_hz = int(getattr(self._connector, "_ppg_default_rate", self.sampling_rate_hz))
        self.accelerometer_sampling_rate_hz = 52  # SDK start_streams requests 52 Hz for Verity Sense ACC.
        errors = self._connector.stream_errors
        active = {getattr(stream, "name", str(stream)) for stream in getattr(self._connector, "_active_streams", set())}
        self.status.acc_streaming = (not self.stream_accelerometer) or ("ACC" in active and "ACC" not in errors)
        self.status.streaming = "PPG" in active and self.status.acc_streaming and "PPG" not in errors
        if self._connector.stream_errors:
            self.status.error = "; ".join(
                f"{k}: {v}" for k, v in self._connector.stream_errors.items()
            )
        # A PMD start acknowledgement alone does not prove that data notifications
        # are flowing. Require at least one packet from each requested stream.
        if self.status.streaming:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            while loop.time() < deadline:
                ppg_ready = self.status.last_sample_at is not None
                acc_ready = not self.stream_accelerometer or self.status.last_acc_sample_at is not None
                if ppg_ready and acc_ready:
                    break
                if self.disconnect_event.is_set():
                    break
                await asyncio.sleep(.05)
            ppg_ready = self.status.last_sample_at is not None
            acc_ready = not self.stream_accelerometer or self.status.last_acc_sample_at is not None
            if not ppg_ready or not acc_ready:
                missing = [name for name, ready in (("PPG", ppg_ready), ("ACC", acc_ready)) if not ready]
                self.status.streaming = False
                self.status.error = "BLE connected and PMD start acknowledged, but no data notifications arrived for: " + ", ".join(missing)
        return self.status

    async def disconnect(self) -> None:
        if self._connector is not None:
            await self._connector.stop_notify()
        self.status.connected = False
        self.status.streaming = False
        self.status.acc_streaming = False
        self.status.signal_quality = "NO_DATA"
        self.disconnect_event.set()
        self._invalidate_verification("polar_disconnected")

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
        with self._lock:
            capture = self._active_capture
            if capture is not None:
                capture.frames.append(
                    PPGFrame(int(timestamp_ns), received, tuple(tuple(map(int, row)) for row in samples))
                )
        self.status.last_sample_at = received
        self.status.signal_quality = "RECEIVING"
        values = [tuple(map(int, row)) for row in samples]
        if values:
            self._recent_ppg.extend(row[2] if len(row) > 2 else row[0] for row in values[-3:])
            self.status.physiological_signal_present = self._physiology_present()
            if not self.status.physiological_signal_present:
                self._invalidate_verification("physiological_signal_lost")

    def _on_acc(self, payload) -> None:
        timestamp_ns, samples = payload
        received = datetime.now(timezone.utc)
        with self._lock:
            capture = self._active_capture
            if capture is not None:
                capture.acc_frames.append(ACCFrame(int(timestamp_ns), received,
                    tuple(tuple(map(int, row)) for row in samples)))
        self.status.last_acc_sample_at = received

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
        if severity == "error":
            self.status.error = message
        if "disconnect" in message.lower() or "link lost" in message.lower():
            self.status.connected = False
            self.status.streaming = False
            self.status.signal_quality = "NO_DATA"
            self.disconnect_event.set()
            self._invalidate_verification("polar_disconnected")
