# whisper-dictation

Offline Whisper-based dictation for KDE Plasma on Wayland.

Press a hotkey to start recording, speak, press it again — Whisper
transcribes and types the text into whatever window has focus. Optionally
uses OpenAI's hosted models instead of a local Whisper. Transcription is
batch-mode: text appears after you stop speaking, not streamed live.

## Requirements

- **KDE Plasma on Wayland.** The on-screen indicators are KDE-specific;
  transcription and typing work on any desktop with `ydotool` + `parec`
  (the OSD is silently skipped elsewhere).
- **Fedora (`dnf`) or Debian/Ubuntu (`apt`)** for the auto-installer. On
  other distros, install `ydotool` and `pipewire-pulse` yourself first.
- X11 is untested (`ydotool` works there, but `wtype`/`xdotool` fit better).

## Install

```sh
git clone <this repo> ~/dev/whisper-dictation
cd ~/dev/whisper-dictation
./setup.sh install            # local, offline backend (default)
./setup.sh install --cloud    # OpenAI backend instead (no local model)
```

Then **log out and back in** (for the `input` group), and bind a KDE global
shortcut (System Settings → Keyboard → Shortcuts → Custom → Command/URL) to:

```
~/.local/bin/whisper-toggle
```

## Use

Focus a text field, press your hotkey (a 🔴 indicator stays on screen while
recording), speak, then press it again. The indicator switches to ⏳ while
transcribing and the text is typed into the focused window. Typical latency
is 1–3 s on a modern CPU with the default `small` model.

## Configuration

All configuration is via environment variables. For the hotkey, set them in
`~/.config/environment.d/whisper.conf` (read at login) — not your shell rc,
which the KDE shortcut doesn't see.

| Variable          | Default | Values                                   |
|-------------------|---------|------------------------------------------|
| `WHISPER_BACKEND` | `local` | `local` `cloud`                          |
| `WHISPER_MODEL`   | `small` | `tiny` `base` `small` `medium` `large-v3` (local only) |
| `WHISPER_DEVICE`  | `cpu`   | `cpu` `cuda` (local only)                |
| `WHISPER_LANG`    | `en`    | ISO 639-1 code                           |
| `WHISPER_DEBUG`   | *(off)* | `1` archives each session's audio + transcript |
| `WHISPER_DEBUG_DIR` | `~/.cache/whisper-dictation` | Debug artifact directory |

`WHISPER_DEBUG=1` writes a `.wav` + `.txt` per session and never prunes them
— leave it off for normal use, and clear the directory when done.

### Switching backends

```sh
./setup.sh backend local              # offline Whisper
./setup.sh backend cloud              # OpenAI
./setup.sh model whisper-1            # pick the cloud model (see below)
```

`backend` sets `WHISPER_BACKEND` and `model` sets `OPENAI_TRANSCRIBE_MODEL` in `whisper.conf` (preserving your key config).
**Log out and back in** for the hotkey to pick it up. To test in a terminal
without logging out, set it in the shell — but for *both* toggle presses,
since the second (stop) press is what transcribes:

```fish
set -gx WHISPER_BACKEND cloud
```

### Cloud backend (OpenAI)

More accurate on accents, noise, and proper nouns, at the cost of network
latency, per-request billing, and sending your audio off the machine.

| Variable                  | Default             | Notes                          |
|---------------------------|---------------------|--------------------------------|
| `OPENAI_TRANSCRIBE_MODEL` | `gpt-4o-mini-transcribe` | also `whisper-1`; **avoid** plain `gpt-4o-transcribe` (see below) |
| `OPENAI_TRANSCRIBE_PROMPT`| *(verbatim prompt)* | Anti-omission steer; set `""` to disable |
| `OPENAI_BASE_URL`         | `https://api.openai.com/v1` | For proxies / compatible endpoints |
| `WHISPER_HTTP_TIMEOUT`    | `300`               | Request timeout, seconds       |
| `WHISPER_HTTP_RETRIES`    | `2`                 | Retries on transient 429 / 5xx |

