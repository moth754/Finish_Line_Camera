#!/usr/bin/env bash
#
# install.sh - set up the Finish Line Recorder on a Linux machine.
#
# Works on two kinds of host:
#
#   * Raspberry Pi (Pi OS, with a Camera Module)  - full system: live camera,
#     continuous recording, playback, OCR.  The Pi camera stack (picamera2,
#     libcamera, PyAV, NumPy) is installed from apt, because picamera2 is built
#     against the system NumPy and pip copies of it break the camera.
#
#   * Any other Linux PC (Debian/Ubuntu/Fedora/Arch/openSUSE) - full system
#     too, using a USB camera (any V4L2 device: a webcam, a capture card).
#     Live capture, recording, playback and OCR all work.  With no camera
#     attached the app falls back to its built-in mock, so a laptop is still
#     useful for playing back a race copied from a Pi and running the OCR.
#
# Usage:
#   ./install.sh                     # venv install, EasyOCR, systemd service
#   ./install.sh --ocr tesseract     # light OCR instead of EasyOCR/PyTorch
#   ./install.sh --ocr none          # no OCR at all (recording + playback only)
#   ./install.sh --no-service        # don't install/enable the systemd unit
#   ./install.sh --port 8080 --yes   # non-interactive, alternate port
#   ./install.sh --help
#
# Safe to re-run: it upgrades an existing install in place and never touches
# recordings/, captures/ or data/.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults (override with the flags above)
# ---------------------------------------------------------------------------

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="${SUDO_USER:-$(id -un)}"
SERVICE_NAME="finish-line"
WEB_PORT=8000
OCR_BACKEND="easyocr"          # easyocr | tesseract | none
USE_VENV=1
INSTALL_SERVICE=1
INSTALL_SUDOERS=1
ASSUME_YES=0
SKIP_SYSTEM_PKGS=0

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

if [ -t 1 ]; then
    BOLD=$'\033[1m'; RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'
    BLU=$'\033[34m'; DIM=$'\033[2m'; RST=$'\033[0m'
else
    BOLD=""; RED=""; GRN=""; YEL=""; BLU=""; DIM=""; RST=""
fi

step()  { printf '\n%s==>%s %s%s%s\n' "$BLU" "$RST" "$BOLD" "$*" "$RST"; }
info()  { printf '    %s\n' "$*"; }
ok()    { printf '    %s+%s %s\n' "$GRN" "$RST" "$*"; }
warn()  { printf '    %s!%s %s\n' "$YEL" "$RST" "$*"; }
die()   { printf '\n%sERROR:%s %s\n\n' "$RED" "$RST" "$*" >&2; exit 1; }

usage() {
    # Print the comment header at the top of this file, stopping at the code.
    awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
    cat <<EOF

Options:
  --dir PATH          install location             (default: this script's folder)
  --user NAME         user the service runs as     (default: $SERVICE_USER)
  --port N            dashboard port               (default: $WEB_PORT)
  --ocr BACKEND       easyocr | tesseract | none   (default: $OCR_BACKEND)
  --name NAME         systemd unit name            (default: $SERVICE_NAME)
  --system-python     install into the system Python instead of a venv
  --no-service        skip the systemd unit
  --no-sudoers        skip the passwordless-shutdown sudoers rule
  --skip-system-pkgs  don't touch apt/dnf/pacman (assume deps are present)
  -y, --yes           don't ask for confirmation
  -h, --help          this message
EOF
    exit 0
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --dir)              PROJECT_DIR="$2"; shift 2 ;;
        --user)             SERVICE_USER="$2"; shift 2 ;;
        --port)             WEB_PORT="$2"; shift 2 ;;
        --ocr)              OCR_BACKEND="$2"; shift 2 ;;
        --name)             SERVICE_NAME="$2"; shift 2 ;;
        --system-python)    USE_VENV=0; shift ;;
        --no-service)       INSTALL_SERVICE=0; shift ;;
        --no-sudoers)       INSTALL_SUDOERS=0; shift ;;
        --skip-system-pkgs) SKIP_SYSTEM_PKGS=1; shift ;;
        -y|--yes)           ASSUME_YES=1; shift ;;
        -h|--help)          usage ;;
        *)                  die "unknown option: $1  (try --help)" ;;
    esac
done

case "$OCR_BACKEND" in
    easyocr|tesseract|none) ;;
    *) die "--ocr must be one of: easyocr, tesseract, none" ;;
