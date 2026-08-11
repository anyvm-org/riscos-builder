#!/bin/bash
# Build the PATCHED qemu-system-arm this builder's RISC OS images need at
# run time, from the upstream source tarball, and package it as
#   <outdir>/qemu-10.2.3-riscos-arm-noble.tar.zst
# with a pruned, self-contained layout:
#   qemu10-riscos-arm/bin/qemu-system-arm
#   qemu10-riscos-arm/share/qemu/keymaps/
# (QEMU locates its datadir relative to the binary, so the tree works from
# any extraction directory.)
#
# Why pinned at all: RISC OS does not boot on ANY released QEMU. Getting it
# to run needed eight fixes, four of them in code that has nothing to do
# with RISC OS, plus one device QEMU has never had. All of it lives in
# files/qemu-riscos-raspi.patch. In summary:
#
#  1. Mailbox channel 0 (VideoCore power management) was never implemented,
#     so the HAL's request to power up USB was answered by nobody and the
#     boot spun on MAIL0_STATUS forever. New bcm2835_mbox_power device.
#  2. bcm2835_ic rejected byte reads. RISC OS dispatches interrupts with
#     LDRB on the basic pending register, and a rejected access becomes an
#     external data abort inside the guest's own IRQ handler -- 3170 IRQs
#     against exactly 3170 aborts before the fix.
#  3. bcm2835_i2c rejected byte access to its FIFO, which RISC OS drives
#     with LDRB/STRB. This was the DataAbort at &FC008620.
#  4. Mailbox channel 3 (VCHIQ) had no peer at all, and RISC OS's connect
#     has no timeout, so BCMVideo's initialisation blocked forever and no
#     display was ever set up. New bcm2835_mbox_vchiq device: it completes
#     the CONNECT handshake and then refuses every service with a CLOSE,
#     which is a state the protocol defines and the guest handles cleanly.
#  5. dwc2 counted frames by dividing a nanosecond delta by a bit-time
#     count, so HFNUM advanced 83 frames per frame and wrapped every 0.2s.
#  6. dwc2 started framing the bus when a device was plugged in rather than
#     when the guest enabled the port.
#  7. dwc2 never re-evaluated GINTSTS.HCHINT on a HAINTMSK write. HCHINT is
#     a read-only summary of HAINT & HAINTMSK, so a driver that quietens a
#     channel by masking it -- which RISC OS does after every control
#     transfer -- left it asserted and spun in its interrupt handler.
#  8. bcm2835_dma subtracted four from a byte count that a guest is free to
#     make odd, so the length wrapped and the copy loop ran about 2^30
#     times, hanging QEMU inside the guest's MMIO write and scribbling far
#     past the destination on the way.
#
# Items 5 to 8 are plain upstream bugs; any guest can trip 8 in particular.
#
# The extra device is hw/usb/dev-smsc95xx.c, an SMSC LAN9512 -- the NIC on
# a real Raspberry Pi. QEMU models no Pi NIC at all, so a guest without a
# CDC/RNDIS driver has no networking on these machines; RISC OS's EtherUSB
# binds only real parts (smsc95xx, ax88772, mcs7830, pegasus).
#
# Usage: bash files/build-qemu-riscos.sh <outdir>
#
# Intended host: ubuntu-24.04 (noble) -- the GitHub Actions runner image
# (the "noble" in the tarball name). The tarball is NOT committed to git:
# the release-files job (.github/data/uploadfiles.yml) builds and uploads
# it beside the image assets; anyvm.py downloads it at run time and falls
# back to the system QEMU elsewhere. Self-contained by design: builders
# never reference other builders' files or release assets.
set -e

QEMU_VER=10.2.3

OUTDIR="$1"
if [ -z "$OUTDIR" ]; then
  echo "usage: $0 <outdir>" >&2
  exit 1
fi
ROOT="$(pwd)"
mkdir -p "$OUTDIR"
OUT="$ROOT/$OUTDIR"
PATCH="$ROOT/files/qemu-riscos-raspi.patch"
[ -s "$PATCH" ] || { echo "missing $PATCH" >&2; exit 1; }

SUDO=sudo
[ "$(id -u)" = 0 ] && SUDO=

export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq build-essential ninja-build pkg-config \
  python3-venv libglib2.0-dev libpixman-1-dev libslirp-dev libfdt-dev \
  zlib1g-dev wget xz-utils zstd >/dev/null

WORK=$(mktemp -d /tmp/qemu-riscos-build.XXXXXX)
echo "build dir: $WORK (left in place; /tmp is ephemeral)"
cd "$WORK"
wget -q "https://download.qemu.org/qemu-${QEMU_VER}.tar.xz"
tar xf "qemu-${QEMU_VER}.tar.xz"
cd "qemu-${QEMU_VER}"

patch -p1 < "$PATCH"

# Same feature trim as the other anyvm pinned-QEMU builds: no GUI, no docs,
# no storage/remote backends the runtime never uses; slirp + VNC + system
# fdt kept (anyvm drives guests over user networking and VNC).
./configure --target-list=arm-softmmu --prefix="$WORK/install" \
  --disable-docs --disable-gtk --disable-sdl --disable-opengl \
  --disable-virglrenderer --disable-spice --disable-smartcard \
  --disable-usb-redir --disable-libiscsi --disable-rbd --disable-glusterfs \
  --disable-libnfs --disable-seccomp --disable-linux-aio --disable-libusb \
  --disable-tpm --enable-slirp --enable-vnc --enable-fdt=system \
  > "$WORK/configure.log" 2>&1 || { tail -30 "$WORK/configure.log"; exit 1; }
make -j"$(nproc)" > "$WORK/make.log" 2>&1 || { tail -30 "$WORK/make.log"; exit 1; }
make install > /dev/null

pkg="$WORK/pkg"
mkdir -p "$pkg/qemu10-riscos-arm/bin" "$pkg/qemu10-riscos-arm/share/qemu"
cp "$WORK/install/bin/qemu-system-arm" "$pkg/qemu10-riscos-arm/bin/"
cp -r "$WORK/install/share/qemu/keymaps" \
      "$pkg/qemu10-riscos-arm/share/qemu/keymaps"

# The raspi machines need no firmware blob -- the ROM is passed with -bios
# and there are no option ROMs on this board -- so nothing else rides along.

bin="$pkg/qemu10-riscos-arm/bin/qemu-system-arm"
"$bin" --version | head -1
# Prove the two new devices survived the trim; a silent drop would only
# show up as a boot hang much later.
"$bin" -device help 2>&1 | grep -q 'usb-net-smsc95xx' \
  || { echo "smsc95xx device missing from the build" >&2; exit 1; }
"$bin" -M raspi2b -device help >/dev/null 2>&1 \
  || { echo "raspi2b machine missing from the build" >&2; exit 1; }
echo "device check: ok"

out="$OUT/qemu-${QEMU_VER}-riscos-arm-noble.tar.zst"
tar --zstd -cf "$out" -C "$pkg" qemu10-riscos-arm
ls -la "$out"
sha256sum "$out"
echo "build-qemu-riscos: done"
