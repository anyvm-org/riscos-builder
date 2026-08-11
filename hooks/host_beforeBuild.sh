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
# 3. Serve the agent for the injected bootstrap to fetch.
#
# It has to be listening before the guest boots, which is why it starts here
# and not in the enablessh hook.  10.0.2.2 is the slirp gateway, i.e. this
# host, so the server binds loopback and is reachable from nowhere else.
# hooks/host_enablessh.py stops it once the agent is installed on disc.
# ---------------------------------------------------------------------------
SERVE="$WORK/serve"
mkdir -p "$SERVE"
cp "$FILES/anyvmd.py" "$SERVE/anyvmd.py"
if [ -f "$WORK/agent-http.pid" ] && kill -0 "$(cat "$WORK/agent-http.pid")" 2>/dev/null; then
    echo "--- agent HTTP server already running ---"
else
    ( cd "$SERVE" && setsid python3 -m http.server "${VM_AGENT_HTTP_PORT:-8099}" \
        --bind 127.0.0.1 > "$WORK/agent-http.log" 2>&1 & echo $! > "$WORK/agent-http.pid" )
    sleep 2
fi
curl -fsS -o /dev/null "http://127.0.0.1:${VM_AGENT_HTTP_PORT:-8099}/anyvmd.py" \
    && echo "--- agent HTTP server is up on ${VM_AGENT_HTTP_PORT:-8099} ---" \
    || { echo "FATAL: the agent HTTP server did not come up" >&2; exit 1; }

echo "=== riscos beforeBuild: done ==="
