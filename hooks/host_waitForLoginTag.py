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
# Getting there is a chain, and every link fails the same way from outside
# (a probe that never answers), so the log points at which one broke:
#   the patched Tasks template ran  ->  Python 2.7.2 started in a 6200K
#   WimpSlot  ->  the guest reached the build host over slirp at 10.0.2.2
#   ->  it fetched files/anyvmd.py  ->  the agent bound port 23.
# build/agent-http.log tells you how far it got. A fetch logged as HTTP/1.0
# is the guest (Python 2's urllib); HTTP/1.1 is the build host's own health
# check from the beforeBuild hook, so one lone 1.1 line means the guest never
# asked.

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
    log("       No guest fetch in the log below means the patched Tasks "
        "template did not run, or the guest has no network. A fetch with no "
        "probe answer afterwards means Python started and then died.")
    sh("cat %s/agent-http.log || true" % (env("VM_WORKDIR") or "build"))
    sys.exit(1)

log("riscos waitForLoginTag: agent is answering")
