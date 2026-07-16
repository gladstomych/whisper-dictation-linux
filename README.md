# whisper-dictation

Offline Whisper-based dictation for KDE Plasma on Wayland.

Press a hotkey to start recording. Speak. Press the hotkey again and
Whisper transcribes what you said, then types the result into whatever
window currently has keyboard focus — your terminal prompt, editor,
chat input, browser bar, anything.

## Why

We like Whisper's accuracy on natural, conversational speech, and it
produces punctuated, capitalized text out of the box — no post-processing
needed. The trade-off is batch-mode transcription: text is produced after
you finish speaking, not streamed live as you talk.

## Scope

Supported:

- **Session**: KDE Plasma on Wayland (OSD indicators are KDE-specific;
  transcription itself works on any desktop that has `ydotool`/`parec`).
- **Distros**: Fedora (`dnf`), Debian/Ubuntu (`apt`). The installer
  auto-detects; on other distros install `ydotool` and `pipewire-pulse`
  manually first.

Not supported:

- **X11**: not tested. `ydotool` works there in principle, but `wtype`
  or `xdotool` are typically more appropriate.
- **GNOME/other compositors**: transcription works; OSD is silently
  skipped (not an error). Add a `gdbus`/`notify-send` wrapper if needed.

## Install

```sh
git clone <this repo> ~/dev/whisper-dictation
cd ~/dev/whisper-dictation
./install.sh install
```

Then log out + back in (so the `input` group applies) and bind a KDE
global shortcut to:

```
~/.local/bin/whisper-toggle
```

(System Settings → Keyboard → Shortcuts → Custom → Command/URL.)

## Use

1. Focus any text field — a terminal prompt, an editor, a chat input,
   a browser URL bar, anything that accepts typed input.
2. Press your hotkey. A 🔴 OSD appears and audio recording starts.
3. Speak naturally.
4. Press the hotkey again. Recording stops, Whisper transcribes the
   audio, and the resulting text is typed into your focused input
   buffer — exactly as if you had typed it yourself.

Typical latency from "second hotkey press" to "text appears" is 1–3
seconds on a modern CPU with the default `small` model.

## Configuration

Environment variables (set in your shell or the systemd unit):

| Variable          | Default | Values                                   |
|-------------------|---------|------------------------------------------|
| `WHISPER_BACKEND` | `local` | `local` `cloud`                          |
| `WHISPER_MODEL`   | `small` | `tiny` `base` `small` `medium` `large-v3`|
| `WHISPER_DEVICE`  | `cpu`   | `cpu` `cuda`                             |
| `WHISPER_LANG`    | `en`    | ISO 639-1 code                           |
| `WHISPER_COOKIE`  | `/tmp/whisper-dictation.cookie` | Override cookie path        |
| `WHISPER_AUDIO`   | `/tmp/whisper-dictation-audio.raw` | Override temp audio path  |
| `WHISPER_DEBUG`   | *(off)* | `1` to archive each session's audio + transcript |
| `WHISPER_DEBUG_DIR` | `~/.cache/whisper-dictation` | Where debug artifacts are written |

`WHISPER_MODEL`/`WHISPER_DEVICE` apply to the `local` backend only.

### Cloud backend (OpenAI)

Set `WHISPER_BACKEND=cloud` to transcribe with OpenAI's hosted models
instead of a local Whisper. This is more accurate (especially on accents,
noise, and proper nouns) at the cost of network latency and per-request
billing — and, of course, your audio leaves the machine. The local
backend stays the default; cloud is fully opt-in.

| Variable                  | Default             | Notes                                     |
|---------------------------|---------------------|-------------------------------------------|
| `OPENAI_API_KEY`          | *(required)*        | Your OpenAI API key                       |
| `OPENAI_TRANSCRIBE_MODEL` | `gpt-4o-transcribe` | `gpt-4o-transcribe` `gpt-4o-mini-transcribe` `whisper-1` |
| `OPENAI_BASE_URL`         | `https://api.openai.com/v1` | For proxies / compatible endpoints |
| `WHISPER_HTTP_TIMEOUT`    | `300`               | API request timeout, seconds              |

**Length limit:** OpenAI caps uploads at 25 MB. Since audio is sent as
uncompressed 16 kHz mono WAV (~1.9 MB/min), that's about **13 minutes** per
recording. Critically, the API does *not* reject an over-limit file — it
returns a *silently truncated* transcript — so whisper-dictation checks the
size itself and shows `⚠ Recording too long — split it` instead of letting
you lose the tail. Keep individual dictations under ~13 min on the cloud
backend.

