"""Talk to Microsoft 365 Copilot by voice.

Whistle (STT) + Chatterbox Nano (TTS) run locally via server.py; questions go to Microsoft 365 Copilot
through the Work IQ Chat API (https://workiq.svc.cloud.microsoft) as the signed-in work account.

Setup: copy config.example.json to config.json and fill in tenant_id / client_id (see README).
Run:   .venv\\Scripts\\python jarvis_m365.py [--hands-free]
"""

import argparse
import atexit
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import msal
import requests
import tzlocal

ROOT = Path(__file__).resolve().parent
WORKIQ = os.environ.get("JARVIS_WORKIQ_URL", "https://workiq.svc.cloud.microsoft/rest")
SCOPES = ["api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask"]
CACHE_FILE = Path(os.environ.get("LOCALAPPDATA", ROOT)) / "Jarvis" / "msal_cache.bin"


# ---------------------------------------------------------------- voice server

class Voice:
    def __init__(self, port: int):
        self.url = f"http://127.0.0.1:{port}"
        self.proc = None
        if not self._healthy():
            print("Loading voice models...", flush=True)
            log = open(ROOT / "voice_server.log", "w", encoding="utf-8")
            self.proc = subprocess.Popen(
                [sys.executable, "-u", str(ROOT / "server.py")], cwd=ROOT,
                env={**os.environ, "JARVIS_PORT": str(port)}, stdout=log, stderr=subprocess.STDOUT,
            )
            atexit.register(self.proc.terminate)
            for _ in range(600):
                if self._healthy():
                    break
                if self.proc.poll() is not None:
                    sys.exit(f"Voice server crashed, see {ROOT / 'voice_server.log'}")
                time.sleep(0.5)
            else:
                sys.exit("Voice server did not start in time.")

    def _healthy(self) -> bool:
        try:
            return requests.get(self.url + "/health", timeout=1).ok
        except requests.RequestException:
            return False

    def post(self, route, body=None, timeout=60):
        r = requests.post(self.url + route, json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def listen(self) -> str:
        return self.post("/listen")["text"]

    def say(self, text: str):
        if text.strip():
            self.post("/speak", {"text": text})

    def wait(self):
        self.post("/wait", timeout=600)

    def stop(self):
        self.post("/stop")


class Speaker:
    """Speaks streamed markdown sentence by sentence, skipping fenced code blocks."""

    def __init__(self, voice: Voice):
        self.voice, self.buf, self.in_fence = voice, "", False

    def push(self, chunk: str):
        self.buf += chunk
        while True:
            fence = self.buf.find("```")
            if self.in_fence:
                if fence < 0:
                    self.buf = self.buf[-2:]
                    return
                eol = self.buf.find("\n", fence)
                self.buf = "" if eol < 0 else self.buf[eol + 1:]
                self.in_fence = False
                continue
            if fence >= 0:
                self.voice.say(self.buf[:fence])
                self.voice.say("I've put the code on screen.")
                self.buf, self.in_fence = self.buf[fence + 3:], True
                continue
            ends = list(re.finditer(r"[.!?](?=\s)|\n", self.buf))
            if ends:
                end = ends[-1].end()
                self.voice.say(self.buf[:end])
                self.buf = self.buf[end:]
            return

    def flush(self):
        if not self.in_fence:
            self.voice.say(self.buf)
        self.buf = ""


# ---------------------------------------------------------------- Microsoft 365 Copilot

def clean_copilot_text(text: str) -> str:
    text = re.sub(r"\[\^\d+\^\]", "", text)  # citation markers
    return re.sub(r"</?(Event|Person|File|Email|Meeting|Message)>", "", text)  # entity tags


class Copilot:
    def __init__(self, cfg: dict):
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.cache = msal.SerializableTokenCache()
        if CACHE_FILE.exists():
            self.cache.deserialize(CACHE_FILE.read_text())
        self.app = msal.PublicClientApplication(
            cfg["client_id"],
            authority=f"https://login.microsoftonline.com/{cfg['tenant_id']}",
            token_cache=self.cache,
            enable_broker_on_windows=True,  # Windows sign-in (WAM): SSO + device compliance on managed laptops
        )
        self.time_zone = cfg.get("time_zone") or tzlocal.get_localzone_name()
        self.conversation_id = None

    def token(self) -> str:
        result = None
        accounts = self.app.get_accounts()
        if accounts:
            result = self.app.acquire_token_silent(SCOPES, account=accounts[0])
        if not result:
            try:
                result = self.app.acquire_token_interactive(
                    SCOPES, parent_window_handle=self.app.CONSOLE_WINDOW_HANDLE, prompt="select_account")
            except Exception as e:  # broker unavailable: fall back to a device code
                print(f"(interactive sign-in failed: {e}; using device code)")
                flow = self.app.initiate_device_flow(scopes=SCOPES)
                print(flow["message"], flush=True)
                result = self.app.acquire_token_by_device_flow(flow)
        if self.cache.has_state_changed:
            CACHE_FILE.write_text(self.cache.serialize())
        if "access_token" not in result:
            sys.exit(f"Sign-in failed: {result.get('error')}: {result.get('error_description')}")
        return result["access_token"]

    def _headers(self):
        return {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}

    @staticmethod
    def _check(r: requests.Response):
        if r.status_code in (401, 403):
            raise RuntimeError(
                f"Microsoft 365 rejected the request ({r.status_code}): {r.text[:300]}\n"
                "Usually this means Work IQ isn't enabled for your tenant, admin consent for "
                "WorkIQAgent.Ask is missing, or your account has no Copilot license/credits. See README.")
        r.raise_for_status()

    def new_conversation(self):
        r = requests.post(f"{WORKIQ}/conversations", headers=self._headers(), json={}, timeout=30)
        self._check(r)
        self.conversation_id = r.json()["id"]

    def ask(self, prompt: str):
        """Yields the reply text incrementally (deltas)."""
        if not self.conversation_id:
            self.new_conversation()
        body = {"message": {"text": prompt}, "locationHint": {"timeZone": self.time_zone}}
        r = requests.post(f"{WORKIQ}/conversations/{self.conversation_id}/chatOverStream",
                          headers={**self._headers(), "Accept": "text/event-stream"},
                          json=body, stream=True, timeout=(30, 300))
        self._check(r)
        yield from self._deltas(iter_sse(r.iter_lines(decode_unicode=True)), prompt)

    @staticmethod
    def _deltas(events, prompt: str):
        # Each event is the whole conversation so far; the raw reply text grows with each update.
        # Hold back a trailing unfinished tag/citation ("</Even", "[^1") so cleaning never splits one.
        sent = ""
        for event in events:
            replies = [m.get("text") or "" for m in event.get("messages", [])]
            replies = [t for t in replies if t.strip() != prompt.strip()]
            if not replies:
                continue
            text = replies[-1]
            if not text.startswith(sent):
                continue
            stable = re.sub(r"(<[^>]*|\[\^[^\]]*)$", "", text)
            if len(stable) > len(sent):
                delta, sent = stable[len(sent):], stable
                yield clean_copilot_text(delta)


def iter_sse(lines):
    data = []
    for line in lines:
        if line is None:
            continue
        if line == "":
            if data:
                try:
                    yield json.loads("\n".join(data))
                except json.JSONDecodeError:
                    pass
            data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        try:
            yield json.loads("\n".join(data))
        except json.JSONDecodeError:
            pass


# ---------------------------------------------------------------- main loop

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hands-free", action="store_true", help="listen again after each answer")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    cfg_path = ROOT / "config.json"
    if not cfg_path.exists():
        sys.exit(f"Missing {cfg_path}. Copy config.example.json to config.json and fill it in (see README).")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    copilot = Copilot(cfg)
    copilot.token()  # sign in up front, not mid-conversation
    voice = Voice(args.port)

    print("\nJarvis is ready. Press Enter and speak, type a question, 'new' for a new chat, or 'q' to quit.")
    while True:
        if args.hands_free:
            typed = ""
        else:
            try:
                typed = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
        if typed.lower() in ("q", "quit", "exit"):
            break
        if typed.lower() == "new":
            copilot.conversation_id = None
            print("Started a new conversation.")
            continue

        prompt = typed
        if not prompt:
            print("Listening...", flush=True)
            prompt = voice.listen()
            if not prompt:
                print("(didn't hear anything)")
                if args.hands_free:
                    break  # silence ends hands-free mode
                continue
            print(f"You: {prompt}")

        voice.stop()
        speaker = Speaker(voice)
        print("Copilot: ", end="", flush=True)
        try:
            for delta in copilot.ask(prompt):
                print(delta, end="", flush=True)
                speaker.push(delta)
        except KeyboardInterrupt:
            voice.stop()
            print("\n(interrupted)")
            continue
        except (RuntimeError, requests.RequestException) as e:
            print(f"\n{e}")
            continue
        speaker.flush()
        print()
        if args.hands_free:
            try:
                voice.wait()
            except KeyboardInterrupt:
                voice.stop()
                break


if __name__ == "__main__":
    main()
