"""FastAPI server: device list, file upload, WebSocket streaming, LLM summary.

Run:  python server.py
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect, Body, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

import audio_capture
import audio_decode
import stt
import summarizer


HERE = Path(__file__).parent
STATIC = HERE / "static"
UPLOADS = HERE / "uploads"
UPLOADS.mkdir(exist_ok=True)


app = FastAPI(title="LocalMeetingSum")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.on_event("startup")
async def _preload_models():
    """Load FunASR models in a thread at startup so the first user request
       isn't blocked by an ~80s cold start."""
    import threading
    def _go():
        try:
            stt._ensure_loaded()
            print("[startup] FunASR models loaded", flush=True)
        except Exception as e:
            print(f"[startup] model preload failed: {e}", flush=True)
    threading.Thread(target=_go, daemon=True, name="model-preload").start()


@app.get("/")
def root():
    return FileResponse(STATIC / "index.html")


@app.get("/api/devices")
def api_devices():
    devs = audio_capture.list_devices()
    return {"devices": [asdict(d) for d in devs]}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    """Accept any audio/video. Save under uploads/, return session id."""
    sid = uuid.uuid4().hex[:12]
    ext = Path(file.filename or "blob").suffix or ".bin"
    target = UPLOADS / f"{sid}{ext}"
    with target.open("wb") as f:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    return {"session_id": sid, "filename": file.filename, "path": str(target)}


@app.post("/api/summarize")
def api_summarize(payload: dict = Body(...)):
    """Body: { utterances: [...], mapping: { "Mic-A": "person1", ... }, instruction?: str }"""
    utterances = payload.get("utterances") or []
    mapping = payload.get("mapping") or {}
    instruction = payload.get("instruction")
    if mapping:
        utterances = [
            {**u, "speaker": mapping.get(u.get("speaker"), u.get("speaker"))}
            for u in utterances
        ]
    try:
        summary = summarizer.summarize(utterances, extra_instruction=instruction)
        return summary
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")


def _short_label(name: str) -> str:
    name = name.strip()
    for sep in [" (", "(", " - "]:
        if sep in name:
            name = name.split(sep, 1)[0]
            break
    return name[:14]


# ============================================================================
# WebSocket connection: holds capture workers + optional STT sessions per source
# ============================================================================