esac

[ -d "$PROJECT_DIR" ] || die "install directory does not exist: $PROJECT_DIR"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
[ -f "$PROJECT_DIR/run.py" ] || die "run.py not found in $PROJECT_DIR - point --dir at the project checkout"
id "$SERVICE_USER" >/dev/null 2>&1 || die "no such user: $SERVICE_USER"

[ "$(id -u)" -eq 0 ] && [ -z "${SUDO_USER:-}" ] && \
    warn "running as root; the service will run as '$SERVICE_USER'"

# ---------------------------------------------------------------------------
# Detect the host
# ---------------------------------------------------------------------------

step "Detecting the system"

OS_NAME="linux"; OS_VERSION=""
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_NAME="${NAME:-linux}"; OS_VERSION="${VERSION_ID:-}"
fi
info "OS:       $OS_NAME $OS_VERSION"

# Package manager
PKG=""
for candidate in apt-get dnf pacman zypper; do
    if command -v "$candidate" >/dev/null 2>&1; then PKG="$candidate"; break; fi
done
if [ -n "$PKG" ]; then info "Packages: $PKG"; else warn "no known package manager found"; fi

# Raspberry Pi?  The device-tree model string is the reliable test.
IS_PI=0
if [ -r /proc/device-tree/model ] && tr -d '\0' < /proc/device-tree/model | grep -qi "raspberry pi"; then
    IS_PI=1
    PI_MODEL="$(tr -d '\0' < /proc/device-tree/model)"
    info "Board:    $PI_MODEL"
fi

# Python
command -v python3 >/dev/null 2>&1 || die "python3 is not installed"
PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
info "Python:   $PY_VERSION"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Python 3.9 or newer is required (found $PY_VERSION)"

if [ "$IS_PI" -eq 1 ]; then
    info "Mode:     Raspberry Pi - CSI camera and USB cameras both supported"
else
    info "Mode:     generic Linux PC - USB (V4L2) cameras supported; no Pi CSI"
    info "          camera stack.  With no camera attached, the mock is used."
fi

# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------

step "Install plan"
info "Directory:   $PROJECT_DIR"
info "Service user: $SERVICE_USER"
info "Dashboard:   http://0.0.0.0:$WEB_PORT/"
info "Python env:  $([ "$USE_VENV" -eq 1 ] && echo "virtualenv at $PROJECT_DIR/.venv" || echo "system Python")"
info "OCR:         $OCR_BACKEND$([ "$OCR_BACKEND" = easyocr ] && echo "  (downloads PyTorch, ~2 GB)" || true)"
info "systemd:     $([ "$INSTALL_SERVICE" -eq 1 ] && echo "$SERVICE_NAME.service (enabled at boot)" || echo "skipped")"

if [ "$ASSUME_YES" -eq 0 ]; then
    printf '\n    Continue? [Y/n] '
    read -r reply </dev/tty || reply="y"
    case "$reply" in [nN]*) echo "    aborted"; exit 0 ;; esac
fi

# sudo wrapper: no-op when already root.
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || die "sudo is required (or run this script as root)"
    SUDO="sudo"
fi

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------

step "Installing system packages"

if [ "$SKIP_SYSTEM_PKGS" -eq 1 ]; then
    warn "skipped (--skip-system-pkgs)"
elif [ -z "$PKG" ]; then
    warn "no supported package manager - install these yourself:"
    warn "  ffmpeg, python3 venv + pip headers$([ "$OCR_BACKEND" = tesseract ] && echo ", tesseract-ocr" || true)"
