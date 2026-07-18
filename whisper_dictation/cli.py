"""
Whisper-based dictation toggle.

Press once to start recording, again to stop and transcribe.
Uses faster-whisper for transcription, ydotool for input simulation,
and the KDE OSD (via qdbus) for visual feedback.

Configuration via environment variables:
  WHISPER_BACKEND          local | cloud                  (default: local)
  WHISPER_MODEL            tiny | base | small | medium | large-v3   (default: small)
  WHISPER_DEVICE           cpu | cuda                      (default: cpu)
  WHISPER_LANG             en | fr | de | ...              (default: en)
  WHISPER_COOKIE           path to cookie file             (default: /tmp/whisper-dictation.cookie)
  WHISPER_AUDIO            path to raw PCM temp file        (default: /tmp/whisper-dictation-audio.raw)
  WHISPER_DEBUG            1 to archive each session's audio + transcript (default: off)
  WHISPER_DEBUG_DIR        where debug artifacts go        (default: ~/.cache/whisper-dictation)

Cloud backend (WHISPER_BACKEND=cloud) uses the OpenAI transcription API:
  OPENAI_TRANSCRIBE_MODEL  gpt-4o-transcribe | gpt-4o-mini-transcribe | whisper-1
                                                           (default: gpt-4o-transcribe)
  OPENAI_BASE_URL          override API base URL           (default: https://api.openai.com/v1)
  WHISPER_HTTP_TIMEOUT     API request timeout, seconds    (default: 300)
  WHISPER_HTTP_RETRIES     retries on transient 429/5xx    (default: 2)

The API key is resolved at call time, in order (see _get_api_key):
  1. OPENAI_API_KEY          plaintext env var (terminal testing; wins if set)
  2. OPENAI_API_KEY_CMD      shell command that prints the key (e.g. pass/gopass)
  3. secret-tool (libsecret / KWallet), on WHISPER_SECRET_TOOL_ATTRS
                                         (default: "service openai-api-key")
Prefer 2 or 3 over 1: an exported key is readable by every process you run.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    """Read an int-valued env var, falling back to ``default`` on junk.

    These are parsed at import time, so a bad value must NOT raise: the KDE
    shortcut runs whisper-toggle with no terminal, and a ValueError here would
    abort before any OSD, leaving the toggle silently dead on both press and
    release.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        sys.stderr.write(
            f"whisper-toggle: {name}={raw!r} is not an integer; using {default}\n"
        )
        return default


_TMP = os.environ.get("TMPDIR", "/tmp")
COOKIE_PATH = Path(os.environ.get("WHISPER_COOKIE", os.path.join(_TMP, "whisper-dictation.cookie")))
AUDIO_PATH = Path(os.environ.get("WHISPER_AUDIO", os.path.join(_TMP, "whisper-dictation-audio.raw")))

DEFAULT_BACKEND = os.environ.get("WHISPER_BACKEND", "local")
DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "small")
DEFAULT_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
DEFAULT_LANG = os.environ.get("WHISPER_LANG", "en")

OPENAI_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
HTTP_TIMEOUT = _env_int("WHISPER_HTTP_TIMEOUT", 300)

# API-key resolution. Prefer a secret manager over a plaintext env var: an
# exported key lives in the session environment, readable by every process
# the user runs (via /proc/PID/environ). The key is resolved at call time
# (not import) so the wallet can be unlocked lazily, in this order:
#   1. OPENAI_API_KEY env var           (terminal testing; wins if set)
#   2. OPENAI_API_KEY_CMD               (a shell command that prints the key,
#                                        e.g. "pass show openai/api")
#   3. secret-tool (libsecret / KWallet) lookup on SECRET_TOOL_ATTRS
OPENAI_API_KEY_CMD = os.environ.get("OPENAI_API_KEY_CMD", "")
# Attribute pair the key is stored under; keep in sync with the README's
# `secret-tool store` command. Overridable for users with an existing entry.
SECRET_TOOL_ATTRS = os.environ.get(
    "WHISPER_SECRET_TOOL_ATTRS", "service openai-api-key"
).split()

# Transient failures (429 rate-limit, 5xx server) are retried a few times
# before giving up, since the captured audio is discarded after the toggle
# and the user would otherwise have to re-record from scratch.
HTTP_MAX_ATTEMPTS = max(1, _env_int("WHISPER_HTTP_RETRIES", 2) + 1)
HTTP_RETRY_BACKOFF_S = 1.5

