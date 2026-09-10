#!/usr/bin/env bash
# Build and install the out-of-tree nct6687 driver for BC-250 fan control.
#
# Why this exists: the in-tree nct6683 driver binds the BC-250's NCT6686 but
# exposes pwmN read-only with no pwmN_enable, so fan control is impossible on a
# stock kernel. nct6687d fixes that.
#
# Four things make this awkward on an rpm-ostree system (Bazzite, Silverblue),
# and each one is handled below. All four were found the hard way.
#
#   1. kernel-devel may not match a custom kernel. If the running kernel is a
#      LocalOverride (e.g. ...bc250cu) there is often no matching -devel
#      package and /lib/modules/$(uname -r)/build is a dangling symlink. Where
#      CONFIG_MODVERSIONS is unset only the vermagic string must match, so the
#      stock tree for the same base version is copied and its release strings
#      corrected.
#   2. /usr/src is read-only, so that copy goes under /var.
#   3. /usr/lib/modules is read-only, so the built module cannot live where
#      modprobe searches. It goes under /var and is insmod-ed by absolute path.
#   4. SELinux blocks both halves of that. A module under /var/lib inherits
#      var_lib_t and the kernel refuses to load it with EACCES and no audit
#      record; and systemd cannot execute a loader script labelled var_lib_t
#      either. The module is relabelled modules_object_t and the loader is
#      installed to /usr/local/bin, which is bin_t.
#
# Re-run after any kernel change: vermagic is pinned to one kernel release and
# the module will silently refuse to load against another.
set -euo pipefail

KREL="$(uname -r)"
STATE_DIR=/var/lib/bc250-autotune
MODULE_DIR="$STATE_DIR/modules"
KBUILD_DIR="/var/lib/bc250-kbuild/$KREL"
SRC_DIR="${BC250_NCT6687_SRC:-$HOME/nct6687d-build}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\n== %s\n' "$1"; }

say "Checking the kernel build tree"
KDIR="/lib/modules/$KREL/build"
if [ -e "$KDIR/Makefile" ]; then
    echo "  using $KDIR"
else
    echo "  $KDIR is missing or dangling (custom kernel without matching -devel)"
    STOCK="$(ls -d /usr/src/kernels/* 2>/dev/null | head -1 || true)"
    [ -n "$STOCK" ] || { echo "  no kernel-devel at all; install one first" >&2; exit 1; }

    if grep -q '^CONFIG_MODVERSIONS=y' "$STOCK/.config" 2>/dev/null; then
        echo "  REFUSING: CONFIG_MODVERSIONS=y in $STOCK" >&2
        echo "  Symbol CRCs from a different build would not match; get the" >&2
        echo "  matching kernel-devel for $KREL instead." >&2
        exit 1
    fi

    echo "  copying $STOCK -> $KBUILD_DIR and correcting its release strings"
    sudo install -d -m 0755 "$(dirname "$KBUILD_DIR")"
    sudo rm -rf "$KBUILD_DIR"
    sudo cp -a "$STOCK" "$KBUILD_DIR"
    echo "$KREL" | sudo tee "$KBUILD_DIR/include/config/kernel.release" >/dev/null
    printf '#define UTS_RELEASE "%s"\n' "$KREL" \
        | sudo tee "$KBUILD_DIR/include/generated/utsrelease.h" >/dev/null
    KDIR="$KBUILD_DIR"
fi

say "Fetching and building nct6687d"
[ -d "$SRC_DIR" ] || git clone --depth 1 https://github.com/Fred78290/nct6687d.git "$SRC_DIR"
make -C "$KDIR" M="$SRC_DIR" modules
[ -f "$SRC_DIR/nct6687.ko" ] || { echo "  build produced no module" >&2; exit 1; }

BUILT_VERMAGIC="$(modinfo "$SRC_DIR/nct6687.ko" | awk '/^vermagic:/ {print $2}')"
echo "  built vermagic : $BUILT_VERMAGIC"
echo "  running kernel : $KREL"
[ "$BUILT_VERMAGIC" = "$KREL" ] || {
    echo "  vermagic does not match the running kernel; it will not load" >&2
    exit 1
}

say "Installing the module"
sudo install -d -m 0755 "$MODULE_DIR"
sudo install -m 0644 "$SRC_DIR/nct6687.ko" "$MODULE_DIR/nct6687-$KREL.ko"
# Without this the kernel refuses to load it: EACCES, and no audit record.
sudo chcon -t modules_object_t "$MODULE_DIR/nct6687-$KREL.ko" 2>/dev/null || true
command -v semanage >/dev/null 2>&1 &&
    sudo semanage fcontext -a -t modules_object_t "$MODULE_DIR(/.*)?" 2>/dev/null || true

say "Blacklisting the in-tree driver"
sudo tee /etc/modprobe.d/bc250-nct6687.conf >/dev/null <<'EOF'
# nct6683 (in-tree) and nct6687 (out-of-tree) bind the same NCT6686 Super I/O.
# The in-tree driver exposes pwmN read-only, so it cannot control fans at all.
# Blacklist it so the out-of-tree module gets the chip instead.
blacklist nct6683
EOF

say "Installing the loader and unit"
# /usr/local/bin is bin_t and, on ostree, a symlink to writable /var/usrlocal.
# systemd cannot exec a script labelled var_lib_t.
sudo install -m 0755 "$HERE/bc250-load-nct6687" /usr/local/bin/bc250-load-nct6687
sudo restorecon -v /usr/local/bin/bc250-load-nct6687 2>/dev/null || true
sudo install -m 0644 "$HERE/bc250-nct6687.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bc250-nct6687.service

say "Verifying"
sleep 2
if ! lsmod | grep -q '^nct6687[[:space:]]'; then
    echo "  module did not load; check: journalctl -u bc250-nct6687" >&2
    exit 1
fi
HWMON=""
for h in /sys/class/hwmon/hwmon*; do
    case "$(cat "$h/name" 2>/dev/null)" in nct668*) HWMON="$h"; break;; esac
done
[ -n "$HWMON" ] || { echo "  module loaded but bound no hwmon device" >&2; exit 1; }

if [ -w "$HWMON/pwm1" ]; then
    echo "  fan control ENABLED: $HWMON/pwm1 is writable"
    echo "  unit: $(systemctl is-active bc250-nct6687)"
    echo
    echo "  Re-run this script after any kernel change."
else
    echo "  pwm1 is still read-only; the in-tree driver may have won the race" >&2
    exit 1
fi
