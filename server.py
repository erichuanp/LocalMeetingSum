"""FastAPI server: file upload, WebSocket streaming (browser-captured PCM), LLM summary.

Audio capture lives in the browser (getUserMedia / getDisplayMedia). The
server receives float32 PCM frames over a WebSocket binary channel, runs the
FunASR 2-pass STT pipeline, and pushes back transcript events as JSON.

Run:  python server.py
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect, Body, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

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


@app.get("/api/health")
def api_health():
    return {"ok": True, "models_loaded": stt._streaming_model is not None}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    """Accept any audio/video file. Save under uploads/, return session id."""
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


def _strip_source_prefix(speaker: str) -> str:
    """'File-A' -> 'A',  'X-AB' -> 'AB'. process_file() emits '<label>-<letter>'."""
    return speaker.rsplit("-", 1)[-1] if speaker else speaker


def _fmt_hms(ms: int) -> str:
    """123_456 ms -> '0:02:03'."""
    s = max(0, int(ms)) // 1000
    return f"{s // 3600}:{(s // 60) % 60:02d}:{s % 60:02d}"


def _to_markdown(utterances: list[dict]) -> str:
    """Render utterances as the format the user specified:
           **发言人：A  0:12:24**
           你好你好，我是发言人A。

           **发言人：B  0:12:27**
           ...
    """
    lines: list[str] = []
    for u in sorted(utterances, key=lambda x: x.get("start_ms", 0)):
        spk = u.get("speaker") or "?"
        ts = _fmt_hms(u.get("start_ms", 0))
        text = (u.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"**发言人：{spk}  {ts}**")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@app.post("/api/transcribe")
def api_transcribe(payload: Optional[dict] = Body(None),
                   path: Optional[str] = Query(None)):
    """Transcribe an audio/video file ALREADY ON THE SERVER's filesystem.

    Writes the Markdown next to the input file:
        <input_dir>/<YYYYMMDDHHMMSS>+<original_filename>.md

    Body or query supports the file path:
        curl -X POST 'http://localhost:788/api/transcribe?path=C:/x/meeting.mp4'
        curl -X POST http://localhost:788/api/transcribe \\
             -H 'Content-Type: application/json' \\
             -d '{"path":"C:/x/meeting.mp4"}'

    No upload — the path is read by the server directly. Intended for LAN use.
    """
    raw_path = path or ((payload or {}).get("path"))
    if not raw_path:
        raise HTTPException(400, "missing 'path' (query string or JSON body)")
    src = Path(raw_path).expanduser()
    if not src.exists():
        raise HTTPException(404, f"file not found: {src}")
    if not src.is_file():
        raise HTTPException(400, f"not a file: {src}")

    # 1. Decode
    try:
        pcm = audio_decode.decode_to_pcm16k_mono(src)
    except Exception as e:
        raise HTTPException(500, f"decode failed: {e}")
    if pcm.size == 0:
        raise HTTPException(400, "no audio in file")
    duration_ms = int(pcm.size * 1000 / 16000)

    # 2. STT (offline + diarization). Use a benign label so we can strip it later.
    try:
        utts = stt.process_file(pcm, source_label="X")
    except Exception as e:
        raise HTTPException(500, f"stt failed: {e}")

    # 3. Strip prefix → plain A, B, C, ...
    for u in utts:
        u["speaker"] = _strip_source_prefix(u.get("speaker", "?"))

    # 4. Write markdown next to the input file.
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    md_name = f"{ts}+{src.name}.md"
    md_path = src.parent / md_name
    try:
        md_path.write_text(_to_markdown(utts), encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"write failed ({md_path}): {e}")

    speakers = sorted({u["speaker"] for u in utts})
    return {
        "ok": True,
        "input": str(src),
        "md_path": str(md_path),
        "speakers": speakers,
        "utterances": len(utts),
        "duration_ms": duration_ms,
    }


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


# ============================================================================
# WebSocket connection: per-client source declarations + STT sessions.
#
# Wire format:
#   text frame    → JSON command (see ws_endpoint)
#   binary frame  → PCM submission
#                   layout: [u8 version=1][u8 id_len][id bytes (UTF-8)]
#                           [float32 LE PCM samples ...]
#                   PCM is mono at the sample_rate declared in open_source.
# ============================================================================

WIRE_VERSION = 1
TARGET_SR = 16000


class Connection:
    """STT state for one WebSocket client. Sources are declared by the client
    over the wire; the server holds StreamingSession instances and feeds them
    PCM as it arrives."""

    def __init__(self, ws: WebSocket, main_loop: asyncio.AbstractEventLoop):
        self.ws = ws
        self.main_loop = main_loop
        self.audio_q: "queue.Queue[dict]" = queue.Queue(maxsize=4096)
        self.declared: set[str] = set()                    # source ids the client has opened
        self.labels: dict[str, str] = {}
        self.sessions: dict[str, stt.StreamingSession] = {}  # only when STT is on
        self.stt_enabled = False
        self.stop_evt = threading.Event()
        self.consumer = threading.Thread(target=self._consume, daemon=True, name="ws-consumer")
        self.consumer.start()

    # ---- source lifecycle ----
    def open_source(self, sid: str, label: Optional[str] = None):
        if not sid:
            return
        self.declared.add(sid)
        self.labels[sid] = label or "Src"
        if self.stt_enabled:
            self._make_session(sid)

    def close_source(self, sid: str):
        self.declared.discard(sid)
        self.labels.pop(sid, None)
        sess = self.sessions.pop(sid, None)
        if sess:
            try:
                for ev in sess.flush():
                    self._send(ev)
            except Exception as e:
                self._send({"type": "error", "source": sid, "message": f"flush: {e}"})

    def relabel(self, sid: str, label: Optional[str]):
        if not label or sid not in self.declared:
            return
        self.labels[sid] = label

    def feed_pcm(self, sid: str, pcm: np.ndarray):
        """Drop into the queue; consumer thread will route to StreamingSession."""
        if sid not in self.declared or pcm.size == 0:
            return
        try:
            self.audio_q.put_nowait({"source": sid, "data": pcm})
        except queue.Full:
            pass

    # ---- STT switch ----
    def start_live(self):
        if self.stt_enabled:
            return
        self.stt_enabled = True
        for sid in list(self.declared):
            self._make_session(sid)
        self._send({"type": "live_started", "sources": list(self.declared)})

    def stop_live(self):
        if not self.stt_enabled:
            return
        self.stt_enabled = False
        for sid, sess in list(self.sessions.items()):
            try:
                for ev in sess.flush():
                    self._send(ev)
            except Exception as e:
                self._send({"type": "error", "source": sid, "message": f"flush: {e}"})
        self.sessions.clear()
        self._send({"type": "live_stopped"})

    def shutdown(self):
        self.stop_evt.set()
        for sess in list(self.sessions.values()):
            try:
                sess.flush()
            except Exception:
                pass
        self.sessions.clear()

    # ---- internals ----
    def _make_session(self, sid: str):
        if sid in self.sessions:
            return
        label = self.labels.get(sid) or "Src"
        self.sessions[sid] = stt.StreamingSession(
            source_key=sid,
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


def _parse_pcm_frame(data: bytes) -> tuple[Optional[str], Optional[np.ndarray]]:
    """Decode our wire format. Returns (source_id, pcm) or (None, None) on bad frame."""
    if len(data) < 2:
        return None, None
    if data[0] != WIRE_VERSION:
        return None, None
    id_len = data[1]
    if id_len == 0 or id_len > 255 or len(data) < 2 + id_len:
        return None, None
    try:
        sid = data[2:2 + id_len].decode("utf-8")
    except UnicodeDecodeError:
        return None, None
    payload = data[2 + id_len:]
    if len(payload) == 0 or len(payload) % 4 != 0:
        return None, None
    pcm = np.frombuffer(payload, dtype="<f4")
    return sid, pcm


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    main_loop = asyncio.get_running_loop()
    conn = Connection(ws, main_loop)
    try:
        while True:
            event = await ws.receive()
            etype = event.get("type")
            if etype == "websocket.disconnect":
                break
            text = event.get("text")
            data = event.get("bytes")
            if text is not None:
                try:
                    msg = json.loads(text)
                except Exception:
                    await ws.send_text(json.dumps({"type": "error", "message": "bad json"}))
                    continue
                await _handle_ctrl(ws, conn, msg)
            elif data is not None:
                sid, pcm = _parse_pcm_frame(data)
                if sid is not None and pcm is not None:
                    conn.feed_pcm(sid, pcm)
    except WebSocketDisconnect:
        pass
    finally:
        conn.shutdown()


async def _handle_ctrl(ws: WebSocket, conn: Connection, msg: dict):
    cmd = msg.get("cmd")
    if cmd == "open_source":
        conn.open_source(msg.get("id"), msg.get("label"))
        await ws.send_text(json.dumps({"type": "source_opened", "id": msg.get("id"),
                                       "label": conn.labels.get(msg.get("id"))}))
    elif cmd == "close_source":
        conn.close_source(msg.get("id"))
        await ws.send_text(json.dumps({"type": "source_closed", "id": msg.get("id")}))
    elif cmd == "relabel":
        conn.relabel(msg.get("id"), msg.get("label"))
    elif cmd == "start_live":
        conn.start_live()
    elif cmd == "stop_live":
        conn.stop_live()
    elif cmd == "process_file":
        if conn.stt_enabled:
            conn.stop_live()
        sid = msg.get("session_id")
        if not sid:
            await ws.send_text(json.dumps({"type": "error", "message": "missing session_id"}))
            return
        matches = list(UPLOADS.glob(f"{sid}.*"))
        if not matches:
            await ws.send_text(json.dumps({"type": "error", "message": "file not found"}))
            return
        path = matches[0]
        await ws.send_text(json.dumps({"type": "progress", "stage": "decoding", "percent": 0}))
        try:
            pcm = await asyncio.to_thread(audio_decode.decode_to_pcm16k_mono, path)
        except Exception as e:
            await ws.send_text(json.dumps({"type": "error", "message": f"decode: {e}"}))
            return
        await ws.send_text(json.dumps({"type": "progress", "stage": "transcribing", "percent": 10}))
        label = msg.get("label") or "File"
        try:
            utts = await asyncio.to_thread(stt.process_file, pcm, label)
        except Exception as e:
            await ws.send_text(json.dumps({"type": "error", "message": f"stt: {e}"}))
            return
        for u in utts:
            await ws.send_text(json.dumps(u, ensure_ascii=False))
        await ws.send_text(json.dumps({"type": "done"}))
    else:
        await ws.send_text(json.dumps({"type": "error", "message": f"unknown cmd: {cmd}"}))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "788"))
    host = os.getenv("HOST", "0.0.0.0")
    ssl_kwargs = {}
    cert = os.getenv("SSL_CERTFILE")
    key = os.getenv("SSL_KEYFILE")
    if cert and key and Path(cert).exists() and Path(key).exists():
        ssl_kwargs = {"ssl_certfile": cert, "ssl_keyfile": key}
        print(f"[startup] HTTPS enabled: {cert}", flush=True)
    uvicorn.run("server:app", host=host, port=port, reload=False, **ssl_kwargs)