# OpenAI's hard upload limit is 25 MB. Over it, the endpoint does NOT reject
# the request — it returns 200 with a silently truncated transcript — so we
# must guard the size ourselves rather than rely on an HTTP error.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

DEBUG = os.environ.get("WHISPER_DEBUG", "") not in ("", "0", "false", "False", "no")
_CACHE = os.environ.get("XDG_CACHE_HOME", os.path.join(os.path.expanduser("~"), ".cache"))
DEBUG_DIR = Path(os.environ.get("WHISPER_DEBUG_DIR", os.path.join(_CACHE, "whisper-dictation")))

# Audio capture format (parec args below must stay in sync with these).
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # bytes per sample (S16LE)
CHANNELS = 1

# parec's default buffer delays the first samples by ~2s, which silently
# eats the start of every recording (you start talking before capture is
# actually flowing). A low target latency makes it stream near-immediately.
PAREC_LATENCY_MS = _env_int("WHISPER_PAREC_LATENCY_MS", 30)


class TranscriptionError(Exception):
    """A failure worth surfacing to the user with a specific OSD message.

    The KDE shortcut runs whisper-toggle with no terminal, so stderr is
    invisible — the OSD is the only feedback. ``osd`` is the short line
    shown to the user; the exception's message carries the full detail
    for stderr/logs. ``icon`` picks the OSD glyph (a soft
    "dialog-information" for benign outcomes like silence, "dialog-error"
    for real failures).
    """

    def __init__(self, osd: str, detail: str = "", icon: str = "dialog-error"):
        super().__init__(detail or osd)
        self.osd = osd
        self.icon = icon


def _find_qdbus() -> str | None:
    """Return the first available qdbus binary name, or None."""
    for candidate in ("qdbus-qt6", "qdbus", "qdbus-qt5"):
        if shutil.which(candidate):
            return candidate
    return None


def osd(icon: str, text: str) -> None:
    """Show a KDE OSD popup. Non-fatal if qdbus is missing or fails."""
    qdbus = _find_qdbus()
    if qdbus is None:
        return  # not on KDE, or qdbus not installed; silently skip
    try:
        subprocess.run(
            [
                qdbus, "org.kde.plasmashell",
                "/org/kde/osdService", "org.kde.osdService.showText",
                icon, text,
            ],
            check=False,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass


def read_cookie_pid() -> int | None:
    if not COOKIE_PATH.exists():
        return None
    try:
        return int(COOKIE_PATH.read_text().strip())
    except (ValueError, OSError):
        return None


def is_running(pid: int | None = None) -> bool:
    """True if a recording session is currently active."""
    if pid is None:
        pid = read_cookie_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def start_recording() -> int:
    """Begin capturing 16kHz mono s16le PCM with parec. Returns recorder PID."""
    AUDIO_PATH.unlink(missing_ok=True)
    COOKIE_PATH.unlink(missing_ok=True)

    osd("microphone-sensitivity-high", "🔴 Whisper ON")

    out = open(AUDIO_PATH, "wb")  # noqa: SIM115 - we want a raw FD for the child
    try:
        proc = subprocess.Popen(
            [
                "parec", "--raw",
                "--format=S16LE", "--rate=16000", "--channels=1",
                f"--latency-msec={PAREC_LATENCY_MS}",
            ],
            stdout=out,
            stderr=subprocess.DEVNULL,
        )
    finally:
        out.close()  # parent doesn't need it; child keeps its own FD

    COOKIE_PATH.write_text(str(proc.pid))
    return proc.pid


def stop_recorder(pid: int) -> None:
    """SIGTERM the recorder (lets it flush), then SIGKILL after a short wait."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(10):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    # Still alive after 1s — force it.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _transcribe_local(pcm) -> str:
    """Transcribe int16 PCM with a locally-run faster-whisper model."""
    # Lazy imports: only the local backend pays the numpy/model load cost.
    import numpy as np
    from faster_whisper import WhisperModel

    audio = pcm.astype(np.float32) / 32768.0

    model = WhisperModel(
        DEFAULT_MODEL,
        device=DEFAULT_DEVICE,
        compute_type="int8" if DEFAULT_DEVICE == "cpu" else "float16",
    )

    segments, _ = model.transcribe(
        audio,
        language=DEFAULT_LANG,
        beam_size=1,
        vad_filter=True,
        without_timestamps=True,
    )

    return "".join(seg.text for seg in segments).strip()


def _pcm_to_wav_bytes(pcm) -> bytes:
    """Wrap raw int16 PCM in a WAV container (no re-encoding)."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    return buf.getvalue()


def _run_key_cmd(cmd: str) -> str | None:
    """Run a shell command that prints the API key on stdout; None on failure."""
    try:
        out = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=15
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"whisper-toggle: OPENAI_API_KEY_CMD timed out: {cmd!r}\n")
        return None
    if out.returncode != 0:
        sys.stderr.write(
            f"whisper-toggle: OPENAI_API_KEY_CMD failed ({out.returncode}): "
            f"{out.stderr.strip()}\n"
        )
        return None
    return out.stdout.strip() or None