else
    case "$PKG" in
    apt-get)
        PKGS=(ffmpeg python3-pip python3-venv python3-dev)
        if [ "$IS_PI" -eq 1 ]; then
            # On a Pi these come from apt on purpose: picamera2 is compiled
            # against the system NumPy, and a pip NumPy in the same env breaks it.
            PKGS+=(python3-picamera2 python3-libcamera python3-av python3-numpy
                   python3-pil python3-simplejpeg python3-opencv)
        fi
        [ "$OCR_BACKEND" = tesseract ] && PKGS+=(tesseract-ocr)
        info "apt-get install ${PKGS[*]}"
        $SUDO apt-get update -qq
        DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y --no-install-recommends "${PKGS[@]}"
        ;;
    dnf)
        PKGS=(ffmpeg-free python3-pip python3-devel)
        [ "$OCR_BACKEND" = tesseract ] && PKGS+=(tesseract)
        info "dnf install ${PKGS[*]}"
        $SUDO dnf install -y "${PKGS[@]}" || warn "some packages failed; check ffmpeg is installed"
        ;;
    pacman)
        PKGS=(ffmpeg python-pip)
        [ "$OCR_BACKEND" = tesseract ] && PKGS+=(tesseract tesseract-data-eng)
        info "pacman -S ${PKGS[*]}"
        $SUDO pacman -Sy --needed --noconfirm "${PKGS[@]}"
        ;;
    zypper)
        PKGS=(ffmpeg python3-pip python3-devel)
        [ "$OCR_BACKEND" = tesseract ] && PKGS+=(tesseract-ocr)
        info "zypper install ${PKGS[*]}"
        $SUDO zypper install -y "${PKGS[@]}" || warn "some packages failed; check ffmpeg is installed"
        ;;
    esac
    ok "system packages done"
fi

# ffmpeg is not optional: it IS the recorder.
if command -v ffmpeg >/dev/null 2>&1; then
    ok "ffmpeg: $(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f1-3)"
else
    die "ffmpeg not found - the recorder cannot run without it"
fi

# ---------------------------------------------------------------------------
# Python environment
# ---------------------------------------------------------------------------

step "Setting up the Python environment"

if [ "$USE_VENV" -eq 1 ]; then
    VENV="$PROJECT_DIR/.venv"
    if [ ! -x "$VENV/bin/python" ]; then
        # --system-site-packages is required on a Pi so the venv can see the
        # apt-installed picamera2 / libcamera; harmless elsewhere.
        info "creating virtualenv at $VENV"
        python3 -m venv --system-site-packages "$VENV" \
            || die "could not create the virtualenv (is python3-venv installed?)"
    else
        info "reusing existing virtualenv at $VENV"
    fi
    PYTHON="$VENV/bin/python"
    PIP="$VENV/bin/pip"
else
    PYTHON="$(command -v python3)"
    PIP="$PYTHON -m pip"
    # Debian/Ubuntu mark the system Python as externally managed (PEP 668).
    if $PYTHON -m pip install --help 2>/dev/null | grep -q break-system-packages; then
        PIP="$PYTHON -m pip --break-system-packages"
    fi
    warn "installing into the system Python - a virtualenv is usually safer"
fi

# Only pip and wheel: upgrading setuptools inside a --system-site-packages venv
# shadows the system copy and breaks PyTorch, which pins setuptools<82.
info "upgrading pip"
$PIP install --quiet --upgrade pip wheel

# --- Core runtime ---------------------------------------------------------
# On a Pi these already came from apt (and must not be replaced by pip copies),
# so only Flask is pip-installed there.
step "Installing Python packages"

# OpenCV is what drives a USB camera (and improves OCR preprocessing), so it
# is part of the core set, not an extra.  On a Pi it came from apt above.
CORE=(Flask)
if [ "$IS_PI" -eq 0 ]; then
    CORE+=(numpy Pillow simplejpeg av opencv-python-headless)
fi
info "pip install ${CORE[*]}"
$PIP install --upgrade "${CORE[@]}"
ok "core packages installed"

# --- OCR backend ----------------------------------------------------------
case "$OCR_BACKEND" in
easyocr)
    # Install the CPU-only PyTorch first.  Without this, pip pulls the CUDA
    # build on an x86 PC: several GB of libraries this app never uses.
    info "pip install torch (CPU build) - this is the slow part"
    $PIP install --upgrade torch torchvision \
        --index-url https://download.pytorch.org/whl/cpu \
        || { warn "CPU-only PyTorch index failed; falling back to the default index"
             $PIP install --upgrade torch torchvision; }
    info "pip install easyocr"
    $PIP install --upgrade easyocr
    ok "EasyOCR installed (its recognition models download on first use)"
    ;;
tesseract)
    info "pip install pytesseract"
    $PIP install --upgrade pytesseract
    if command -v tesseract >/dev/null 2>&1; then
        ok "Tesseract: $(tesseract --version 2>&1 | head -1)"
    else
        warn "the tesseract binary is missing - install it with your package manager"
    fi
    ;;
none)
    info "no OCR backend (recording, playback and the timeline still work)"
    ;;
esac

