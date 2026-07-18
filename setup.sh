#!/usr/bin/env bash
#
# whisper-dictation setup for KDE Plasma on Wayland.
#
#   ./setup.sh install           # install, local (offline) backend
#   ./setup.sh install --cloud   # install, OpenAI cloud backend
#   ./setup.sh backend local     # switch the hotkey to the local backend
#   ./setup.sh backend cloud     # switch the hotkey to the cloud backend
#   ./setup.sh set-key           # store your OpenAI key in KWallet (secret-tool)
#   ./setup.sh uninstall         # undo everything this script installed
#
# Backend selection lives in ~/.config/environment.d/whisper.conf (loaded into
# the session at login, so the KDE global shortcut sees it). `install --cloud`
# and `backend` write WHISPER_BACKEND there; a change takes effect after the
# next log out / back in.
#
# Supported distros: any with dnf (Fedora) or apt (Debian/Ubuntu).
# Supported session: KDE Plasma on Wayland. The script will warn (but not
# block) on other desktops; only the OSD visual feedback is KDE-specific.
#
# This is a standalone installer (does NOT depend on nerd-dictation,
# but the toggle UX is inspired by it — see README.md for credits).
# It installs:
#   - ydotool (system package, for uinput-based keystroke simulation on Wayland)
#   - per-user ydotoold service (the system one runs as root and is unusable)
#   - udev rule for /dev/uinput
#   - user in input group
#   - whisper-dictation Python package via `uv tool install`
#   - Whisper small model (~460 MB, downloaded on first transcription)
#
# Manual step left to the user: bind a KDE global shortcut to
#   ~/.local/bin/whisper-toggle

set -euo pipefail

# ---------- config ----------
# REPO_DIR
REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_BIN="${HOME}/.local/bin"
STATE_DIR="${HOME}/.local/state/whisper-dictation-setup"
STATE_FILE="${STATE_DIR}/installed"

# Backend for this install: "local" (offline Whisper) or "cloud" (OpenAI).
# Set by the --cloud flag; drives whether we set up env.d + skip the model.
BACKEND="local"
ENVD_CONF="${HOME}/.config/environment.d/whisper.conf"

# ---------- helpers ----------
info() { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m==> WARN:\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m==> ERROR:\033[0m %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

check_session_scope() {
  # This project explicitly targets KDE Plasma on Wayland.
  # Warn (but don't block) if the current session looks different.
  local session_type="${XDG_SESSION_TYPE:-}"
  local desktop="${XDG_CURRENT_DESKTOP:-}"
  if [[ -n "$session_type" && "$session_type" != "wayland" ]]; then
    warn "XDG_SESSION_TYPE='${session_type}' — this project targets Wayland."
    warn "Input simulation via ydotool will still work, but you may want xdotool/wtype instead."
  fi
  if [[ -n "$desktop" && "$desktop" != KDE ]]; then
    warn "XDG_CURRENT_DESKTOP='${desktop}' — OSD indicators will not show on non-KDE."
    warn "Transcription will still work; only the visual feedback is KDE-specific."
  fi
}

need_sudo() {
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then return; fi
  sudo -v >/dev/null 2>&1 || die "sudo access required for: $*"
}

mark_state()  { mkdir -p "$STATE_DIR"; printf '%s\n' "$1" >> "$STATE_FILE"; }
has_state()   { [[ -f "$STATE_FILE" ]] && grep -qx -- "$1" "$STATE_FILE"; }
unmark_state() {
  [[ -f "$STATE_FILE" ]] || return 0
  local tmp="${STATE_FILE}.tmp"
  grep -vx -- "$1" "$STATE_FILE" > "$tmp" || true
  mv "$tmp" "$STATE_FILE"
}

ensure_path_has_local_bin() {
  case ":${PATH}:" in
    *":${LOCAL_BIN}:"*) ;;
    *) export PATH="${LOCAL_BIN}:${PATH}" ;;
  esac
}

# Detect the system package manager and the package names we need.
# Sets: PKG_MGR (dnf|apt|none) and an install function `pkg_install`.
detect_pkg_mgr() {
  if [[ -n "${PKG_MGR:-}" ]]; then return; fi
  if command -v dnf >/dev/null 2>&1; then
    PKG_MGR=dnf
    PKG_YDOTOOL=ydotool
    PKG_PIPEWIRE=pipewire-pulseaudio
  elif command -v apt >/dev/null 2>&1; then
    PKG_MGR=apt
    PKG_YDOTOOL=ydotool
    PKG_PIPEWIRE=pipewire-pulse
  else
    PKG_MGR=none
  fi
}

