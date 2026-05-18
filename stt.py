"""FunASR 2-pass streaming STT + per-source speaker diarization.

Design:
  - One streaming Paraformer instance produces partial text every ~600ms.
  - A separate offline Paraformer (with VAD + punctuation) re-decodes each
    finalized utterance using its full bidirectional context — this is the
    semantic self-correction pass (鱼 → 鱿鱼 once it sees the whole utterance).
  - One FSMN-VAD instance per source detects utterance boundaries online.
  - One CAM++ instance produces speaker embeddings; we cluster online per
    source with cosine similarity. Sources never share a speaker — different
    sources are different people by axiom.

All models are shared across sources (FunASR is thread-safe enough for our
serialized call pattern — every call holds the GIL anyway).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


# FunASR (via tqdm) calls sys.stderr.flush() on every inference. When stderr
# is a broken pipe (uvicorn detached, parent `| head` exited, etc.), the flush
# raises OSError EINVAL and the WHOLE inference call fails. We wrap stderr in
# an error-swallowing proxy so any flush/write failure is silently dropped.

class _SafeStream:
    def __init__(self, underlying):
        self._u = underlying
    def write(self, s):
        try:
            return self._u.write(s)
        except OSError:
            return len(s) if isinstance(s, str) else 0
    def flush(self):
        try:
            self._u.flush()
        except OSError:
            pass
    def isatty(self):
        try:
            return self._u.isatty()
        except OSError:
            return False
    def __getattr__(self, name):
        return getattr(self._u, name)

# Wrap once, idempotent.
if not isinstance(sys.stderr, _SafeStream):
    sys.stderr = _SafeStream(sys.stderr)
if not isinstance(sys.stdout, _SafeStream):
    sys.stdout = _SafeStream(sys.stdout)

# Defer heavy import to module init time, but allow tests to mock.
_funasr = None
_streaming_model = None
_offline_model = None
_vad_model = None
_spk_model = None
_models_lock = threading.Lock()


def _ensure_loaded():
    global _funasr, _streaming_model, _offline_model, _vad_model, _spk_model
    with _models_lock:
        if _streaming_model is not None:
            return
        from funasr import AutoModel  # noqa
        _funasr = AutoModel
        device = os.getenv("STT_DEVICE", "cuda")
        ncpu = int(os.getenv("STT_NCPU", "4"))
        # Streaming model: real-time partials.
        _streaming_model = AutoModel(
            model="paraformer-zh-streaming",
            device=device, ncpu=ncpu, disable_update=True,
        )
        # Offline rescoring model: full-context corrected text + punctuation.
        _offline_model = AutoModel(
            model="paraformer-zh",
            punc_model="ct-punc",
            device=device, ncpu=ncpu, disable_update=True,
        )
        # Online VAD for end-of-utterance detection.
        _vad_model = AutoModel(
            model="fsmn-vad",
            device=device, ncpu=ncpu, disable_update=True,
        )
        # Speaker embedder.
        _spk_model = AutoModel(
            model="cam++",
            device=device, ncpu=ncpu, disable_update=True,
        )


@dataclass
class Utterance:
    id: int
    source: str
    start_ms: int
    end_ms: int
    text: str
    speaker: str        # e.g., "Mic-A"
    spk_embedding: Optional[np.ndarray] = None


@dataclass
class _SpkCluster:
    label: str
    centroid: np.ndarray
    count: int


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


class StreamingSession:
    """One per source. Feed PCM in, get events out."""

    SR = 16000

    def __init__(self, source_key: str, source_label: str,
                 spk_threshold: float = 0.55,
                 silence_ms: int = 500,
                 max_utterance_ms: int = 15000):
        _ensure_loaded()
        self.source_key = source_key
        self.source_label = source_label   # human-readable, used as speaker prefix
        self.spk_threshold = spk_threshold
        self.silence_ms = silence_ms
        self.max_utterance_ms = max_utterance_ms

        # Streaming cache
        self.stream_cache: dict = {}
        # VAD cache
        self.vad_cache: dict = {}

        # Audio buffer of the current (in-progress) utterance, 16k mono float32
        self.utt_audio = np.zeros(0, dtype=np.float32)
        self.utt_start_ms: Optional[int] = None  # wall-clock-ish start of utterance
        self.last_voice_ms: Optional[int] = None
        self.total_ms_received: int = 0

        # Cumulative partial text since last utterance flush
        self.partial_text: str = ""

        # Speaker clusters (per-source). Labels chosen here.
        self.clusters: list[_SpkCluster] = []
        self._next_spk_idx = 0
        self._next_utt_id = 0

        # Streaming model chunk config — Paraformer-zh-streaming is trained on these.
        self.chunk_size = [0, 10, 5]
        self.encoder_chunk_look_back = 4
        self.decoder_chunk_look_back = 1
        # 10 * 60ms = 600ms — required input length per streaming step.
        self._stream_chunk_samples = int(self.SR * self.chunk_size[1] * 60 / 1000)
        self._stream_input_buf = np.zeros(0, dtype=np.float32)

    def _alloc_speaker(self, emb: np.ndarray) -> str:
        # Find best matching cluster.
        best_idx, best_sim = -1, -1.0
        for i, c in enumerate(self.clusters):
            s = _cosine(emb, c.centroid)
            if s > best_sim:
                best_sim, best_idx = s, i
        if best_idx >= 0 and best_sim >= self.spk_threshold:
            c = self.clusters[best_idx]
            # Update centroid (running mean).
            c.centroid = (c.centroid * c.count + emb) / (c.count + 1)
            c.count += 1
            return c.label
        # New speaker.
        label = self._next_speaker_label()
        self.clusters.append(_SpkCluster(label=label, centroid=emb.copy(), count=1))
        return label

    def _next_speaker_label(self) -> str:
        # A, B, ..., Z, AA, AB, ...
        n = self._next_spk_idx
        self._next_spk_idx += 1
        s = ""
        n += 1
        while n > 0:
            n, r = divmod(n - 1, 26)
            s = chr(ord("A") + r) + s
        return f"{self.source_label}-{s}"

    def feed(self, pcm: np.ndarray) -> list[dict]:
        """Feed a chunk of 16kHz mono float32 PCM. Returns a list of events:
              {"type": "partial", "source", "text"}
              {"type": "utterance", "source", "id", "start_ms", "end_ms", "text", "speaker"}
        """
        events: list[dict] = []
        if pcm.size == 0:
            return events
        ms = int(pcm.size * 1000 / self.SR)
        chunk_start_ms = self.total_ms_received
        self.total_ms_received += ms

        # --- 1) Streaming partial: feed exact-size chunks to the streaming model.
        self._stream_input_buf = np.concatenate([self._stream_input_buf, pcm])
        while self._stream_input_buf.shape[0] >= self._stream_chunk_samples:
            step = self._stream_input_buf[: self._stream_chunk_samples]
            self._stream_input_buf = self._stream_input_buf[self._stream_chunk_samples :]
            try:
                res = _streaming_model.generate(
                    input=step,
                    cache=self.stream_cache,
                    is_final=False,
                    chunk_size=self.chunk_size,
                    encoder_chunk_look_back=self.encoder_chunk_look_back,
                    decoder_chunk_look_back=self.decoder_chunk_look_back,
                )
                t = res[0].get("text", "") if res else ""
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                t = ""
                events.append({"type": "error", "source": self.source_key,
                               "message": f"streaming: {e}",
                               "trace": tb.splitlines()[-3:],
                               "shape": str(step.shape), "dtype": str(step.dtype)})
            if t:
                self.partial_text += t
                events.append({"type": "partial", "source": self.source_key,
                               "text": self.partial_text})

        # --- 2) Buffer audio for offline rescoring & VAD.
        if self.utt_start_ms is None:
            self.utt_start_ms = chunk_start_ms
        self.utt_audio = np.concatenate([self.utt_audio, pcm])

        # --- 3) VAD online — emits speech segments incrementally.
        try:
            vad_res = _vad_model.generate(
                input=pcm, cache=self.vad_cache, is_final=False, chunk_size=200,
            )
            segs = vad_res[0].get("value", []) if vad_res else []
        except Exception as e:
            segs = []
            events.append({"type": "error", "source": self.source_key, "message": f"vad: {e}"})

        # VAD values come as [[start_ms, end_ms], ...]; -1 means open boundary.
        # Track most recent confirmed voice activity.
        for s, e in segs:
            if e == -1:
                # still speaking; bump last_voice
                self.last_voice_ms = self.total_ms_received
            else:
                self.last_voice_ms = e

        # --- 4) Decide whether to finalize the utterance.
        utt_len_ms = int(self.utt_audio.size * 1000 / self.SR)
        silence_run_ms = (self.total_ms_received - (self.last_voice_ms or self.utt_start_ms))
        should_finalize = (
            self.last_voice_ms is not None
            and silence_run_ms >= self.silence_ms
            and utt_len_ms >= 500  # don't fire on tiny blips
        ) or utt_len_ms >= self.max_utterance_ms

        if should_finalize:
            events.extend(self._finalize_utterance())

        return events

    def flush(self) -> list[dict]:
        """Force-finalize whatever is buffered. Call when user stops live capture."""
        events: list[dict] = []
        # Push leftover streaming-input bytes as the final chunk so the streaming
        # model emits any remaining partial.
        if self._stream_input_buf.size > 0:
            try:
                res = _streaming_model.generate(
                    input=self._stream_input_buf,
                    cache=self.stream_cache,
                    is_final=True,
                    chunk_size=self.chunk_size,
                    encoder_chunk_look_back=self.encoder_chunk_look_back,
                    decoder_chunk_look_back=self.decoder_chunk_look_back,
                )
                t = res[0].get("text", "") if res else ""
            except Exception:
                t = ""
            if t:
                self.partial_text += t
            self._stream_input_buf = np.zeros(0, dtype=np.float32)
        # Finalize the buffered utterance.
        events.extend(self._finalize_utterance())
        return events

    def _finalize_utterance(self) -> list[dict]:
        events: list[dict] = []
        audio = self.utt_audio
        utt_start = self.utt_start_ms or 0
        utt_end = utt_start + int(audio.size * 1000 / self.SR)
        # Reset utterance buffer & partial state BEFORE we run offline so a new
        # utterance can start accumulating immediately.
        self.utt_audio = np.zeros(0, dtype=np.float32)
        self.utt_start_ms = None
        self.last_voice_ms = None
        self.partial_text = ""

        if audio.size < int(0.3 * self.SR):
            return events  # too short, drop

        # Offline rescoring (full bidirectional context → semantic correction).
        try:
            res = _offline_model.generate(input=audio, batch_size_s=300)
            text = (res[0].get("text") or "").strip() if res else ""
        except Exception as e:
            events.append({"type": "error", "source": self.source_key, "message": f"offline: {e}"})
            return events
        if not text:
            return events

        # Speaker embedding.
        try:
            spk_res = _spk_model.generate(input=audio)
            emb = None
            if spk_res:
                emb = spk_res[0].get("spk_embedding")
                if emb is None:
                    # Some versions store at different key
                    emb = spk_res[0].get("embedding") or spk_res[0].get("value")
            if emb is None:
                speaker = f"{self.source_label}-A"
            else:
                emb = np.asarray(emb, dtype=np.float32).reshape(-1)
                speaker = self._alloc_speaker(emb)
        except Exception:
            speaker = f"{self.source_label}-A"

        uid = self._next_utt_id
        self._next_utt_id += 1
        events.append({
            "type": "utterance",
            "source": self.source_key,
            "id": f"{self.source_key}:{uid}",
            "start_ms": utt_start,
            "end_ms": utt_end,
            "text": text,
            "speaker": speaker,
        })
        return events


# ============================================================================
# Offline processing for uploaded files
# ============================================================================

_file_model = None  # cam++-equipped offline model for full-file processing


def process_file(audio_pcm_16k_mono: np.ndarray, source_label: str = "File",
                 progress_cb=None) -> list[dict]:
    """Run the offline 2-pass model with diarization on a complete audio buffer.
       Returns a list of utterance event dicts (same shape as streaming utterance).
    """
    _ensure_loaded()
    global _file_model
    if _file_model is None:
        from funasr import AutoModel
        _file_model = AutoModel(
            model="paraformer-zh",
            vad_model="fsmn-vad",
            punc_model="ct-punc",
            spk_model="cam++",
            device=os.getenv("STT_DEVICE", "cuda"),
            ncpu=int(os.getenv("STT_NCPU", "4")),
            disable_update=True,
        )

    res = _file_model.generate(
        input=audio_pcm_16k_mono,
        batch_size_s=300,
        sentence_timestamp=True,
    )
    if not res:
        return []
    r0 = res[0]
    # r0 typically has 'sentence_info' = [{start, end, text, spk}, ...]
    out: list[dict] = []
    sentence_info = r0.get("sentence_info") or []
    if not sentence_info:
        # Fallback: single big segment.
        if r0.get("text"):
            out.append({
                "type": "utterance",
                "source": f"file:{source_label}",
                "id": f"file:0",
                "start_ms": 0,
                "end_ms": int(audio_pcm_16k_mono.size * 1000 / 16000),
                "text": r0["text"].strip(),
                "speaker": f"{source_label}-A",
            })
        return out
    # Map spk_id -> speaker label (A, B, C ...) consistent within file.
    spk_map: dict[int, str] = {}
    next_idx = 0
    for i, s in enumerate(sentence_info):
        spk = s.get("spk", 0)
        if spk not in spk_map:
            n = next_idx
            next_idx += 1
            lab = ""
            n += 1
            while n > 0:
                n, r = divmod(n - 1, 26)
                lab = chr(ord("A") + r) + lab
            spk_map[spk] = f"{source_label}-{lab}"
        out.append({
            "type": "utterance",
            "source": f"file:{source_label}",
            "id": f"file:{i}",
            "start_ms": int(s.get("start", 0)),
            "end_ms": int(s.get("end", 0)),
            "text": (s.get("text") or "").strip(),
            "speaker": spk_map[spk],
        })
        if progress_cb:
            progress_cb((i + 1) / len(sentence_info))
    return out
