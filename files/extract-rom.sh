#!/bin/bash
# Pull RISCOS.IMG out of RISC OS Open's SD card image.
#
# Used by the release-files job, which runs on its own without any image
# build, so it cannot reuse what hooks/host_prepareImage.sh extracts -- it
# fetches the archive itself. The ROM is a release asset because the raspi
# machines have no firmware of their own for QEMU to fall back on and
# anyvm.py cannot read it out of the qcow2 without parsing a partition table
# and a FAT volume on every boot.
#
# Deliberately no qemu-nbd here: the archive holds a raw image, and mtools
# reads a FAT volume straight out of a file at an offset, so this needs no
# root and no kernel module -- unlike prepareImage, which has to work on the
# already-converted qcow2.

set -euo pipefail

OUT="${1:-build}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# Kept in step with VM_VHD_LINK in conf/riscos-5.30-armv7.conf. Read it from
# there rather than repeating the URL, so a release bump only has to change
# one file (the upstream watcher edits the conf).
CONF="$HERE/../conf/riscos-5.30-armv7.conf"
URL="$(sed -n 's/^VM_VHD_LINK="\(.*\)"$/\1/p' "$CONF" | head -1)"
if [ -z "$URL" ]; then
    echo "FATAL: no VM_VHD_LINK found in $CONF" >&2
    exit 1
fi

mkdir -p "$OUT"
ZIP="$OUT/riscos-sd.zip"
RAW="$OUT/riscos-sd.img"

echo "=== extract-rom: $URL ==="
if [ ! -f "$ZIP" ]; then
    curl -fL --retry 3 -o "$ZIP" "$URL"
fi

if [ ! -f "$RAW" ]; then
    member="$(unzip -Z1 "$ZIP" | grep -iE '\.img$' | head -1)"
    if [ -z "$member" ]; then
        echo "FATAL: no *.img member in $ZIP" >&2
        unzip -Z1 "$ZIP" >&2
        exit 1
    fi
    echo "--- unpacking $member ---"
    unzip -p "$ZIP" "$member" > "$RAW"
fi

# Read the FAT partition's offset from the table instead of hardcoding sector
# 10, so a repartitioned upstream image still works.
fat_start="$(partx -g -o START -n 1:1 "$RAW" | tr -d ' ')"
if [ -z "$fat_start" ]; then
    echo "FATAL: could not read the FAT partition offset" >&2
    partx -o NR,START,SECTORS,TYPE "$RAW" >&2 || true
    exit 1
fi
echo "--- FAT partition at sector $fat_start ---"

MTOOLS_SKIP_CHECK=1 mcopy -o -i "$RAW@@$((fat_start * 512))" ::RISCOS.IMG "$OUT/RISCOS.IMG"
ls -l "$OUT/RISCOS.IMG"

# The raw image is several GB; the release job has no use for it afterwards
# and the runner's disk is not generous.
rm -f "$RAW" "$ZIP"

echo "=== extract-rom: done ==="
