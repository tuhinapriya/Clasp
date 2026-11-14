"""Clasp's real Vexo Open Kit bridge. JSON lines on stdout; commands on stdin."""
import json
import queue
import sys
import threading
import time
from pathlib import Path

project = Path(__file__).resolve().parent.parent
kit_python = project / "vexo-open-kit" / "python"
sys.path.insert(0, str(kit_python))


_emit_lock = threading.Lock()


def emit(event_type, **data):
    with _emit_lock:
        print(json.dumps({"type": event_type, **data}, separators=(",", ":")), flush=True)


def main():
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        emit("error", message="The Vexo bridge requires the configured band number.")
        return 2
    number = int(sys.argv[1])
    if number < 1 or number > 999:
        emit("error", message="Vexo band number must be from 1 to 999.")
        return 2
    try:
        from vexo_open.sync import Band
    except Exception as error:
        emit("error", message=f"Could not import the local Vexo Open Kit Python SDK: {error}")
        return 2

    commands = queue.Queue()

    def read_commands():
        for line in sys.stdin:
            try:
                commands.put(json.loads(line))
            except Exception:
                continue
        commands.put({"command": "disconnect"})

    threading.Thread(target=read_commands, daemon=True).start()
    band = None
    audio_count = 0
    audio_last_sequence = None
    audio_report_at = time.monotonic()
    microphone_on = False
    stopped = False
    capture = None
    verifier = None
    deferred_capture = None
    voice_jobs = queue.Queue()
    model_ready = threading.Event()
    model_errors = []
    profile_path = project / ".clasp" / "voice-profile.json"
    model_path = project / ".clasp" / "models" / "spkrec-ecapa-voxceleb"

    try:
        # Load Torch/SpeechBrain before opening BLE. Importing and initializing the
        # local model can briefly starve the SDK's heartbeat loop on this Mac.
        # Keep the BLE connection and voice processing on separate phases so a
        # real wearable link is never reported connected while the process stalls.
        try:
            from speaker_verifier import LocalSpeakerVerifier
            verifier = LocalSpeakerVerifier(profile_path, model_path)
            verifier.load(lambda status, model: emit("voice.status", status=status, model=model))
            model_ready.set()
            emit("voice.profile", profile=verifier.profile_metadata())
        except Exception as error:
            model_errors.append(str(error) or error.__class__.__name__)
            model_ready.set()
            emit("voice.error", reason=f"Local ECAPA model unavailable: {model_errors[-1]}")

        # This is the SDK's documented BLE interface. Band.connect verifies the band number
        # reported by the device and closes the link if it is not the requested band.
        band = Band.connect(number)
        expected_name = f"Vexo-HACK-{number:02d}"
        if band.number != number or band.name != expected_name or band.transport_kind != "ble":
            band.disconnect()
            emit("error", message=f"The connected device did not verify as {expected_name} over Bluetooth.")
            return 2

        def on_motion(sample):
            emit("motion", sample={"ax": sample.ax, "ay": sample.ay, "az": sample.az, "gx": sample.gx, "gy": sample.gy, "gz": sample.gz}, time=sample.time)

        def on_event(event):
            emit("gesture", gesture=event.kind, time=event.time, bandTime=event.t)

        def on_link(event):
            emit("link", rssi=event.rssi, phy=event.phy, time=event.time)

        def on_audio(packet):
            nonlocal audio_count, audio_last_sequence, audio_report_at
            if not microphone_on:
                return
            audio_count += 1
            audio_last_sequence = packet.seq
            if capture is not None:
                try:
                    decoded = capture["decoder"].feed(packet.opus, packet.seq)
                    if decoded:
                        capture["pcm"].append(decoded)
                    capture["packets"] += 1
                    capture["lastPacketAt"] = packet.time
                except Exception as error:
                    capture["error"] = str(error) or "Could not decode Vexo audio."
            now = time.monotonic()
            if now - audio_report_at >= 1:
                emit("audio-summary", packets=audio_count, lastSequence=audio_last_sequence, time=packet.time)
                audio_count = 0
                audio_report_at = now

        def on_disconnect(event):
            nonlocal stopped, capture
            stopped = True
            if capture is not None:
                emit("voice.capture.failed", turnId=capture.get("turnId"), reason="Vexo disconnected during voice capture.")
                capture = None
            emit("disconnected", reason=event.reason)

        band.on("motion", on_motion)
        band.on("event", on_event)
        band.on("link", on_link)
        band.on("audio", on_audio)
        band.on("disconnect", on_disconnect)
        band.configure(motion=25, events=True, link=True)
        emit("connected", bandNumber=band.number, name=band.name, transport=band.transport_kind, firmware=band.info.firmware, capabilities=band.info.capabilities)

        def begin_capture(command):
            nonlocal capture, microphone_on, deferred_capture
            kind = command.get("kind")
            if kind not in ("registration", "agent-turn", "test"):
                emit("voice.capture.failed", turnId=command.get("turnId"), reason="Unsupported Vexo voice capture request.")
                return
            if capture is not None:
                emit("voice.capture.failed", turnId=command.get("turnId"), reason="Another Vexo voice capture is already running.")
                return
            if verifier is None:
                emit("voice.capture.failed", turnId=command.get("turnId"), reason="Local ECAPA verifier dependencies are unavailable.")
                return
            if not model_ready.is_set():
                deferred_capture = command
                emit("voice.status", status="loading", model="SpeechBrain ECAPA-TDNN", turnId=command.get("turnId"))
                return
            if model_errors:
                event = "voice.registration.failed" if kind == "registration" else "voice.capture.failed"
                emit(event, turnId=command.get("turnId"), reason=f"Local ECAPA model failed to load: {model_errors[-1]}")
                return
            replace = command.get("replace") is True
            if kind == "registration" and verifier.profile_metadata()["registered"] and not replace:
                emit("voice.registration.failed", turnId=command.get("turnId"), reason="A voice profile already exists; explicit re-enrollment confirmation is required.")
                return
            try:
                emit("voice.status", status="loading", model="SpeechBrain ECAPA-TDNN", turnId=command.get("turnId"))
                from vexo_open.audio import PcmDecoder
                decoder = PcmDecoder()
            except Exception as error:
                emit("voice.capture.failed", turnId=command.get("turnId"), reason=f"Could not load the local ECAPA model or Vexo Opus decoder: {error}")
                return
            was_on = microphone_on
            try:
                if not microphone_on:
                    band.configure(audio=True)
                    microphone_on = True
                    emit("microphone", active=True)
                duration = 10 if kind == "registration" else 8
                capture = {
                    "kind": kind,
                    "turnId": command.get("turnId"),
                    "replace": replace,
                    "decoder": decoder,
                    "pcm": [],
                    "packets": 0,
                    "targetPackets": duration * 50,
                    "startedAt": time.time(),
                    "lastPacketAt": None,
                    "microphoneWasOn": was_on,
                    "error": None,
                }
                emit("voice.capture.started", kind=kind, turnId=command.get("turnId"), durationSeconds=duration, source="verified Vexo Open Kit microphone")
            except Exception as error:
                capture = None
                if not was_on:
                    try:
                        band.configure(audio=False)
                        microphone_on = False
                        emit("microphone", active=False)
                    except Exception:
                        pass
                emit("voice.capture.failed", turnId=command.get("turnId"), reason=f"Could not start the Vexo microphone: {error}")

        def finish_capture():
            nonlocal capture, microphone_on
            current = capture
            if current is None:
                return
            capture = None
            try:
                pcm = b"".join(current["pcm"]) + current["decoder"].flush()
                if current["error"]:
                    raise RuntimeError(current["error"])
                source = {
                    "device": band.name,
                    "bandNumber": band.number,
                    "transport": band.transport_kind,
                    "codec": "opus",
                    "sampleRate": 16000,
                    "channels": 1,
                    "packetCount": current["packets"],
                    "capturedSeconds": round(len(pcm) / (2 * 16000), 2),
                    "rawAudioStored": False,
                }
                voice_jobs.put({"kind": current["kind"], "turnId": current["turnId"], "replace": current["replace"], "pcm": pcm, "source": source})
                emit("voice.capture.completed", kind=current["kind"], turnId=current["turnId"], source=source)
            except Exception as error:
                event = "voice.registration.failed" if current["kind"] == "registration" else "voice.validation.completed"
                if event == "voice.validation.completed":
                    now = time.time()
                    emit(event, turnId=current["turnId"], factor="voice-speaker", decision="UNCERTAIN", identityId=None, verifier="speechbrain/spkrec-ecapa-voxceleb", similarity=None, verifiedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), expiresAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 15)), simulated=False, audioSource={"device": band.name, "bandNumber": band.number, "transport": band.transport_kind, "codec": "opus", "sampleRate": 16000, "channels": 1, "packetCount": current["packets"], "rawAudioStored": False}, audioUsable=False, reason=str(error) or "Local speaker verification failed.")
                    emit("voice.transcription", turnId=current["turnId"], text="", speakerDecision="UNCERTAIN", reason=str(error))
                else:
                    emit(event, turnId=current["turnId"], reason=str(error) or "Voice enrollment failed.")
            finally:
                current["pcm"].clear()
                if not current["microphoneWasOn"] and microphone_on:
                    try:
                        band.configure(audio=False)
                    finally:
                        microphone_on = False
                        emit("microphone", active=False)

        def process_voice_jobs():
            while True:
                job = voice_jobs.get()
                if job is None:
                    return
                pcm = job["pcm"]
                if job["kind"] == "registration":
                    try:
                        profile = verifier.register(pcm, job["replace"])
                        emit("voice.registration.completed", turnId=job["turnId"], profile=profile, source=job["source"])
                    except Exception as error:
                        emit("voice.registration.failed", turnId=job["turnId"], reason=str(error) or "Voice enrollment failed.")
                    finally:
                        del pcm
                    continue

                try:
                    result = verifier.validate(pcm, job["source"])
                except Exception as error:
                    now = time.time()
                    result = {
                        "factor": "voice-speaker", "decision": "UNCERTAIN",
                        "identityId": None,
                        "verifier": "speechbrain/spkrec-ecapa-voxceleb", "similarity": None,
                        "verifiedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                        "expiresAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 15)),
                        "simulated": False, "audioSource": job["source"], "audioUsable": False,
                        "reason": str(error) or "Local speaker verification failed.",
                    }
                emit("voice.test.completed" if job["kind"] == "test" else "voice.validation.completed", turnId=job["turnId"], **result)
                if job["kind"] == "test":
                    continue
                transcript = ""
                transcription_error = None
                if result.get("audioUsable"):
                    try:
                        transcript = verifier.transcribe(pcm)
                    except Exception as error:
                        transcription_error = f"Local transcription unavailable: {error}"
                emit("voice.transcription", turnId=job["turnId"], text=transcript, speakerDecision=result["decision"], reason=transcription_error or result.get("reason"), source=job["source"])
                del pcm

        threading.Thread(target=process_voice_jobs, name="clasp-speaker-worker", daemon=True).start()

        while band.connected and not stopped:
            try:
                command = commands.get(timeout=0.2)
            except queue.Empty:
                command = None
            if command:
                action = command.get("command")
                if action == "microphone-on" and not microphone_on:
                    band.configure(audio=True)
                    microphone_on = True
                    emit("microphone", active=True)
                elif action == "microphone-off" and microphone_on:
                    band.configure(audio=False)
                    microphone_on = False
                    emit("microphone", active=False)
                elif action == "capture-voice":
                    begin_capture(command)
                elif action == "stop-voice" and capture is not None:
                    finish_capture()
                elif action == "disconnect":
                    if microphone_on:
                        band.configure(audio=False)
                        microphone_on = False
                    band.disconnect()
                    stopped = True
                    break
            emit("heartbeat", bandNumber=band.number, name=band.name)
            band.wait(0.2)
            if capture is not None and capture["packets"] >= capture["targetPackets"]:
                finish_capture()
            if deferred_capture is not None and model_ready.is_set():
                command, deferred_capture = deferred_capture, None
                begin_capture(command)
    except Exception as error:
        emit("error", message=str(error) or error.__class__.__name__)
        return 1
    finally:
        if band is not None and band.connected:
            try:
                band.disconnect()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