pkg_install() {
  # Install one package by name using the detected package manager.
  case "${PKG_MGR}" in
    dnf) sudo dnf install -y "$1" ;;
    apt) sudo apt update >/dev/null 2>&1 || true; sudo apt install -y "$1" ;;
    *) die "no supported package manager (need dnf or apt). Install '$1' manually and re-run." ;;
  esac
}

pkg_is_installed() {
  case "${PKG_MGR}" in
    dnf) rpm -q "$1" >/dev/null 2>&1 ;;
    apt) dpkg -s "$1" >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

# ---------- install steps ----------

install_ydotool() {
  info "Installing ydotool via ${PKG_MGR:-?}"
  need_sudo "install ydotool"
  if pkg_is_installed "$PKG_YDOTOOL"; then
    info "ydotool already installed"
  else
    pkg_install "$PKG_YDOTOOL"
  fi
  mark_state "pkg:ydotool"
}

install_pipewire_pulse() {
  info "Ensuring ${PKG_PIPEWIRE} is installed (provides parec)"
  need_sudo "install ${PKG_PIPEWIRE}"
  if pkg_is_installed "$PKG_PIPEWIRE"; then
    info "${PKG_PIPEWIRE} already installed"
  else
    pkg_install "$PKG_PIPEWIRE"
  fi
  mark_state "pkg:pipewire-pulse"
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    info "uv already available; leaving it alone"
    return
  fi
  info "Installing uv (was not on PATH)"
  if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
    die "uv install failed"
  fi
  ensure_path_has_local_bin
  command -v uv >/dev/null 2>&1 || die "uv not found after install"
  mark_state "uv:installed"
}

install_whisper_dictation() {
  info "Installing whisper-dictation via uv tool"
  ensure_path_has_local_bin
  if [[ -x "${LOCAL_BIN}/whisper-toggle" ]]; then
    info "whisper-toggle already installed; upgrading"
    uv tool upgrade --force whisper-dictation \
      --reinstall-package whisper-dictation 2>/dev/null \
      || uv tool install --force "${REPO_DIR}"
  else
    uv tool install --force "${REPO_DIR}"
  fi
  mark_state "whisper-dictation:installed"
}

install_udev_rule() {
  local rulefile="/etc/udev/rules.d/80-uinput.rules"
  local rule='KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"'
  info "Installing udev rule for /dev/uinput"
  need_sudo "write ${rulefile}"
  if sudo test -f "$rulefile" && sudo grep -qxF "$rule" "$rulefile"; then
    info "udev rule already present"
  else
    echo "$rule" | sudo tee "$rulefile" >/dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger /dev/uinput
  fi
  mark_state "udev:uinput"
}

add_user_to_input_group() {
  info "Adding ${USER} to input group"
  need_sudo "usermod -aG input"
  if id -nG "$USER" | grep -qw input; then
    info "${USER} already in input group"
  else
    sudo usermod -aG input "$USER"
    warn "You must log out and back in (or reboot) for the group change to take effect."
  fi
  mark_state "group:input"
}

enable_ydotoold() {
  info "Ensuring per-user ydotoold service (NOT the system one)"

  if sudo systemctl cat ydotool.service >/dev/null 2>&1 \
     && sudo systemctl is-enabled ydotool.service >/dev/null 2>&1; then
    info "Disabling system ydotool.service (runs ydotoold as root, socket inaccessible)"
    sudo systemctl disable --now ydotool.service || true
    mark_state "disabled:system-ydotool-service"
  fi

  # Resolve the real path to ydotoold rather than hardcoding /usr/bin/.
  local ydotoold_bin
  ydotoold_bin="$(command -v ydotoold || true)"
  [[ -n "$ydotoold_bin" ]] || ydotoold_bin="/usr/bin/ydotoold"

  mkdir -p "${HOME}/.config/systemd/user"
  local user_unit="${HOME}/.config/systemd/user/ydotool.service"
  cat > "$user_unit" <<EOF
[Unit]
Description=ydotoold (per-user, for whisper-dictation)

[Service]
Type=simple
ExecStart=${ydotoold_bin}
Restart=always

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now ydotool.service \
    || warn "could not enable user ydotool.service"
  mark_state "service:user"
}

