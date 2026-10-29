#!/bin/sh
# Install a LABRAT nest or rat as a systemd service.
#
#   sudo ./packaging/install.sh nest        # the lab's server
#   sudo ./packaging/install.sh rat         # a computer at the bench
#
# Run from a copy of the repository to install that copy, or from anywhere to install the latest release from
# PyPI. Running it again upgrades LABRAT and leaves the configuration alone.
set -eu

KIND="${1:-}"
case "$KIND" in nest|rat) ;; *) echo "usage: sudo $0 nest|rat" >&2; exit 64 ;; esac
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0 $KIND" >&2; exit 77; }
command -v python3 >/dev/null || { echo "python3 is missing (apt install python3 python3-venv)" >&2; exit 69; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "LABRAT needs Python 3.11 or newer" >&2; exit 69; }
python3 -c 'import venv, ensurepip' 2>/dev/null || { echo "python3-venv is missing (apt install python3-venv)" >&2; exit 69; }

PREFIX=/opt/labrat
CONF=/etc/labrat
HERE="$(cd "$(dirname "$0")/.." && pwd)"

id labrat >/dev/null 2>&1 || useradd --system --home-dir /var/lib/labrat --no-create-home --shell /usr/sbin/nologin labrat
if [ "$KIND" = rat ]; then
    for g in dialout gpio video plugdev; do
        getent group "$g" >/dev/null && usermod -a -G "$g" labrat
    done
fi

# a rat sees the system's Python packages too, for Aravis (GigE Vision) and other libraries installed with apt
VENV_OPTS=""
[ "$KIND" = rat ] && VENV_OPTS="--system-site-packages"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv $VENV_OPTS "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
EXTRA=""
[ "$KIND" = rat ] && EXTRA="[rat]"
if [ -f "$HERE/pyproject.toml" ] && grep -q 'name = "labrat-lab"' "$HERE/pyproject.toml"; then
    "$PREFIX/venv/bin/pip" install --quiet --upgrade "$HERE$EXTRA"
else
    "$PREFIX/venv/bin/pip" install --quiet --upgrade "labrat-lab$EXTRA"
fi
ln -sf "$PREFIX/venv/bin/labrat" /usr/local/bin/labrat

mkdir -p "$CONF"
if [ ! -f "$CONF/$KIND.toml" ]; then
    STATE_DIRECTORY=/var/lib/labrat "$PREFIX/venv/bin/labrat" "$KIND" init --config "$CONF/$KIND.toml"
    chown root:labrat "$CONF/$KIND.toml"
    chmod 640 "$CONF/$KIND.toml"
else
    echo "keeping $CONF/$KIND.toml"
fi

"$PREFIX/venv/bin/labrat" service "$KIND" > "/etc/systemd/system/labrat-$KIND.service"
systemctl daemon-reload
systemctl enable --now "labrat-$KIND"
systemctl restart "labrat-$KIND"
echo
echo "labrat-$KIND is running: systemctl status labrat-$KIND"
[ "$KIND" = rat ] && echo "its key: sudo labrat rat key --config $CONF/rat.toml"
exit 0