def _secret_tool_lookup() -> str | None:
    """Look the key up in libsecret / KWallet via secret-tool; None if absent."""
    if not shutil.which("secret-tool"):
        return None
    try:
        out = subprocess.run(
            ["secret-tool", "lookup", *SECRET_TOOL_ATTRS],
            capture_output=True,
            text=True,
            timeout=30,  # may block on a wallet-unlock prompt
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write("whisper-toggle: secret-tool lookup timed out\n")
        return None
    # rc != 0 (typically 1) just means "no such secret" — not an error here.
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _get_api_key() -> str | None:
    """Resolve the OpenAI API key: env var, then key command, then secret store.

    Resolved at call time so a secret manager can prompt to unlock lazily,
    rather than forcing it at import (which the toggle-on path never needs).
    """
    env = os.environ.get("OPENAI_API_KEY")
    if env:
        return env
    if OPENAI_API_KEY_CMD:
        key = _run_key_cmd(OPENAI_API_KEY_CMD)
        if key:
            return key
    return _secret_tool_lookup()


def _transcribe_cloud(pcm) -> str:
    """Transcribe int16 PCM via the OpenAI transcription API (stdlib only)."""
    import urllib.error
    import urllib.request
    import uuid

    api_key = _get_api_key()
    if not api_key:
        raise TranscriptionError(
            "⚠ No OpenAI API key found",
            "no key from OPENAI_API_KEY, OPENAI_API_KEY_CMD, or secret-tool "
            f"({' '.join(SECRET_TOOL_ATTRS)})",
        )

    wav = _pcm_to_wav_bytes(pcm)

    if len(wav) > MAX_UPLOAD_BYTES:
        minutes = len(pcm) / SAMPLE_RATE / 60
        raise TranscriptionError(
            "⚠ Recording too long — split it",
            f"WAV is {len(wav) / 1024 / 1024:.1f} MB ({minutes:.1f} min); OpenAI's "
            f"limit is 25 MB (~13 min of 16kHz mono) and silently truncates above it",
        )

    # Build a multipart/form-data body by hand to avoid pulling in the openai SDK.
    boundary = f"----whisper-dictation-{uuid.uuid4().hex}"
    fields = {"model": OPENAI_MODEL, "language": DEFAULT_LANG, "response_format": "text"}

    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
    )
    parts.append(wav)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        f"{OPENAI_BASE_URL}/audio/transcriptions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    for attempt in range(1, HTTP_MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                # response_format=text returns the transcript as the raw body.
                return resp.read().decode("utf-8").strip()
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", "replace").strip()
            # 429 (rate limit) and 5xx (server) are transient — retry with a
            # short backoff before surfacing. Everything else is terminal.
            transient = exc.code == 429 or 500 <= exc.code < 600
            if transient and attempt < HTTP_MAX_ATTEMPTS:
                sys.stderr.write(
                    f"whisper-toggle: OpenAI {exc.code}, retry "
                    f"{attempt}/{HTTP_MAX_ATTEMPTS - 1}\n"
                )
                time.sleep(HTTP_RETRY_BACKOFF_S * attempt)
                continue
            osd_msg, detail = _openai_http_error(exc.code, err_body)
            raise TranscriptionError(
                osd_msg, f"OpenAI API {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                raise TranscriptionError(
                    "⚠ OpenAI request timed out", str(reason)
                ) from exc
            raise TranscriptionError(
                "⚠ Cannot reach OpenAI (network?)", str(reason)
            ) from exc
        except TimeoutError as exc:  # bare socket timeout on some Python builds
            raise TranscriptionError("⚠ OpenAI request timed out", str(exc)) from exc

    # Unreachable: the loop either returns or raises on the final attempt.
    raise TranscriptionError("⚠ OpenAI request failed", "exhausted retries")