setup_cloud_env() {
  # Configure the OpenAI cloud backend via environment.d, which is loaded into
  # the systemd user session — so the KDE global shortcut (which does NOT see
  # your interactive shell) picks it up. We never write a real key: the file is
  # created with an empty placeholder for the user to fill in, chmod 600.
  info "Configuring cloud backend (env.d: ${ENVD_CONF})"
  mkdir -p "$(dirname -- "$ENVD_CONF")"
  if [[ -f "$ENVD_CONF" ]]; then
    warn "${ENVD_CONF} already exists — leaving it untouched."
    warn "Ensure it contains: WHISPER_BACKEND=cloud and OPENAI_API_KEY=sk-..."
  else
    cat > "$ENVD_CONF" <<'EOF'
# whisper-dictation cloud backend (OpenAI).
WHISPER_BACKEND=cloud
# Provide the key ONE of these ways (first match wins):
#   1. plaintext below (simplest, least safe — readable by all your processes)
OPENAI_API_KEY=
#   2. a command that prints it:  OPENAI_API_KEY_CMD=pass show openai/api
#   3. secret-tool (KWallet). Leave OPENAI_API_KEY empty and run once:
#        secret-tool store --label='OpenAI API key' service openai-api-key
# Optional overrides:
# OPENAI_TRANSCRIBE_MODEL=gpt-4o-transcribe
# WHISPER_HTTP_RETRIES=2
EOF
    chmod 600 "$ENVD_CONF"
    mark_state "cloud:envd"
    info "Wrote ${ENVD_CONF} (chmod 600). Add your OPENAI_API_KEY there."
  fi
}

conf_set_backend() {
  # Idempotently set WHISPER_BACKEND=<value> in the env.d file, preserving any
  # other lines (key config, model overrides). Creates the file if absent.
  local value="$1"
  mkdir -p "$(dirname -- "$ENVD_CONF")"
  if [[ -f "$ENVD_CONF" ]]; then
    local tmp="${ENVD_CONF}.tmp"
    grep -v '^[[:space:]]*WHISPER_BACKEND=' "$ENVD_CONF" > "$tmp" || true
    printf 'WHISPER_BACKEND=%s\n' "$value" >> "$tmp"
    mv "$tmp" "$ENVD_CONF"
  else
    cat > "$ENVD_CONF" <<EOF
# whisper-dictation backend selection (loaded into the session at login).
WHISPER_BACKEND=${value}
EOF
    mark_state "cloud:envd"
  fi
  chmod 600 "$ENVD_CONF"
}

cmd_backend() {
  local value="${1:-}"
  case "$value" in
    local | cloud) ;;
    *) die "usage: $0 backend <local|cloud>" ;;
  esac
  conf_set_backend "$value"
  info "Backend set to '${value}' in ${ENVD_CONF}"
  if [[ "$value" == "cloud" ]]; then
    if command -v secret-tool >/dev/null 2>&1 \
       && secret-tool lookup service openai-api-key >/dev/null 2>&1; then
      info "OpenAI key found in KWallet — good to go."
    else
      warn "No OpenAI key stored yet. Run:  $0 set-key"
      warn "(or set OPENAI_API_KEY / OPENAI_API_KEY_CMD in ${ENVD_CONF})"
    fi
  fi
  warn "Log out and back in for the KDE hotkey to pick up the change."
}

cmd_set_key() {
  # Store the OpenAI key in the login keyring (KWallet via libsecret) so the
  # cloud backend can fetch it with `secret-tool lookup` — no plaintext key in
  # any file or in the process environment.
  if ! command -v secret-tool >/dev/null 2>&1; then
    die "secret-tool not found. Install libsecret first: \
Fedora 'sudo dnf install libsecret', Debian/Ubuntu 'sudo apt install libsecret-tools'."
  fi
  info "Storing OpenAI API key in KWallet (you will be prompted; input is hidden)."
  secret-tool store --label='OpenAI API key' service openai-api-key \
    || die "secret-tool store failed"
  info "Stored. The cloud backend reads it via: secret-tool lookup service openai-api-key"
}

