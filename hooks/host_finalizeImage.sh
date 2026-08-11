#!/bin/bash
# Put the boot-time scaffold back, after the guest is down and before export.
#
# hooks/host_beforeBuild.sh overwrote $.!Boot.RO530Hook.Boot.Tasks.TimeSetup
# with a bootstrap that fetches the agent over HTTP from the build host. That
# was a one-shot: hooks/host_enablessh.py has since written the agent to disc
# and installed a real Tasks entry, so the shipped image must not carry a
# bootstrap that points at a machine it will never see again -- it would run
# on every boot, fail, and (because Obey is line by line and X-prefixed) do
# so silently while wasting the first seconds of every start.
#
# It has to happen here rather than in the guest: RISC OS holds an Obey file
# open while it executes, the agent was launched by TimeSetup and never
# returns, so writing to it from inside fails with `error 88 -- this file is
# already open`. Once the machine is down the file is just bytes again.
#
# TWO copies need restoring. The template we patched is the RO530Hook one,
# but RISC OS copied it into $.!Boot.Choices.Boot on first boot, and the
# Choices copy is the one that actually runs.

set -euo pipefail

WORK="${VM_WORKDIR:-build}"
QCOW="${VM_WORK_QCOW:-${VM_OS_NAME}.qcow2}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TOOL="$HERE/../files/rofilecore.py"
ORIG="$WORK/TimeSetup.orig"
NBD=/dev/nbd0

echo "=== riscos finalizeImage: restoring the stock TimeSetup ==="

if [ ! -f "$ORIG" ]; then
    echo "FATAL: $ORIG is missing -- beforeBuild did not save the original" >&2
    exit 1
fi

_cleanup() { sudo qemu-nbd --disconnect "$NBD" 2>/dev/null || true; }
trap _cleanup EXIT

sudo modprobe nbd max_part=16
sudo qemu-nbd --disconnect "$NBD" 2>/dev/null || true
sudo qemu-nbd --connect="$NBD" "$QCOW"
sudo partprobe "$NBD" 2>/dev/null || true
sleep 2
sudo chmod 0666 "$NBD" 2>/dev/null || true

# The template, addressed by path -- its fragment is always calibrated.
python3 "$TOOL" "$NBD" patch '$.!Boot.RO530Hook.Boot.Tasks.TimeSetup' "$ORIG" \
    --prefix 'X Obey $.Programming.Python.!Python27.!Boot'

# The live Choices copy. It was created by the guest on first boot, so its
# fragment holds no directory and was never calibrated; locate() finds it by
# content instead. The bootstrap's own first line is the marker, and it is
# unique now that the template has just been restored -- which is exactly why
# the template is restored FIRST. Reverse the order and there are two
# matching copies and the tool refuses (by design) rather than guessing.
python3 "$TOOL" "$NBD" patch '$.!Boot.Choices.Boot.Tasks.TimeSetup' "$ORIG" \
    --prefix 'X Obey $.Programming.Python.!Python27.!Boot'

echo "--- verify: no bootstrap left anywhere on the volume ---"
if sudo grep -c -a 'urllib.urlopen' "$NBD" 2>/dev/null | grep -qv '^0$'; then
    echo "FATAL: the HTTP bootstrap is still present in the image" >&2
    exit 1
fi

echo "--- verify: the permanent launcher is there ---"
python3 "$TOOL" "$NBD" cat '$.!Boot.Choices.Boot.Tasks.AnyVM' --prefix '| Start the anyvm agent'

sync
sudo qemu-nbd --disconnect "$NBD"

# `qemu-nbd --disconnect` returns once the kernel client is gone, but the
# server process still holds an exclusive write lock and exits asynchronously;
# the export needs that lock. Wait for both signals, same as reactos-builder's
# files/inject.sh and plan9-builder's prepareImage hook.
_nbd_name="$(basename "$NBD")"
for _try in $(seq 1 120); do
    if [ ! -e "/sys/block/${_nbd_name}/pid" ] \
       && qemu-img resize "$QCOW" +0 >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done
trap - EXIT

echo "=== riscos finalizeImage: done ==="
