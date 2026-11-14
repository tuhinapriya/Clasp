from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

from .models import Capture, PPGFrame


class SensorStatus:
    def __init__(self) -> None:
        self.connected = False
        self.streaming = False
        self.device_name: str | None = None
        self.device_address: str | None = None
        self.last_sample_at: datetime | None = None
        self.signal_quality = "NO_DATA"
        self.error: str | None = None


class PolarPPG:
    """Async local sensor API. No network service or Clasp app dependency."""

    def __init__(self, device: str | None = None, sampling_rate_hz: int = 135) -> None:
        if sampling_rate_hz not in (28, 44, 55, 135, 176):
            raise ValueError("unsupported Verity Sense PPG sampling rate")
        self.target = device
        self.sampling_rate_hz = sampling_rate_hz
        self.status = SensorStatus()
        self._connector = None
        self._lock = Lock()
        self._active_capture: Capture | None = None

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
        self.status.device_address = getattr(device, "address", None)
        self._connector = create_polar_connector(
            device,
            ppg_callback=self._on_ppg,
            sdk_mode=self.sampling_rate_hz != 55,
            ppg_sample_rate=self.sampling_rate_hz,
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
        self.status.streaming = "PPG" not in self._connector.stream_errors
        if self._connector.stream_errors:
            self.status.error = "; ".join(
                f"{k}: {v}" for k, v in self._connector.stream_errors.items()
            )
        return self.status

    async def disconnect(self) -> None:
        if self._connector is not None:
            await self._connector.stop_notify()
        self.status.connected = False
        self.status.streaming = False
        self.status.signal_quality = "NO_DATA"

    async def capture(
        self,
        participant_id: str,
        duration_s: float,
        *,
        session_type: str = "verification",
        output_dir: str | None = None,
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
            device_name=self.status.device_name,
            device_address=self.status.device_address,
        )
        with self._lock:
            self._active_capture = capture
        try:
            await asyncio.sleep(duration_s)
        finally:
            with self._lock:
                self._active_capture = None
            capture.ended_at = datetime.now(timezone.utc)
            capture.stream_errors = dict(self._connector.stream_errors)
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

    def _on_log(self, message: str, severity: str) -> None:
        if severity == "error":
            self.status.error = message
        if "disconnect" in message.lower() or "link lost" in message.lower():
            self.status.connected = False
            self.status.streaming = False
            self.status.signal_quality = "NO_DATA"