def _openai_http_error(code: int, body: str) -> tuple[str, str]:
    """Map an OpenAI HTTP error to a (short OSD line, detail) pair.

    OpenAI returns JSON like {"error": {"message", "type", "code"}}; we
    parse it to distinguish e.g. a bad key from an unknown model, and
    fall back to the raw status when the body isn't the expected shape.
    """
    import json

    detail = body
    err_code = ""
    try:
        err = json.loads(body).get("error", {})
        detail = err.get("message") or body
        err_code = err.get("code") or err.get("type") or ""
    except (ValueError, AttributeError):
        pass

    if code == 401 or err_code == "invalid_api_key":
        osd_msg = "⚠ Invalid OpenAI API key"
    elif code == 403:
        osd_msg = "⚠ OpenAI access denied"
    elif err_code in ("model_not_found", "invalid_model") or code == 404:
        osd_msg = f"⚠ Unknown model: {OPENAI_MODEL}"
    elif err_code == "insufficient_quota":
        osd_msg = "⚠ OpenAI quota exceeded"
    elif code == 429:
        osd_msg = "⚠ OpenAI rate limited — retry"
    elif code == 413:
        osd_msg = "⚠ Recording too long for OpenAI"
    elif 500 <= code < 600:
        osd_msg = "⚠ OpenAI server error — retry"
    else:
        osd_msg = f"⚠ OpenAI error {code}"
    return osd_msg, detail


def _debug_save(name: str, data: bytes) -> None:
    """Best-effort archive of a debug artifact; never raises into the flow."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path = DEBUG_DIR / name
        path.write_bytes(data)
        sys.stderr.write(f"whisper-toggle: [debug] wrote {path}\n")
    except OSError as exc:
        sys.stderr.write(f"whisper-toggle: [debug] save failed: {exc}\n")


def transcribe_and_type() -> None:
    """Load the captured raw PCM, transcribe it, type the result via ydotool."""
    import numpy as np

    try:
        pcm = np.fromfile(AUDIO_PATH, dtype=np.int16)
    except OSError as exc:
        raise TranscriptionError("⚠ No audio captured", str(exc)) from exc

    # WHISPER_DEBUG: archive the exact audio (playable WAV) before transcribing,
    # so a later "half is missing" can be diagnosed as capture vs. API by
    # comparing this file against the saved transcript below.
    stamp = time.strftime("%Y%m%d-%H%M%S") if DEBUG else ""
    if DEBUG:
        _debug_save(f"{stamp}.wav", _pcm_to_wav_bytes(pcm))

    if len(pcm) < 1600:  # < 0.1s of audio
        raise TranscriptionError(
            "⚫ Too little audio",
            "too little audio captured, skipping",
            icon="dialog-information",
        )

    if DEFAULT_BACKEND.lower() == "cloud":
        text = _transcribe_cloud(pcm)
    else:
        text = _transcribe_local(pcm)

    if DEBUG:
        # Saved even when empty — an empty .txt next to a full .wav is itself
        # the diagnosis (audio captured fine, transcription returned nothing).
        _debug_save(f"{stamp}.txt", text.encode("utf-8"))

    if not text:
        raise TranscriptionError(
            "⚫ No speech detected",
            "no text recognized",
            icon="dialog-information",
        )

    try:
        subprocess.run(["ydotool", "type", "--", text], check=True)
    except FileNotFoundError as exc:
        raise TranscriptionError("⚠ ydotool not found", str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        raise TranscriptionError(
            "⚠ Typing failed (is ydotoold running?)", str(exc)
        ) from exc


def stop_recording_and_transcribe() -> None:
    pid = read_cookie_pid()
    COOKIE_PATH.unlink(missing_ok=True)
    if pid is None:
        return

    stop_recorder(pid)
    osd("media-playback-stop", "⚫ Whisper OFF · transcribing…")

    try:
        transcribe_and_type()
    except TranscriptionError as exc:
        # Known failure with a user-facing message; detail goes to stderr.
        sys.stderr.write(f"whisper-toggle: {exc}\n")
        osd(exc.icon, exc.osd)
        return
    except Exception as exc:  # noqa: BLE001 - surface failure via OSD, not a crash
        sys.stderr.write(f"whisper-toggle: transcription failed: {exc}\n")
        osd("dialog-error", "⚠ Whisper failed")
        return
    finally:
        AUDIO_PATH.unlink(missing_ok=True)

    time.sleep(1.2)  # let the "transcribing" OSD be readable
    osd("media-playback-stop", "⚫ Whisper done")


def main() -> int:
    argparse.ArgumentParser(
        description="Toggle Whisper-based dictation on/off.",
    ).parse_args()

    if is_running():
        stop_recording_and_transcribe()
    else:
        start_recording()
    return 0


if __name__ == "__main__":
    sys.exit(main())
