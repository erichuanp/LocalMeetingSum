"""Audio device enumeration and capture worker.

Windows WASAPI with loopback support — uses PyAudioWPatch, a PyAudio fork
shipping a PortAudio build that exposes WASAPI loopback "input" endpoints
for every output device. Stock sounddevice's bundled PortAudio doesn't have
loopback compiled in, which is why we use this fork.
"""
from __future__ import annotations

import ctypes
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pyaudiowpatch as pa


TARGET_SR = 16000  # FunASR Paraformer expects 16kHz mono


# ---------------------------------------------------------------------------
# Single shared PyAudio instance for the process. PyAudio (and the underlying
# PortAudio) is NOT safe to construct multiple times concurrently from
# different threads on Windows — doing so segfaults. We init once, share
# everywhere, and tear down on process exit.
# ---------------------------------------------------------------------------
_pa_instance: Optional[pa.PyAudio] = None
_pa_lock = threading.Lock()


def _get_pa() -> pa.PyAudio:
    global _pa_instance
    with _pa_lock:
        if _pa_instance is None:
            _pa_instance = pa.PyAudio()
        return _pa_instance


def shutdown_pa():
    global _pa_instance
    with _pa_lock:
        if _pa_instance is not None:
            try:
                _pa_instance.terminate()
            except Exception:
                pass
            _pa_instance = None


@dataclass
class DeviceInfo:
    key: str            # stable id we use over the wire: "in:28", "loop:32", ...
    index: int          # PyAudio host-api device index
    name: str           # without the " [Loopback]" suffix
    kind: str           # "input" | "loopback"
    channels: int
    default_samplerate: int


def list_devices() -> list[DeviceInfo]:
    """Capturable WASAPI devices: real input mics + loopback endpoints of outputs."""
    p = _get_pa()
    out: list[DeviceInfo] = []
    wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
    for d in p.get_device_info_generator_by_host_api(host_api_index=wasapi["index"]):
        if d["maxInputChannels"] <= 0:
            continue
        is_loop = bool(d.get("isLoopbackDevice", False))
        name = d["name"]
        if is_loop and name.endswith(" [Loopback]"):
            name = name[: -len(" [Loopback]")]
        kind = "loopback" if is_loop else "input"
        key = f"{'loop' if is_loop else 'in'}:{d['index']}"
        out.append(DeviceInfo(
            key=key,
            index=d["index"],
            name=name,
            kind=kind,
            channels=int(d["maxInputChannels"]),
            default_samplerate=int(d["defaultSampleRate"]),
        ))
    return out


def device_by_key(key: str) -> Optional[DeviceInfo]:
    for d in list_devices():
        if d.key == key:
            return d
    return None


def _resample_to_16k_mono(pcm: np.ndarray, src_sr: int) -> np.ndarray:
    """pcm: 1-D mono float32. Returns 16kHz mono float32."""
    if src_sr == TARGET_SR:
        return pcm.astype(np.float32, copy=False)
    ratio = TARGET_SR / src_sr
    n_out = int(round(pcm.shape[0] * ratio))
    if n_out <= 1:
        return np.zeros(0, dtype=np.float32)
    x_old = np.linspace(0.0, 1.0, num=pcm.shape[0], endpoint=False, dtype=np.float64)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False, dtype=np.float64)
    return np.interp(x_new, x_old, pcm).astype(np.float32)


class CaptureWorker(threading.Thread):
    """One thread per device. Pushes dict events onto out_queue:
         {"type": "level",  "source": key, "level": float}   ~level_hz times/sec
         {"type": "pcm",    "source": key, "data": ndarray}   every emit_ms (16k mono)
         {"type": "error",  "source": key, "message": str}    on stream open failure
    """

    def __init__(self, dev: DeviceInfo, out_queue: "queue.Queue[dict]",
                 emit_ms: int = 600, level_hz: int = 20):
        super().__init__(daemon=True, name=f"cap-{dev.key}")
        self.dev = dev
        self.out = out_queue
        self.emit_ms = emit_ms
        self._level_interval = 1.0 / max(1, level_hz)
        self._last_level_t = 0.0
        self._peak_acc = 0.0
        self._stop_evt = threading.Event()
        self._buf = np.zeros(0, dtype=np.float32)
        self._native_sr = dev.default_samplerate
        self._channels = dev.channels
        self._block = int(self._native_sr * emit_ms / 1000)
        # Hardware callback size — ~20ms gives reactive level meter + low latency.
        self._hw_block = max(64, int(self._native_sr * 0.02))

    def stop(self):
        self._stop_evt.set()

    def run(self):
        # WASAPI requires COM to be initialized on the thread that opens the
        # stream. Without this, PortAudio falls back to WDM-KS and fails.
        com_initialized = False
        try:
            hr = ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # MULTITHREADED
            com_initialized = (hr in (0, 1))
        except Exception:
            pass

        p = _get_pa()
        stream = None
        try:
            # Serialize stream creation across workers — PortAudio's WASAPI
            # backend dislikes simultaneous opens.
            with _pa_lock:
                stream = p.open(
                    format=pa.paFloat32,
                    channels=self._channels,
                    rate=self._native_sr,
                    input=True,
                    input_device_index=self.dev.index,
                    frames_per_buffer=self._hw_block,
                    stream_callback=self._callback,
                )
                stream.start_stream()
            while not self._stop_evt.is_set() and stream.is_active():
                time.sleep(0.05)
        except Exception as e:
            self.out.put({"type": "error", "source": self.dev.key, "message": str(e)})
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            if com_initialized:
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass

    def _callback(self, in_data, frame_count, time_info, status_flags):
        if self._stop_evt.is_set():
            return (None, pa.paComplete)
        try:
            arr = np.frombuffer(in_data, dtype=np.float32)
            if self._channels > 1:
                arr = arr.reshape(-1, self._channels)
                mono = arr.mean(axis=1).astype(np.float32)
            else:
                mono = arr.astype(np.float32, copy=False)
        except Exception:
            return (None, pa.paContinue)

        # Peak amplitude (mono, normalized).
        if mono.size:
            p = float(np.abs(mono).max())
            if p > self._peak_acc:
                self._peak_acc = p

        # Level emit at fixed rate.
        now = time.monotonic()
        if now - self._last_level_t >= self._level_interval:
            self._last_level_t = now
            try:
                self.out.put_nowait({"type": "level", "source": self.dev.key,
                                     "level": self._peak_acc})
            except queue.Full:
                pass
            self._peak_acc = 0.0

        # Accumulate + emit 16kHz mono chunks for STT.
        self._buf = np.concatenate([self._buf, mono]) if self._buf.size else mono.copy()
        while self._buf.shape[0] >= self._block:
            slice_native = self._buf[: self._block]
            self._buf = self._buf[self._block :]
            mono16k = _resample_to_16k_mono(slice_native, self._native_sr)
            try:
                self.out.put_nowait({"type": "pcm", "source": self.dev.key, "data": mono16k})
            except queue.Full:
                pass

        return (None, pa.paContinue)
