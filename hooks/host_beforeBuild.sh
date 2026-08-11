#!/bin/bash
# Build inputs that have to exist before setup() runs.
#
# Runs at build.py's earliest hook point.  Note what is NOT done here: the
# disc image is not prepared.  clearVM() runs AFTER this hook and deletes
# both $VM_WORKDIR/<osname>.qcow2 and <osname>.img, so anything written to
# those names would be thrown away before _prep_vhd_disk() ever looked at it
# -- and silently, with the build failing much later at "the agent never
# answered".  Image preparation is in hooks/host_prepareImage.sh, which runs
# after _prep_vhd_disk() has materialised the qcow2.
#
# The download IS done here, to $VM_WORKDIR/<osname>.imgzip.  clearVM() does
# not touch that name and _prep_vhd_disk()'s zip branch reuses an archive it
# finds there, so fetching it early costs nothing and keeps a rebuild from
# pulling 155 MB again.
#
# Products, all under $VM_WORKDIR:
#   qemu-10.2.3-riscos-arm-noble.tar.zst   the patched QEMU (VM_QEMU_TAR)
#   riscos.imgzip                          ROOL's archive, for _prep_vhd_disk
#   serve/anyvmd.py + a live HTTP server   the agent, for the guest to fetch
#
# Nothing from RISC OS Open is committed to this repo; it is all fetched here.

set -euo pipefail

WORK="${VM_WORKDIR:-build}"
HERE="$(cd "$(dirname "$0")" && pwd)"
FILES="$HERE/../files"
OSNAME="${VM_OS_NAME:-riscos}"
ZIP_URL="${VM_VHD_LINK:?VM_VHD_LINK must be set by the conf}"
ZIP="$WORK/$OSNAME.imgzip"

mkdir -p "$WORK"

echo "=== riscos beforeBuild ==="

# ---------------------------------------------------------------------------
# 0. Host deps.
#
# mtools is the one the runner does not already have, and it is needed by
# hooks/host_prepareImage.sh to lift RISCOS.IMG out of the image's FAT boot
# partition. Installed here, in the FIRST hook, so prepareImage can rely on
# it -- discovering it missing there costs a QEMU build and a 155 MB download
# first. Everything else these hooks reach for is already on the runner:
# partx, fdisk, qemu-img, qemu-nbd, curl, patch, make. The archive itself is
# never unzipped here -- _prep_vhd_disk() does that with Python's zipfile.
# ---------------------------------------------------------------------------
echo "--- host deps ---"
export DEBIAN_FRONTEND=noninteractive
sudo -E apt-get update -q
sudo -E apt-get install -y -q --no-install-recommends mtools

# ---------------------------------------------------------------------------
# 1. The patched QEMU.  Built here rather than committed: 30 MB of binaries do
#    not belong in git, and this repo must not reach for a sibling's copy.
# ---------------------------------------------------------------------------
if [ ! -f "$WORK/qemu-10.2.3-riscos-arm-noble.tar.zst" ]; then
    echo "--- building the patched QEMU ---"
    # Invoked through `bash` rather than executed, exactly as netbsd-builder
    # and openbsd-builder invoke theirs. Nothing in files/ carries the
    # executable bit in this fleet -- a repo authored on Windows cannot
    # record one -- so running it directly fails with "Permission denied"
    # and exit 126, which is what happened on the first CI run.
    bash "$FILES/build-qemu-riscos.sh" "$WORK"
else
    echo "--- patched QEMU tarball already present ---"
fi

# ---------------------------------------------------------------------------
# 2. ROOL's SD image archive.
# ---------------------------------------------------------------------------
if [ ! -f "$ZIP" ]; then
    echo "--- downloading $ZIP_URL ---"
    # aria2c first (segmented; ROOL answers Range requests), curl as the
    # fallback -- and the fallback is not theoretical. On ubuntu-24.04 runners
    # aria2c cannot complete a TLS handshake with riscosopen.org at all:
    #   SSL/TLS handshake failure: A TLS fatal alert has been received.
    # curl to the same URL from the same runner is fine, so this is aria2's
    # TLS backend, not the network. Trying aria2c inside the `if` keeps
    # `set -e` from killing the build on that failure.
    if command -v aria2c >/dev/null 2>&1 \
       && aria2c -c -x8 -s8 -d "$WORK" -o "$(basename "$ZIP")" "$ZIP_URL"; then
        echo "--- fetched with aria2c ---"
    else
        echo "--- aria2c unavailable or failed; falling back to curl ---"
        rm -f "$ZIP"
        curl -fL --retry 3 -o "$ZIP" "$ZIP_URL"
    fi
else
    echo "--- $ZIP already present ---"
fi
ls -l "$ZIP"

# ---------------------------------------------------------------------------
# 3. Serving the agent to the guest: nothing to do here.
#
# build.py's startWeb() already runs `python -m http.server` over the REPO
# ROOT, on the default port 8000, and it starts immediately after this hook
# and before the VM does -- so files/anyvmd.py is reachable from inside the
# guest as http://192.168.122.1:8000/files/anyvmd.py by the time the injected
# bootstrap runs. That is the same host and port ghostbsd already fetches its
# install answer file from.
#
# This hook used to stand up its own server on 8099 and point the bootstrap
# at 10.0.2.2. Both were wrong: build.py configures slirp as
# net=192.168.122.0/24,host=192.168.122.1, so 10.0.2.2 -- QEMU's DEFAULT
# gateway, which is what a bare `-netdev user` gives you and therefore what
# local testing saw -- is not the host here and never answers.
# ---------------------------------------------------------------------------

echo "=== riscos beforeBuild: done ==="
