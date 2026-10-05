"""Jarvis voice server: Whistle (STT) + Chatterbox Nano (TTS) behind a tiny local HTTP API.

Endpoints (127.0.0.1 only):
  GET  /health          -> {"ok": true, "speaking": bool}
  POST /listen          -> records from the default mic until you stop talking, returns {"text": ...}
  POST /speak {"text"}  -> queues text to be spoken (returns immediately)
  POST /wait            -> blocks until the speech queue is empty
  POST /stop            -> stops speaking and clears the queue
"""

import os

os.environ.setdefault("NEEDLE_TELEMETRY", "0")  # cactus-needle sends anonymous usage counts otherwise
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import queue
import re
import sys
import threading
import time
import warnings

warnings.filterwarnings("ignore")

import numpy as np
import sounddevice as sd
import torch
import uvicorn
from fastapi import FastAPI
from huggingface_hub import snapshot_download
from pydantic import BaseModel

import needle
from chatterbox.tts_turbo import ChatterboxTurboTTS

HOST = "127.0.0.1"
PORT = int(os.environ.get("JARVIS_PORT", "8765"))
VOICE = os.environ.get("JARVIS_VOICE")  # optional path to a ~10 s reference .wav to clone
DEVICE = os.environ.get("JARVIS_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")

MIC_SR = 16000
BLOCK = 480  # 30 ms
SILENCE_END_S = float(os.environ.get("JARVIS_SILENCE", "1.0"))  # pause that ends your turn
MIC_THRESHOLD = float(os.environ.get("JARVIS_MIC_THRESHOLD", "0.012"))  # min RMS that counts as speech
NO_SPEECH_TIMEOUT_S = 8.0
MAX_UTTERANCE_S = 29.5  # Whistle handles up to 30 s per pass

print(f"[jarvis] loading Chatterbox Nano on {DEVICE} ...", flush=True)
# Same files from_pretrained() fetches, minus the 1 GB non-meanflow vocoder that Nano never loads.
nano_dir = snapshot_download(
    "ResembleAI/chatterbox-nano",
    allow_patterns=["*.safetensors", "*.json", "*.txt", "*.pt", "*.model"],
    ignore_patterns=["s3gen.safetensors"],
)
tts = ChatterboxTurboTTS.from_local(nano_dir, DEVICE, nano=True)
if VOICE:
    tts.prepare_conditionals(VOICE)
tts.generate("Ready.")  # warm-up: the first generation is much slower
print("[jarvis] loading Whistle ...", flush=True)
needle.transcribe(np.zeros(MIC_SR, dtype=np.float32))
print(f"[jarvis] models ready on {DEVICE}", flush=True)
if "--check" in sys.argv:  # used by setup.ps1: download + smoke-test the models, then exit
    sys.exit(0)

app = FastAPI()
speech_q: "queue.Queue[tuple[int, str]]" = queue.Queue()
generation = 0  # bumped by /stop so queued/in-flight sentences are dropped
pending = 0  # sentences queued but not yet played (or dropped)
pending_lock = threading.Lock()
mic_lock = threading.Lock()


# ---------------------------------------------------------------- TTS

