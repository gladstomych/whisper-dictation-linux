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

Cloud backend (WHISPER_BACKEND=cloud) uses the OpenAI transcription API:
  OPENAI_API_KEY           required when backend is cloud
  OPENAI_TRANSCRIBE_MODEL  gpt-4o-transcribe | gpt-4o-mini-transcribe | whisper-1
                                                           (default: gpt-4o-transcribe)
  OPENAI_BASE_URL          override API base URL           (default: https://api.openai.com/v1)
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

_TMP = os.environ.get("TMPDIR", "/tmp")
COOKIE_PATH = Path(os.environ.get("WHISPER_COOKIE", os.path.join(_TMP, "whisper-dictation.cookie")))
AUDIO_PATH = Path(os.environ.get("WHISPER_AUDIO", os.path.join(_TMP, "whisper-dictation-audio.raw")))

DEFAULT_BACKEND = os.environ.get("WHISPER_BACKEND", "local")
DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "small")
DEFAULT_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
DEFAULT_LANG = os.environ.get("WHISPER_LANG", "en")

OPENAI_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")

# Audio capture format (parec args below must stay in sync with these).
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # bytes per sample (S16LE)
CHANNELS = 1


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


def _transcribe_cloud(pcm) -> str:
    """Transcribe int16 PCM via the OpenAI transcription API (stdlib only)."""
    import urllib.error
    import urllib.request
    import uuid

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise TranscriptionError(
            "⚠ No OpenAI API key set",
            "WHISPER_BACKEND=cloud but OPENAI_API_KEY is not set in the environment",
        )

    wav = _pcm_to_wav_bytes(pcm)

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
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            # response_format=text returns the transcript as the raw body.
            return resp.read().decode("utf-8").strip()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace").strip()
        osd_msg, detail = _openai_http_error(exc.code, body)
        raise TranscriptionError(osd_msg, f"OpenAI API {exc.code}: {detail}") from exc
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


def transcribe_and_type() -> None:
    """Load the captured raw PCM, transcribe it, type the result via ydotool."""
    import numpy as np

    try:
        pcm = np.fromfile(AUDIO_PATH, dtype=np.int16)
    except OSError as exc:
        raise TranscriptionError("⚠ No audio captured", str(exc)) from exc

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
