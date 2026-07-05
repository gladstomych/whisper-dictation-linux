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
seconds on a modern CPU with the default `base` model.

## Configuration

Environment variables (set in your shell or the systemd unit):

| Variable          | Default | Values                                   |
|-------------------|---------|------------------------------------------|
| `WHISPER_MODEL`   | `base`  | `tiny` `base` `small` `medium` `large-v3`|
| `WHISPER_DEVICE`  | `cpu`   | `cpu` `cuda`                             |
| `WHISPER_LANG`    | `en`    | ISO 639-1 code                           |
| `WHISPER_COOKIE`  | `/tmp/whisper-dictation.cookie` | Override cookie path        |
| `WHISPER_AUDIO`   | `/tmp/whisper-dictation-audio.raw` | Override temp audio path  |

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