# Match app/config.py's OCR_BACKEND to what was actually installed.
if [ "$OCR_BACKEND" != "easyocr" ]; then
    if grep -q '^OCR_BACKEND = ' "$PROJECT_DIR/app/config.py"; then
        sed -i "s|^OCR_BACKEND = .*|OCR_BACKEND = \"$OCR_BACKEND\"|" "$PROJECT_DIR/app/config.py"
        ok "app/config.py: OCR_BACKEND set to \"$OCR_BACKEND\""
    fi
fi

# ---------------------------------------------------------------------------
# Camera device access
# ---------------------------------------------------------------------------
#
# /dev/video* is owned by root:video, so the service user must be in the
# "video" group or a USB camera cannot be opened at all.  This is the single
# most common reason a webcam "is not detected".

step "Camera device access"
if getent group video >/dev/null 2>&1; then
    if id -nG "$SERVICE_USER" 2>/dev/null | tr ' ' '\n' | grep -qx video; then
        ok "$SERVICE_USER is already in the 'video' group"
    else
        if $SUDO usermod -aG video "$SERVICE_USER"; then
            ok "added $SERVICE_USER to the 'video' group"
            warn "log out and back in (or reboot) before a USB camera will open"
            warn "in an interactive session; the systemd service picks it up on restart"
        else
            warn "could not add $SERVICE_USER to 'video' - USB cameras may not open"
        fi
    fi
else
    warn "no 'video' group on this system - skipping"
fi

VIDEO_NODES="$(ls /dev/video* 2>/dev/null | wc -l)"
if [ "$VIDEO_NODES" -gt 0 ]; then
    info "$VIDEO_NODES /dev/video* node(s) present"
else
    info "no /dev/video* nodes - plug a USB camera in and press Rescan in Settings"
fi

# ---------------------------------------------------------------------------
# Runtime directories
# ---------------------------------------------------------------------------

step "Creating runtime directories"
for d in data captures recordings; do
    mkdir -p "$PROJECT_DIR/$d"
    info "$PROJECT_DIR/$d"
done
# Everything the service writes must belong to the user it runs as.
if [ "$(id -u)" -eq 0 ] || [ "$SERVICE_USER" != "$(id -un)" ]; then
    $SUDO chown -R "$SERVICE_USER:$(id -gn "$SERVICE_USER")" \
        "$PROJECT_DIR/data" "$PROJECT_DIR/captures" "$PROJECT_DIR/recordings"
fi
ok "directories ready"

# ---------------------------------------------------------------------------
# Import check
# ---------------------------------------------------------------------------

step "Checking the install"
IMPORT_REPORT="$("$PYTHON" - <<'PYEOF'
mods = [("flask", "web dashboard"), ("numpy", "motion detection"),
        ("simplejpeg", "preview encoding"), ("PIL", "still images"),
        ("av", "decoding video for OCR")]
optional = [("picamera2", "live Pi (CSI) camera"),
            ("cv2", "USB cameras + better OCR preprocessing"),
            ("easyocr", "bib OCR"), ("pytesseract", "bib OCR")]
missing = []
for name, why in mods:
    try:
        __import__(name); print(f"    + {name:<14} {why}")
    except Exception:
        print(f"    - {name:<14} {why}  [MISSING]"); missing.append(name)
for name, why in optional:
    try:
        __import__(name); print(f"    + {name:<14} {why}")
    except Exception:
        print(f"      {name:<14} {why}  (not installed)")
raise SystemExit(1 if missing else 0)
PYEOF
)" && IMPORT_OK=1 || IMPORT_OK=0
printf '%s\n' "$IMPORT_REPORT"
if [ "$IMPORT_OK" -eq 1 ]; then
    ok "all required modules import"
else
    warn "some required modules are missing - the app will not start until they are installed"
fi

# ---------------------------------------------------------------------------
# systemd service
# ---------------------------------------------------------------------------

if [ "$INSTALL_SERVICE" -eq 1 ]; then
    step "Installing the systemd service"
    if ! command -v systemctl >/dev/null 2>&1; then
        warn "systemd not found - skipping the service (start it by hand with: $PYTHON run.py)"
    else
        UNIT="/etc/systemd/system/$SERVICE_NAME.service"
        SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
        TMP_UNIT="$(mktemp)"
        cat > "$TMP_UNIT" <<EOF
