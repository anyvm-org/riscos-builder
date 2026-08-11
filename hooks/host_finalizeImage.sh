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
MARKER="$WORK/bootstrap.marker"
NBD=/dev/nbd0

echo "=== riscos finalizeImage: restoring the stock TimeSetup ==="

if [ ! -f "$ORIG" ]; then
    echo "FATAL: $ORIG is missing -- beforeBuild did not save the original" >&2
    exit 1
fi

# Give the disc back the size prepareImage chose. createVMFromVHD() adds a
# flat +200G to every VHD-based image (the engine's default sparse disk; it
# does NOT consult VM_DISK_SIZE on that path), so by the time we get here the
# 4 GiB disc is 204 GiB. Nothing can live up there -- the partition table ends
# at 1.8 GiB and the guest cannot see past it -- but everything below walks
# the whole device: two rofilecore scans, a grep, and a read-back, i.e. four
# full-length reads. At 204 GiB that is a quarter of an hour of pure I/O per
# build, for 200 GiB of holes.
#
# Shrinking is safe above 2 GiB: QEMU's SD model only demands a power of two
# at or below SDSC_MAX_CAPACITY, and 512 KiB alignment above that
# (hw/sd/sd.c, sd_realize()). 4 GiB satisfies both.
#
# The partition end has to be read through nbd -- partx cannot see a
# partition table inside a qcow2 -- and the resize needs the image closed, so
# the device is attached once to measure, detached to shrink, and attached
# again for the real work. Attaching is instant; only the scans are slow, and
# those now happen on 4 GiB instead of 204.
_cleanup() { sudo qemu-nbd --disconnect "$NBD" 2>/dev/null || true; }
trap _cleanup EXIT

sudo modprobe nbd max_part=16
sudo qemu-nbd --disconnect "$NBD" 2>/dev/null || true
sudo qemu-nbd --connect="$NBD" "$QCOW"
sudo partprobe "$NBD" 2>/dev/null || true
sleep 2

_last_end="$(sudo partx -g -o END "$NBD" 2>/dev/null | tr -d ' ' | sort -n | tail -1)"
sudo qemu-nbd --disconnect "$NBD"
_nbd0="$(basename "$NBD")"
for _t in $(seq 1 120); do
    if [ ! -e "/sys/block/${_nbd0}/pid" ] \
       && qemu-img resize "$QCOW" +0 > /dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

if [ -n "$_last_end" ] && [ "$_last_end" -lt 8388608 ]; then
    echo "--- shrinking to 4 GiB (last partition ends at sector $_last_end) ---"
    qemu-img resize --shrink "$QCOW" 4294967296
    qemu-img info "$QCOW" | sed -n 's/^virtual size/    virtual size/p'
else
    # Not fatal: a bigger disc only costs time. But say so loudly, because it
    # means the layout is not what this hook was written against.
    echo "--- NOT shrinking: last partition ends at sector '${_last_end:-?}',"
    echo "    which is not below the 4 GiB mark; leaving the size alone ---"
fi

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
# Match on the agent URL, which prepareImage wrote out for exactly this, and
# not on the bootstrap's code: `urllib.urlopen` occurs 21 times on a stock
# RISC OS disc (Python 2.7's urllib.py, its .pyc, and half a dozen files in
# its test suite), so scanning for that could never report a clean image.
if [ ! -f "$MARKER" ]; then
    echo "FATAL: $MARKER is missing -- prepareImage did not record the" >&2
    echo "       bootstrap marker, so this check cannot be trusted" >&2
    exit 1
fi
_marker="$(head -1 "$MARKER")"
echo "    looking for: $_marker"
if sudo grep -c -a -F "$_marker" "$NBD" 2>/dev/null | grep -qv '^0$'; then
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