def clean_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links/images -> label
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"^\s{0,3}(#{1,6}|[-*+]|\d+\.|>)\s+", "", text, flags=re.M)
    text = re.sub(r"[*_~|#]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text: str, max_len: int = 220) -> list[str]:
    parts = re.split(r"(?<=[.!?;:])\s+", text)
    out = []
    for p in parts:
        while len(p) > max_len:  # very long sentence: break at a comma/space
            cut = max(p.rfind(",", 0, max_len), p.rfind(" ", 0, max_len))
            cut = cut if cut > 40 else max_len
            out.append(p[:cut + 1].strip())
            p = p[cut + 1:]
        if p.strip():
            out.append(p.strip())
    return out


def done_one():
    global pending
    with pending_lock:
        pending = max(0, pending - 1)


def speaker_loop():
    """Generates sentence N+1 while sentence N is playing."""
    play_q: "queue.Queue[tuple[int, np.ndarray]]" = queue.Queue(maxsize=2)

    def player():
        # One long-lived stream owned by this thread; stopping = noticing `generation` changed
        # between 50 ms chunks (calling sd.stop() from another thread crashes PortAudio on Windows).
        chunk = tts.sr // 20
        with sd.OutputStream(samplerate=tts.sr, channels=1, dtype="float32") as out:
            while True:
                gen, wav = play_q.get()
                for i in range(0, len(wav), chunk):
                    if gen != generation:
                        break
                    out.write(wav[i:i + chunk].astype(np.float32).reshape(-1, 1))
                done_one()

    threading.Thread(target=player, daemon=True).start()
    while True:
        gen, sentence = speech_q.get()
        if gen != generation:
            done_one()
            continue
        try:
            with torch.inference_mode():
                wav = tts.generate(sentence).squeeze(0).cpu().numpy()
        except Exception as e:  # never let one bad sentence kill the loop
            print(f"[jarvis] TTS error: {e}", flush=True)
            done_one()
            continue
        play_q.put((gen, wav))


threading.Thread(target=speaker_loop, daemon=True).start()


# ---------------------------------------------------------------- STT

def record_utterance() -> np.ndarray:
    """Simple energy VAD: calibrate on ambient noise, start on speech, stop after a pause."""
    frames: list[np.ndarray] = []
    pre_roll: list[np.ndarray] = []
    started = False
    silence = 0.0
    t0 = time.time()
    noise = []

    with sd.InputStream(samplerate=MIC_SR, channels=1, dtype="float32", blocksize=BLOCK) as stream:
        while True:
            block, _ = stream.read(BLOCK)
            block = block[:, 0].copy()
            rms = float(np.sqrt(np.mean(block ** 2)))
            elapsed = time.time() - t0

            if len(noise) < 10:  # first 300 ms -> noise floor
                noise.append(rms)
                continue
            threshold = max(MIC_THRESHOLD, 3.0 * float(np.median(noise)))

            if not started:
                pre_roll = (pre_roll + [block])[-10:]
                if rms > threshold:
                    started = True
                    frames = pre_roll[:]
                elif elapsed > NO_SPEECH_TIMEOUT_S:
                    return np.zeros(0, dtype=np.float32)
                continue

            frames.append(block)
            silence = silence + BLOCK / MIC_SR if rms < threshold else 0.0
            if silence >= SILENCE_END_S or len(frames) * BLOCK / MIC_SR >= MAX_UTTERANCE_S:
                break

    audio = np.concatenate(frames)
    peak = float(np.max(np.abs(audio))) or 1.0
    return (audio / peak * 0.9).astype(np.float32)


# ---------------------------------------------------------------- API

class SpeakBody(BaseModel):
    text: str


@app.get("/health")
def health():
    return {"ok": True, "speaking": pending > 0, "device": DEVICE}


@app.post("/listen")
def listen():
    stop()  # don't transcribe Jarvis's own voice
    with mic_lock:
        audio = record_utterance()
    if audio.size < MIC_SR * 0.3:
        return {"text": ""}
    result = needle.transcribe(audio)
    print(f"[jarvis] heard: {result['text']!r}", flush=True)
    return {"text": result["text"].strip()}


@app.post("/speak")
def speak(body: SpeakBody):
    sentences = split_sentences(clean_for_speech(body.text))
    global pending
    with pending_lock:
        pending += len(sentences)
    for s in sentences:
        speech_q.put((generation, s))
    return {"queued": len(sentences)}


@app.post("/wait")
def wait():
    while pending > 0:
        time.sleep(0.05)
    return {"ok": True}


@app.post("/stop")
def stop():
    global generation
    generation += 1  # queued, in-flight and playing sentences are dropped by the loops
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