`gpt-4o-transcribe` is the most accurate; `gpt-4o-mini-transcribe` is
cheaper and slightly less accurate; `whisper-1` is the original API model.
No extra Python dependencies are needed — the cloud path uses only the
standard library.

Because the KDE global shortcut runs `whisper-toggle` with the session
environment (not your interactive shell), export the key where that
environment is set — e.g. add to `~/.config/environment.d/whisper.conf`:

```ini
WHISPER_BACKEND=cloud
OPENAI_API_KEY=sk-...
```

then log out and back in. (Testing from a terminal, a normal `export`
in your shell is enough.)

### Model sizes (approximate, English)

| Model     | Size   | RAM    | Relative speed | Accuracy   |
|-----------|--------|--------|----------------|------------|
| `tiny`    | 39 M   | ~300 M | fastest        | low        |
| `base`    | 74 M   | ~600 M | fast           | decent     |
| `small`   | 244 M  | ~1.2 G | medium         | good       |
| `medium`  | 769 M  | ~2.8 G | slow           | very good  |
| `large-v3`| 1.5 B  | ~5 G   | slowest        | best       |

For CPU-only use, `base` or `small` is the sweet spot. If you have an
NVIDIA GPU, set `WHISPER_DEVICE=cuda` and you can run `medium` or
`large-v3` in real time.

## Feedback & errors

Because the hotkey runs `whisper-toggle` with no terminal, the KDE OSD is
your only feedback. Failures show a specific message so you can tell them
apart at a glance:

| OSD                              | Meaning                                        |
|----------------------------------|------------------------------------------------|
| `⚫ No speech detected`           | Transcription ran but produced no text          |
| `⚫ Too little audio`             | You toggled off almost immediately              |
| `⚠ No OpenAI API key set`        | `WHISPER_BACKEND=cloud` but no `OPENAI_API_KEY` |
| `⚠ Invalid OpenAI API key`       | Key rejected (401)                              |
| `⚠ Unknown model: …`             | `OPENAI_TRANSCRIBE_MODEL` not recognized        |
| `⚠ OpenAI quota exceeded`        | Billing/quota exhausted                         |
| `⚠ OpenAI rate limited — retry`  | Too many requests; try again                     |
| `⚠ Cannot reach OpenAI (network?)` | DNS/connection failure                        |
| `⚠ Typing failed (is ydotoold running?)` | `ydotool` couldn't inject the text     |

Full detail for any of these is written to stderr — run `whisper-toggle`
from a terminal to see it when debugging.

## How it works

```
┌─────────────┐  raw PCM   ┌──────────────┐  text   ┌──────────┐
│   parec     ├───────────▶│  faster-     ├────────▶│ ydotool  │
│ (recorder)  │  16k s16   │  whisper     │         │ (typing) │
└─────────────┘            └──────────────┘         └──────────┘
       ▲                          ▲
       │ subprocess               │ numpy float32
       │                          │
┌──────┴──────────────────────────┴────────┐
│        whisper-toggle (Python)           │
│  - manages cookie file for toggle        │
│  - shows KDE OSD via qdbus (auto-detect) │
└──────────────────────────────────────────┘
```

With `WHISPER_BACKEND=cloud`, the middle box is swapped for a call to the
OpenAI transcription API (the captured PCM is wrapped in a WAV container
and POSTed); the recorder and typing stages are unchanged.

The toggle state is tracked via a cookie file containing the recorder's
PID. A dead PID is treated as "not running" so a crashed session can
never lock you out.

## Uninstall

```sh
./install.sh uninstall
```

Removes everything except the Whisper model cache (asks first).

## Credits

The toggle-driven, hotkey-activated dictation UX is inspired by
[`nerd-dictation`](https://github.com/ideasman42/nerd-dictation) by
Campbell Barton — worth checking out if you want streaming (live) results
from VOSK rather than Whisper's batch transcription.

This project builds on:

- [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) — the
  Whisper inference engine (CTranslate2-backed).
- [Whisper](https://github.com/openai/whisper) — OpenAI's speech
  recognition model.
- [`ydotool`](https://github.com/ReimuNotMoe/ydotool) — Wayland-native
  input simulation via uinput.
- [PipeWire](https://pipewire.org/) / `parec` — audio capture.

## License

GPL-3.0-or-later.
