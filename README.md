# Jarvis: talk to Copilot by voice

Two front ends share one local voice server (`server.py`):

| | Talks to | Run |
|---|---|---|
| `jarvis_m365.py` | **Microsoft 365 Copilot** (Work IQ Chat API, your work account) | `Jarvis.bat` (add `--hands-free` to keep listening) |
| `vscode-jarvis/` | GitHub Copilot Chat in VS Code | `Ctrl+Alt+J` in VS Code |

## Microsoft 365 Copilot

Console app: press **Enter** and speak (or type a question), `new` starts a new chat, `q` quits, `Ctrl+C` interrupts an answer.
The reply prints and is read aloud while it streams. Sign-in uses the Windows account broker, so on a managed laptop it's usually just SSO.

**One-time IT setup (needs a tenant admin; you can't do this yourself):**
1. Enable the Work IQ API in the tenant (creates its service principal).
2. Register an Entra app: *Mobile and desktop* platform with redirect URI
   `ms-appx-web://microsoft.aad.brokerplugin/<client-id>` and `http://localhost`, and **Allow public client flows = Yes**.
3. Add the delegated permission **Work IQ API → `WorkIQAgent.Ask`** and grant **admin consent**.
4. Your account needs Microsoft 365 Copilot access. Work IQ usage is metered in Copilot Credits.

Then copy `config.example.json` to `config.json` with the tenant ID and the app's client ID. Neither is a secret.
A 401/403 error almost always means one of steps 1 to 4 is missing.
Docs: https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/rest/copilotroot-post-conversations

# GitHub Copilot Chat (VS Code)

- **STT:** [Whistle](https://huggingface.co/Cactus-Compute/whistle) (16.9 MB, `cactus-needle`)
- **TTS:** [Chatterbox Nano](https://huggingface.co/ResembleAI/chatterbox-nano) (110M params)
- **LLM:** whichever Copilot model is selected in the VS Code chat model picker

```
mic -> server.py (/listen, Whistle) -> @jarvis participant -> Copilot model
                                                   |
speakers <- server.py (/speak, Chatterbox Nano) <--+  (sentence by sentence while streaming)
```

## Use (VS Code)

- `Ctrl+Alt+J`: push to talk. Opens Copilot Chat, sends `@jarvis /talk`, listens until you pause, then answers out loud.
- `Ctrl+Alt+K`: stop speaking.
- You can also type `@jarvis <question>` (the answer is read aloud too) and attach `#file` context as usual.
- Code blocks show in chat but aren't read aloud.

Settings (`jarvis.*`): `handsFree` (keep listening after each reply), `speakTypedPrompts`, `port`, `serverDir`.
Env vars for `server.py`: `JARVIS_VOICE=path\to\10s_clip.wav` (voice cloning), `JARVIS_SILENCE` (seconds of pause that end your turn, default 1.0),
`JARVIS_MIC_THRESHOLD` (default 0.012, lower it if Jarvis doesn't hear you), `JARVIS_DEVICE` (`cuda`/`cpu`).
Server logs are in the **Jarvis Voice** output panel.

## Install (any Windows PC; GPU optional)

Needs Python 3.12 (with the `py` launcher) and Git. Then, in the repo folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1          # add -VSCode for the VS Code extension, -Cpu to force CPU
```

It creates `.venv`, installs CPU or CUDA PyTorch depending on whether an NVIDIA GPU is present, installs pinned packages
from `requirements.txt` plus Chatterbox (from GitHub, which has Nano support that the PyPI release lacks), downloads
the models (Chatterbox Nano ~1.85 GB and Whistle ~17 MB, cached in `%USERPROFILE%\.cache\huggingface`), test-runs them, and creates `config.json`.
Total disk use is about 4 GB on CPU.

Without a GPU, everything runs on the CPU. On a desktop i7, Chatterbox Nano speaks about 2x faster than real time (8 threads)
and 1.5x (4 threads). Expect roughly real time on a thin laptop. The next sentence is generated while the current one plays.