# Finish Line Recorder - generated by install.sh on $(date -Iseconds)
#
#   journalctl -u $SERVICE_NAME.service -f      # follow the logs
#   sudo systemctl restart $SERVICE_NAME        # restart
#   sudo systemctl disable --now $SERVICE_NAME  # stop and remove from boot

[Unit]
Description=Finish Line Recorder
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$PROJECT_DIR
ExecStart=$PYTHON $PROJECT_DIR/run.py --port $WEB_PORT

# Unbuffered so logs reach the journal as they happen.
Environment=PYTHONUNBUFFERED=1

# The dashboard's "Restart app" button exits non-zero on purpose; a recording
# in progress is always finalised by ffmpeg before the process goes away.
Restart=on-failure
RestartSec=3

# Give ffmpeg time to close the segment it is writing.
TimeoutStopSec=30
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
EOF
        $SUDO install -m 0644 "$TMP_UNIT" "$UNIT"
        rm -f "$TMP_UNIT"
        info "wrote $UNIT"
        $SUDO systemctl daemon-reload
        $SUDO systemctl enable "$SERVICE_NAME.service" >/dev/null 2>&1
        ok "enabled at boot"
        if $SUDO systemctl restart "$SERVICE_NAME.service"; then
            sleep 2
            if $SUDO systemctl is-active --quiet "$SERVICE_NAME.service"; then
                ok "service is running"
            else
                warn "service is not active - check: journalctl -u $SERVICE_NAME -n 40"
            fi
        else
            warn "could not start the service - check: journalctl -u $SERVICE_NAME -n 40"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Shutdown sudoers rule (powers the machine off from the dashboard)
# ---------------------------------------------------------------------------

if [ "$INSTALL_SUDOERS" -eq 1 ]; then
    step "Allowing the dashboard's shutdown button"
    SHUTDOWN_BIN=""
    for candidate in /usr/sbin/shutdown /sbin/shutdown; do
        [ -x "$candidate" ] && { SHUTDOWN_BIN="$candidate"; break; }
    done
    if [ -z "$SHUTDOWN_BIN" ]; then
        warn "no shutdown binary found - skipping"
    else
        TMP_SUDO="$(mktemp)"
        cat > "$TMP_SUDO" <<EOF
# Lets the Finish Line Recorder power this machine off from the dashboard.
# Exactly one command, nothing else.  The dashboard asks twice before calling it.
$SERVICE_USER ALL=(root) NOPASSWD: $SHUTDOWN_BIN
EOF
        if $SUDO visudo -cqf "$TMP_SUDO" 2>/dev/null; then
            $SUDO install -m 0440 "$TMP_SUDO" "/etc/sudoers.d/$SERVICE_NAME-shutdown"
            ok "installed /etc/sudoers.d/$SERVICE_NAME-shutdown"
        else
            warn "the generated sudoers rule did not validate - skipping it"
        fi
        rm -f "$TMP_SUDO"
        # config.py defaults to /usr/sbin/shutdown; correct it if this host differs.
        if [ "$SHUTDOWN_BIN" != "/usr/sbin/shutdown" ]; then
            sed -i "s|\"/usr/sbin/shutdown\"|\"$SHUTDOWN_BIN\"|" "$PROJECT_DIR/app/config.py"
            info "app/config.py: shutdown path set to $SHUTDOWN_BIN"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$IP" ] && IP="$(hostname)"

step "Installed"
cat <<EOF

    Dashboard:   ${BOLD}http://$IP:$WEB_PORT/${RST}
    Project:     $PROJECT_DIR
    Python:      $PYTHON
EOF
if [ "$INSTALL_SERVICE" -eq 1 ] && command -v systemctl >/dev/null 2>&1; then
cat <<EOF
    Logs:        journalctl -u $SERVICE_NAME.service -f
    Restart:     sudo systemctl restart $SERVICE_NAME
EOF
else
cat <<EOF
    Start it:    cd $PROJECT_DIR && $PYTHON run.py --port $WEB_PORT
EOF
fi
if [ "$IS_PI" -eq 0 ]; then
cat <<EOF

    ${BOLD}Camera:${RST} plug in a USB camera, then choose it in Settings -> Camera
    (press Rescan if it was connected after the page loaded).  With no camera
    attached the app runs its mock scene, which is still enough to play back a
    race copied from a Pi and run the OCR over it:

        rsync -a pi@<pi-address>:~/finish_line_camera/recordings/ $PROJECT_DIR/recordings/
EOF
fi
echo
