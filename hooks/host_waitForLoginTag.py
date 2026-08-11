# waitForLoginTag hook for RISC OS. Host-side python, exec()'d into
# build.py's globals.
#
# There is no login prompt to wait for and no console to read it from: the
# BCM2835 ROM has no USB keyboard driver, so RISC OS boots straight to the
# desktop announcing "No keyboard present - autobooting" and never asks
# anything. The engine's console login-tag wait could only ever time out
# here, and a waitForLoginTag hook short-circuits start_and_wait(), so this
# replaces it with the only readiness signal that means anything for this
# guest: the anyvm agent answering the marker probe.
#
# It has to be a PROBE, not a connect. QEMU's slirp binds the host side of a
# hostfwd the moment QEMU starts and only tries to reach the guest when data
# flows, so a bare TCP connect to the forwarded port succeeds immediately --
# before RISC OS has even loaded its ROM. Measured: "up after 5s" on a boot
# that takes ninety. _telnet_ready_check() sends VM_TELNET_PROBE_CMD and
# looks for VM_TELNET_PROBE_MARKER coming back, which nothing but the agent
# can produce.
#
# Getting there is a chain, and every link fails the same way from outside --
# a probe that never answers:
#   the patched Tasks template ran  ->  Python 2.7.2 started in a 6200K
#   WimpSlot  ->  the guest reached the build host over slirp at
#   192.168.122.1  ->  it fetched files/anyvmd.py from build.py's own web
#   server  ->  the agent bound port 23.
# There is no access log to consult: startWeb() sends that server's output to
# DEVNULL. What this hook can offer instead is the console -- RISC OS writes
# nothing to serial, but the web console screenshot build.py captures shows
# how far the desktop got, and a boot stopped on an error box is the classic
# shape (an un-X-prefixed command in an injected script, which this guest
# cannot dismiss because it has no keyboard).

import time as _ro_time

_ro_deadline = _ro_time.time() + int(env("VM_LOGIN_MAX_SECONDS") or "900")

log("riscos waitForLoginTag: waiting for the agent to answer the marker probe")

_ro_up = False
while _ro_time.time() < _ro_deadline:
    if _telnet_ready_check():
        _ro_up = True
        break
    _ro_time.sleep(5)

if not _ro_up:
    log("FATAL: riscos waitForLoginTag: the agent never answered the probe.")
    log("       Look at the captured console below and at screen.png in the "
        "build artifacts: a desktop with an error box means an injected "
        "script raised one (every line must be X-prefixed -- this guest has "
        "no keyboard to dismiss it); a bare desktop means the Tasks entry "
        "ran but Python or the fetch failed.")
    sh("cat _screenText.txt 2>/dev/null || true")
    sys.exit(1)

log("riscos waitForLoginTag: agent is answering")