install() {
  info "Installing whisper-dictation stack (backend: ${BACKEND})"
  check_session_scope
  detect_pkg_mgr
  if [[ "$PKG_MGR" == "none" ]]; then
    warn "No dnf or apt detected. System packages (ydotool, pipewire-pulse)"
    warn "must be installed manually; this script will skip them."
  fi
  install_ydotool
  install_pipewire_pulse
  install_uv
  install_whisper_dictation
  install_udev_rule
  add_user_to_input_group
  enable_ydotoold
  if [[ "$BACKEND" == "cloud" ]]; then
    setup_cloud_env
  fi

  # The model/key line and one manual step differ per backend.
  local model_line step_key
  if [[ "$BACKEND" == "cloud" ]]; then
    model_line='Cloud (OpenAI) backend configured — no local model is downloaded.
Transcription runs on OpenAI'\''s servers; your audio leaves the machine.'
    step_key="  1. Add your OpenAI API key to:
         ${ENVD_CONF}
     Set the OPENAI_API_KEY= line (get a key at platform.openai.com).
  2. Log out + back in (or reboot) — for the input-group change AND so the
     session picks up ${ENVD_CONF}."
  else
    model_line='The Whisper "small" model (~460 MB) will be downloaded
automatically on your first toggle-off transcription.'
    step_key="  1. Log out + back in (or reboot) for the input-group change."
  fi

  cat >&2 <<EOF

------------------------------------------------------------
Install complete. ${model_line}

Remaining MANUAL steps:

${step_key}
  3. Verify ydotool:  focus a text field, then run
         ydotool type "hello"
     "hello" should appear in the focused window.
  4. Smoke test whisper-toggle:
         whisper-toggle   # press → speak → press again
  5. Bind a KDE global shortcut to:
         ${LOCAL_BIN}/whisper-toggle
     (System Settings > Keyboard > Shortcuts > Custom > Command/URL)
EOF

  if [[ "$BACKEND" != "cloud" ]]; then
    cat >&2 <<EOF

Want cloud (OpenAI) transcription instead? Re-run:
    $0 install --cloud
or see README.md § "Cloud backend (OpenAI)".
EOF
  fi

  cat >&2 <<EOF

State recorded in: ${STATE_FILE}
To undo everything:  $0 uninstall
------------------------------------------------------------
EOF
}

# ---------- uninstall steps ----------

uninstall_whisper_dictation() {
  if command -v whisper-toggle >/dev/null 2>&1; then
    info "uv tool uninstall whisper-dictation"
    uv tool uninstall whisper-dictation 2>/dev/null || true
  fi
  unmark_state "whisper-dictation:installed"
}

disable_ydotoold() {
  if has_state "service:user"; then
    info "Disabling user ydotool.service"
    systemctl --user disable --now ydotool.service 2>/dev/null || true
    rm -f "${HOME}/.config/systemd/user/ydotool.service"
    systemctl --user daemon-reload 2>/dev/null || true
  fi
  unmark_state "service:user"
  if has_state "disabled:system-ydotool-service"; then
    info "(Not re-enabling system ydotool.service; it is unusable as non-root.)"
    unmark_state "disabled:system-ydotool-service"
  fi
}

remove_udev_rule() {
  local rulefile="/etc/udev/rules.d/80-uinput.rules"
  if sudo test -f "$rulefile"; then
    info "Removing udev rule"
    need_sudo "rm ${rulefile}"
    sudo rm -f "$rulefile"
    sudo udevadm control --reload-rules
    sudo udevadm trigger /dev/uinput
  fi
  unmark_state "udev:uinput"
}

remove_user_from_input_group() {
  if id -nG "$USER" | grep -qw input; then
    info "Removing ${USER} from input group"
    need_sudo "gpasswd -d ${USER} input"
    sudo gpasswd -d "$USER" input || true
  fi
  unmark_state "group:input"
}

uninstall_ydotool() {
  if pkg_is_installed "$PKG_YDOTOOL"; then
    info "Removing ydotool via ${PKG_MGR}"
    need_sudo "remove ydotool"
    case "$PKG_MGR" in
      dnf) sudo dnf remove -y ydotool || true ;;
      apt) sudo apt remove -y ydotool || true ;;
    esac
  fi
  unmark_state "pkg:ydotool"
  unmark_state "pkg:pipewire-pulse"
}

uninstall_uv() {
  if has_state "uv:installed"; then
    if command -v uv >/dev/null 2>&1; then
      info "Removing uv (installed by this script)"
      uv self uninstall -y 2>/dev/null \
        || rm -f "${LOCAL_BIN}/uv" "${HOME}/.local/share/uv" 2>/dev/null || true
    fi
    unmark_state "uv:installed"
  fi
}