The cloud path uses only the standard library — no extra dependencies.
Uploads are capped at 25 MB (~13 min of audio); over that OpenAI silently
truncates, so whisper-dictation refuses and shows `⚠ Recording too long`.

> **Model choice matters.** Plain `gpt-4o-transcribe` reliably *drops the last
> words* of short/abrupt clips — a widely reported, unfixed defect — which is
> exactly the shape of dictation. The default is `gpt-4o-mini-transcribe`
> (complete and cheaper here); `whisper-1` is the most truncation-resistant
> fallback. A verbatim prompt + `temperature=0` are sent to further discourage
> omission and silence hallucinations.

**API key.** Resolved at transcription time, first match wins:

1. `OPENAI_API_KEY` — plaintext env var. Readable by every process you run
   (`/proc/PID/environ`), so fine for a terminal test, not the hotkey.
2. `OPENAI_API_KEY_CMD` — a command that prints the key, e.g.
   `OPENAI_API_KEY_CMD=pass show openai/api`.
3. `secret-tool` (libsecret / KWallet) — the recommended default. Store once
   and put nothing but `WHISPER_BACKEND=cloud` in `whisper.conf`:

   ```sh
   ./setup.sh set-key    # hidden prompt → stores the key in KWallet
   ```

   Override the lookup attributes with `WHISPER_SECRET_TOOL_ATTRS` (default
   `service openai-api-key`) if you stored it differently.

### Model sizes (local, approximate, English)

| Model     | Size   | RAM    | Speed    | Accuracy   |
|-----------|--------|--------|----------|------------|
| `tiny`    | 39 M   | ~300 M | fastest  | low        |
| `base`    | 74 M   | ~600 M | fast     | decent     |
| `small`   | 244 M  | ~1.2 G | medium   | good       |
| `medium`  | 769 M  | ~2.8 G | slow     | very good  |
| `large-v3`| 1.5 B  | ~5 G   | slowest  | best       |

`base`/`small` are the CPU sweet spot; with `WHISPER_DEVICE=cuda` on an
NVIDIA GPU you can run `medium`/`large-v3` in real time.

## Errors

The hotkey has no terminal, so failures surface as a specific OSD:

| OSD                              | Meaning                                        |
|----------------------------------|------------------------------------------------|
| `⚫ No speech detected`           | Transcription ran but produced no text          |
| `⚫ Too little audio`             | Toggled off almost immediately                  |
| `⚠ No OpenAI API key found`      | Cloud backend, no key from env / cmd / secret-tool |
| `⚠ Invalid OpenAI API key`       | Key rejected (401)                              |
| `⚠ OpenAI quota exceeded`        | Billing/quota exhausted                         |
| `⚠ OpenAI rate limited — retry`  | Too many requests                               |
| `⚠ Cannot reach OpenAI (network?)` | DNS/connection failure                        |
| `⚠ Typing failed (is ydotoold running?)` | `ydotool` couldn't inject the text     |

Full detail goes to stderr — run `whisper-toggle` from a terminal to see it.

## How it works

`parec` captures 16 kHz mono PCM to a temp file; on the second toggle,
faster-whisper (or the OpenAI API, for `cloud`) transcribes it and `ydotool`
types the result. Toggle state is a cookie file holding the recorder's PID —
a dead PID reads as "not running", so a crash can never lock you out.

## Uninstall

```sh
./setup.sh uninstall    # keeps the Whisper model cache (asks first)
```

## Credits & license

UX inspired by [`nerd-dictation`](https://github.com/ideasman42/nerd-dictation).
Built on [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper),
[Whisper](https://github.com/openai/whisper),
[`ydotool`](https://github.com/ReimuNotMoe/ydotool), and
[PipeWire](https://pipewire.org/)/`parec`.

GPL-3.0-or-later.