class Connection:
    """Owns the audio workers + STT state for one WebSocket client.

    Sources can be added/removed at any time. STT is a separate switch — when
    enabled, all current and future sources also feed a StreamingSession.
    """

    def __init__(self, ws: WebSocket, main_loop: asyncio.AbstractEventLoop):
        self.ws = ws
        self.main_loop = main_loop
        self.audio_q: "queue.Queue[dict]" = queue.Queue(maxsize=4096)
        self.workers: dict[str, audio_capture.CaptureWorker] = {}
        self.labels: dict[str, str] = {}
        self.sessions: dict[str, stt.StreamingSession] = {}  # only when STT is on
        self.stt_enabled = False
        self.stop_evt = threading.Event()
        self.consumer = threading.Thread(target=self._consume, daemon=True, name="ws-consumer")
        self.consumer.start()

    # ---- source lifecycle ----
    def add_source(self, key: str, label: Optional[str] = None) -> bool:
        if key in self.workers:
            if label:
                self.labels[key] = label
            return True
        dev = audio_capture.device_by_key(key)
        if dev is None:
            self._send({"type": "error", "message": f"unknown device {key}"})
            return False
        self.labels[key] = label or _short_label(dev.name)
        chunk_ms = int(os.getenv("STREAM_CHUNK_MS", "600"))
        w = audio_capture.CaptureWorker(dev, self.audio_q, emit_ms=chunk_ms)
        self.workers[key] = w
        w.start()
        if self.stt_enabled:
            self._make_session(key)
        return True

    def remove_source(self, key: str):
        w = self.workers.pop(key, None)
        if w:
            w.stop()
        self.labels.pop(key, None)
        sess = self.sessions.pop(key, None)
        if sess:
            try:
                for ev in sess.flush():
                    self._send(ev)
            except Exception as e:
                self._send({"type": "error", "source": key, "message": f"flush: {e}"})

    # ---- STT switch ----
    def start_live(self):
        if self.stt_enabled:
            return
        self.stt_enabled = True
        for key in list(self.workers.keys()):
            self._make_session(key)
        self._send({"type": "live_started", "sources": list(self.workers.keys())})

    def stop_live(self):
        if not self.stt_enabled:
            return
        self.stt_enabled = False
        for key, sess in list(self.sessions.items()):
            try:
                for ev in sess.flush():
                    self._send(ev)
            except Exception as e:
                self._send({"type": "error", "source": key, "message": f"flush: {e}"})
        self.sessions.clear()
        self._send({"type": "live_stopped"})

    def shutdown(self):
        self.stop_evt.set()
        for w in list(self.workers.values()):
            w.stop()
        self.workers.clear()
        for sess in list(self.sessions.values()):
            try:
                # Best-effort: flush, but the WS is closing so messages may not reach client.
                sess.flush()
            except Exception:
                pass
        self.sessions.clear()

    # ---- internals ----
    def _make_session(self, key: str):
        if key in self.sessions:
            return
        label = self.labels.get(key) or "Src"
        self.sessions[key] = stt.StreamingSession(
            source_key=key,
            source_label=label,
            spk_threshold=float(os.getenv("SPK_SIM_THRESHOLD", "0.55")),
            silence_ms=int(os.getenv("VAD_SILENCE_MS", "500")),
            max_utterance_ms=int(os.getenv("MAX_UTTERANCE_MS", "15000")),
        )

    def _consume(self):
        while not self.stop_evt.is_set():
            try:
                msg = self.audio_q.get(timeout=0.1)
            except queue.Empty:
                continue
            mtype = msg.get("type")
            if mtype == "level":
                self._send(msg)
            elif mtype == "error":
                self._send(msg)
            elif mtype == "pcm":
                sess = self.sessions.get(msg["source"])
                if sess is None:
                    continue
                try:
                    for ev in sess.feed(msg["data"]):
                        self._send(ev)
                except Exception as e:
                    self._send({"type": "error", "source": msg["source"], "message": f"stt: {e}"})

    def _send(self, ev: dict):
        if self.stop_evt.is_set():
            return
        try:
            payload = json.dumps(ev, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        coro = self.ws.send_text(payload)
        asyncio.run_coroutine_threadsafe(coro, self.main_loop)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    main_loop = asyncio.get_running_loop()
    conn = Connection(ws, main_loop)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                await ws.send_text(json.dumps({"type": "error", "message": "bad json"}))
                continue
            cmd = msg.get("cmd")
            if cmd == "add_source":
                conn.add_source(msg.get("key"), msg.get("label"))
                await ws.send_text(json.dumps({
                    "type": "source_added", "key": msg.get("key"),
                    "label": conn.labels.get(msg.get("key")),
                }))
            elif cmd == "remove_source":
                conn.remove_source(msg.get("key"))
                await ws.send_text(json.dumps({"type": "source_removed", "key": msg.get("key")}))
            elif cmd == "relabel":
                key = msg.get("key")
                if key in conn.workers:
                    conn.labels[key] = msg.get("label") or conn.labels.get(key, "Src")
            elif cmd == "start_live":
                conn.start_live()
            elif cmd == "stop_live":
                conn.stop_live()
            elif cmd == "process_file":
                if conn.stt_enabled:
                    conn.stop_live()
                # Also stop any active captures to avoid contention with file STT.
                for k in list(conn.workers.keys()):
                    conn.remove_source(k)
                sid = msg.get("session_id")
                if not sid:
                    await ws.send_text(json.dumps({"type": "error", "message": "missing session_id"}))
                    continue
                matches = list(UPLOADS.glob(f"{sid}.*"))
                if not matches:
                    await ws.send_text(json.dumps({"type": "error", "message": "file not found"}))
                    continue
                path = matches[0]
                await ws.send_text(json.dumps({"type": "progress", "stage": "decoding", "percent": 0}))
                try:
                    pcm = await asyncio.to_thread(audio_decode.decode_to_pcm16k_mono, path)
                except Exception as e:
                    await ws.send_text(json.dumps({"type": "error", "message": f"decode: {e}"}))
                    continue
                await ws.send_text(json.dumps({"type": "progress", "stage": "transcribing", "percent": 10}))
                label = msg.get("label") or "File"
                try:
                    utts = await asyncio.to_thread(stt.process_file, pcm, label)
                except Exception as e:
                    await ws.send_text(json.dumps({"type": "error", "message": f"stt: {e}"}))
                    continue
                for u in utts:
                    await ws.send_text(json.dumps(u, ensure_ascii=False))
                await ws.send_text(json.dumps({"type": "done"}))
            else:
                await ws.send_text(json.dumps({"type": "error", "message": f"unknown cmd: {cmd}"}))
    except WebSocketDisconnect:
        pass
    finally:
        conn.shutdown()


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "788"))
    host = os.getenv("HOST", "0.0.0.0")
    uvicorn.run("server:app", host=host, port=port, reload=False)
