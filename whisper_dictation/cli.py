"""
Whisper-based dictation toggle.

Press once to start recording, again to stop and transcribe.
Uses faster-whisper for transcription, ydotool for input simulation,
and the KDE OSD (via qdbus) for visual feedback.

Configuration via environment variables:
  WHISPER_MODEL   tiny | base | small | medium | large-v3   (default: base)
  WHISPER_DEVICE  cpu | cuda                              (default: cpu)
  WHISPER_LANG    en | fr | de | ...                      (default: en)
  WHISPER_COOKIE  path to cookie file                     (default: /tmp/whisper-dictation.cookie)
  WHISPER_AUDIO   path to raw PCM temp file               (default: /tmp/whisper-dictation-audio.raw)
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

DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "base")
DEFAULT_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
DEFAULT_LANG = os.environ.get("WHISPER_LANG", "en")


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


def transcribe_and_type() -> None:
    """Load the captured raw PCM, run Whisper, type the result via ydotool."""
    # Lazy imports: toggle-on stays cheap, only toggle-off pays the model load.
    import numpy as np
    from faster_whisper import WhisperModel

    try:
        audio = np.fromfile(AUDIO_PATH, dtype=np.int16)
    except OSError as exc:
        sys.stderr.write(f"whisper-toggle: cannot read audio file: {exc}\n")
        return
    audio = audio.astype(np.float32) / 32768.0

    if len(audio) < 1600:  # < 0.1s of audio
        sys.stderr.write("whisper-toggle: too little audio captured, skipping\n")
        return

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

    text = "".join(seg.text for seg in segments).strip()

    if text:
        try:
            subprocess.run(["ydotool", "type", "--", text], check=True)
        except subprocess.CalledProcessError as exc:
            sys.stderr.write(f"whisper-toggle: ydotool failed: {exc}\n")
    else:
        sys.stderr.write("whisper-toggle: no text recognized\n")


def stop_recording_and_transcribe() -> None:
    pid = read_cookie_pid()
    COOKIE_PATH.unlink(missing_ok=True)
    if pid is None:
        return

    stop_recorder(pid)
    osd("media-playback-stop", "⚫ Whisper OFF · transcribing…")

    try:
        transcribe_and_type()
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
