#!/usr/bin/env bash
# Phase 0 recon: inventory a BC-250 box. READ-ONLY -- touches no hardware state.
# Run on the target: bash phase0_recon.sh
set -uo pipefail

hr() { printf '\n===== %s =====\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }
try() { if have "$1"; then "$@" 2>&1 | head -40; else echo "MISSING: $1"; fi; }

hr "HOST / DISTRO"
uname -a
[ -r /etc/os-release ] && cat /etc/os-release
echo "-- ostree/immutable --"
have rpm-ostree && rpm-ostree status --booted 2>&1 | head -20 || echo "rpm-ostree: absent"
echo "writable /usr? "; [ -w /usr ] && echo yes || echo "no (immutable)"

hr "SESSION / DISPLAY (screenshot backend)"
echo "XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-unset}"
echo "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-unset}  DISPLAY=${DISPLAY:-unset}"
echo "XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-unset}"
loginctl list-sessions 2>&1 | head -10
for t in grim scrot import spectacle gnome-screenshot ksnip; do
  printf '%-18s %s\n' "$t" "$(command -v $t || echo MISSING)"
done

hr "BC-250 PCI PRESENCE"
if have lspci; then
  lspci -nn | grep -iE "amd|ati|vga|display" | head -20
  echo "-- Cyan Skillfish (1002:13fe / 1002:143f) --"
  lspci -nn | grep -iE "13fe|143f" || echo "no cyan-skillfish PCI id matched"
else echo "MISSING: lspci"; fi

hr "AMDGPU / DRM"
ls -d /sys/class/drm/card* 2>/dev/null
for c in /sys/class/drm/card*/device; do
  [ -e "$c/uevent" ] || continue
  echo "-- $c --"
  grep -E "DRIVER|PCI_ID" "$c/uevent" 2>/dev/null
  for f in pp_dpm_sclk power_dpm_force_performance_level gpu_busy_percent; do
    [ -r "$c/$f" ] && { echo "  $f:"; head -20 "$c/$f" | sed 's/^/    /'; }
  done
done

hr "HWMON INVENTORY (fan/temp/power sources)"
for h in /sys/class/hwmon/hwmon*; do
  [ -r "$h/name" ] || continue
  n=$(cat "$h/name")
  echo "-- $h  name=$n"
  ls "$h" | grep -E "^(temp[0-9]+_input|fan[0-9]+_input|pwm[0-9]+|power[0-9]+_average|in[0-9]+_input)$" \
    | sed 's/^/    /' | head -25
done
echo "-- nct6687 present? --"
grep -l nct6687 /sys/class/hwmon/hwmon*/name 2>/dev/null || echo "nct6687 NOT loaded"
lsmod 2>/dev/null | grep -E "nct6687|amdgpu" || true

hr "UPSTREAM TOOLING PRESENT"
for t in cyan-skillfish-governor-smu bc250-detect bc250-apply bc250-cu-live-manager \
         umr setpci amdgpu_top sensors stress stress-ng furmark FurMark_GUI dkms python3; do
  printf '%-28s %s\n' "$t" "$(command -v $t || echo MISSING)"
done
echo "-- config/unit paths --"
for p in /etc/cyan-skillfish-governor-smu/config.toml /etc/bc250-smu-oc.conf \
         /etc/bc250-cu-live-manager.conf /etc/systemd/system/bc250-smu-oc.service; do
  printf '%-52s %s\n' "$p" "$([ -e "$p" ] && echo EXISTS || echo absent)"
done

hr "GOVERNOR SERVICE + D-BUS"
for s in cyan-skillfish-governor-smu cyan-skillfish-governor oberon-governor \
         bc250-smu-oc bc250-cu-live-manager; do
  printf '%-32s active=%-10s enabled=%s\n' "$s" \
    "$(systemctl is-active $s 2>/dev/null || echo -)" \
    "$(systemctl is-enabled $s 2>/dev/null || echo -)"
done
if have busctl; then
  echo "-- system bus name --"
  busctl list 2>/dev/null | grep -i cyanskillfish || echo "com.cyanskillfish.Governor NOT on system bus"
  echo "-- introspect PerformanceMode --"
  busctl introspect com.cyanskillfish.Governor /com/cyanskillfish/Governor 2>&1 | head -30
  echo "-- Allowed range --"
  busctl introspect com.cyanskillfish.Governor /com/cyanskillfish/Governor/Range/Allowed 2>&1 | head -15
else echo "MISSING: busctl"; fi

hr "CPU"
lscpu 2>/dev/null | grep -E "Model name|^CPU\(s\)|Thread|Core|MHz" | head -10
echo "threads present: $(nproc --all 2>/dev/null)  online: $(nproc 2>/dev/null)"
grep MHz /proc/cpuinfo 2>/dev/null | head -8

hr "PRIVILEGE"
echo "user=$(id -un) uid=$(id -u)"
sudo -n true 2>/dev/null && echo "passwordless sudo: YES" || echo "passwordless sudo: NO (or needs tty)"

hr "DONE"