cleanup_caches() {
  # Optional: remove the downloaded Whisper model cache.
  local cache="${HOME}/.cache/huggingface"
  if [[ ! -d "$cache" ]]; then
    return
  fi
  # Non-interactive (no TTY): default to keeping the cache.
  if [[ ! -t 0 ]]; then
    info "Whisper model cache at ${cache} left in place (non-interactive)"
    return
  fi
  warn "Whisper model cache at ${cache} (~150 MB+) still present."
  read -r -p "Delete it too? [y/N] " ans
  case "$ans" in
    y|Y) rm -rf "$cache"; info "Cache removed." ;;
    *) info "Leaving cache in place." ;;
  esac
}

uninstall() {
  info "Uninstalling whisper-dictation stack"
  detect_pkg_mgr
  warn "This will:"
  warn "  - uv tool uninstall whisper-dictation"
  warn "  - disable + remove the user ydotool.service"
  warn "  - remove the udev rule /etc/udev/rules.d/80-uinput.rules"
  warn "  - remove ${USER} from the input group"
  warn "  - dnf remove ydotool"
  if has_state "uv:installed"; then
    warn "  - remove uv (it was installed by this script)"
  fi
  echo >&2
  read -r -p "Proceed with uninstall? [y/N] " ans
  case "$ans" in
    y|Y) ;;
    *) info "aborted"; exit 0 ;;
  esac

  uninstall_whisper_dictation
  disable_ydotoold
  remove_udev_rule
  remove_user_from_input_group
  uninstall_ydotool
  uninstall_uv
  cleanup_caches
  # Drop the state marker but never auto-delete the key file (see summary).
  unmark_state "cloud:envd"

  rm -f /tmp/whisper-dictation.cookie /tmp/whisper-dictation-audio.raw 2>/dev/null || true

  if [[ -f "$STATE_FILE" ]] && [[ ! -s "$STATE_FILE" ]]; then
    rm -f "$STATE_FILE"
    rmdir "$STATE_DIR" 2>/dev/null || true
  fi

  cat >&2 <<EOF

------------------------------------------------------------
Uninstall complete. Remaining MANUAL steps:

  - Remove the KDE shortcut you bound to whisper-toggle
    (System Settings > Shortcuts > Custom).
  - Log out + back in for the input-group removal to apply.
$(if has_state "cloud:envd" || [[ -f "$ENVD_CONF" ]]; then
    printf '  - Cloud key file left in place (may hold your API key):\n        %s\n    Delete it yourself if you no longer need it.\n' "$ENVD_CONF"
  fi)
To reinstall later:  $0 install
------------------------------------------------------------
EOF
}

# ---------- main ----------

usage() {
  cat >&2 <<EOF
Usage: $0 <command>

  install [--cloud]  Install the stack. Default is the local (offline)
                     backend (Whisper 'small' model, ~460 MB, downloaded on
                     first transcription). --cloud selects the OpenAI backend
                     instead: no local model, and writes the env.d config.
  backend <local|cloud>
                     Switch the backend for the KDE hotkey by setting
                     WHISPER_BACKEND in ${ENVD_CONF}
                     (takes effect after the next log out / back in).
  set-key            Store your OpenAI API key in KWallet (via secret-tool),
                     so the cloud backend needs no plaintext key anywhere.
  uninstall          Undo everything this script installed.

To pre-warm the local model:

  uv run --with faster-whisper python -c \\
    "from faster_whisper import WhisperModel; \\
     WhisperModel('small', device='cpu', compute_type='int8')"

To use a different local model, set WHISPER_MODEL in the environment
before invoking whisper-toggle (tiny/base/small/medium/large-v3).
EOF
}

cmd="${1:-}"
shift || true
case "$cmd" in
  install)
    for arg in "$@"; do
      case "$arg" in
        --cloud) BACKEND="cloud" ;;
        --local) BACKEND="local" ;;
        *) err "unknown option: ${arg}"; usage; exit 1 ;;
      esac
    done
    install
    ;;
  backend)   cmd_backend "${1:-}" ;;
  set-key)   cmd_set_key ;;
  uninstall) uninstall ;;
  *) usage; exit 1 ;;
esac
