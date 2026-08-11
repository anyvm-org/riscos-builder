# enablessh hook for RISC OS. Host-side python, exec()'d into build.py's
# globals.
#
# There is no sshd to enable. What this hook does is turn the build-time
# scaffold into a shipped image: the agent is running only because the
# patched Tasks template pulled it over HTTP from this host and exec'd it,
# which works exactly once and only while the build host is there. Here it is
# written to disc as a real file and given a real Tasks entry, so the exported
# image comes up on its own. main() REQUIRES an enablessh hook for a
# telnet-transport guest -- it aborts when none ran.
#
# The agent installs itself, through its own tar channel. That is not a
# flourish: it is the only way to CREATE a file on a FileCore volume from
# outside the guest. files/rofilecore.py can overwrite a file in place at its
# existing length, but it cannot allocate, so it can never add one.
#
# One thing deliberately does NOT happen here: putting the Tasks template
# back. RISC OS holds an Obey file open for as long as it is executing, and
# the agent was started BY that template and never returns, so a write to it
# fails with `error 88 -- this file is already open`. The restore is offline,
# in hooks/host_finalizeImage.sh, once the machine is down.

import io as _ro_io
import socket as _ro_socket
import tarfile as _ro_tarfile
import time as _ro_time

_ro_port = int(read_state(env("VM_OS_NAME"), "sshport") or "2222")

# A host_*.py hook is exec()'d INTO build.py's own globals, so `__file__` here
# is build.py -- NOT this file. dirname() is therefore already the repo root,
# and the "hooks/.. " step a reader expects would climb one level too far
# (that shipped once: '<repo>/../files/anyvmd.py: No such file or directory').
# The .sh hooks are real subprocesses and do compute HERE/../files correctly.
_ro_files = os.path.join(os.path.dirname(os.path.abspath(__file__)), "files")
if not os.path.isdir(_ro_files):
    log("FATAL: riscos enablessh: cannot find the files/ directory (looked in "
        "%s)" % _ro_files)
    sys.exit(1)

AGENT_DIR = "/AnyVM"                       # RISC OS $.AnyVM
AGENT_RO = "$.AnyVM.anyvmd/py"
TASKS_DIR = "/!Boot/Choices/Boot/Tasks"
TASKS_RO = "$.!Boot.Choices.Boot.Tasks.AnyVM"

_RO_IAC, _RO_WILL, _RO_DO = 255, 251, 253


def _ro_push(directory, members):
    """Host tar stream -> the agent's tarfile extractor, over telnet.

    Same wire shape as anyvm.py's _tar_push_telnet, which is what the agent
    was written against: negotiate BINARY, send the command, then the archive
    with every 0xFF doubled, and wait for the marker the agent prints once
    tarfile returned cleanly."""
    sock = _ro_socket.create_connection(("127.0.0.1", _ro_port), 20)
    got = bytearray()
    try:
        sock.sendall(bytes([_RO_IAC, _RO_WILL, 0, _RO_IAC, _RO_DO, 0]))
        _ro_time.sleep(2.0)
        sock.sendall(("mkdir -p '%s' && cd '%s' && tar -xf -\r\n"
                      % (directory, directory)).encode("latin-1"))
        _ro_time.sleep(1.0)
        buf = _ro_io.BytesIO()
        tf = _ro_tarfile.open(fileobj=buf, mode="w|",
                              format=_ro_tarfile.USTAR_FORMAT)
        for name, data in members.items():
            ti = _ro_tarfile.TarInfo("./" + name)
            ti.size = len(data)
            ti.mtime = 0
            ti.mode = 0o644
            tf.addfile(ti, _ro_io.BytesIO(data))
        tf.close()
        sock.sendall(buf.getvalue().replace(b"\xff", b"\xff\xff"))
        sock.settimeout(0.5)
        end = _ro_time.time() + 90
        while _ro_time.time() < end:
            try:
                chunk = sock.recv(4096)
            except _ro_socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            got += chunk
            if b"anyvm-tar-done" in got:
                return True
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


log("riscos enablessh: installing the agent onto the disc")

with open(os.path.join(_ro_files, "anyvmd.py"), "rb") as _fh:
    _ro_agent = _fh.read()
with open(os.path.join(_ro_files, "tasks-anyvm.obey"), "rb") as _fh:
    _ro_launcher = _fh.read().replace(b"\r\n", b"\n")

for _ro_dir, _ro_members in ((AGENT_DIR, {"anyvmd.py": _ro_agent}),
                             (TASKS_DIR, {"AnyVM": _ro_launcher})):
    if not _ro_push(_ro_dir, _ro_members):
        log("FATAL: riscos enablessh: no completion marker after pushing into "
            "%s -- the agent did not finish extracting." % _ro_dir)
        sys.exit(1)
    log("riscos enablessh: pushed %s" % _ro_dir)

# tar carries no RISC OS filetype, so everything lands as Text (&FFF).
# BootRun only runs a Tasks entry that is an Obey file (&FEB); without this
# the launcher would sit there being ignored on every boot.
_ro_ok, _ro_text = telnet_exec([
    "SetType %s Obey" % TASKS_RO,
    "Info %s" % TASKS_RO,
    "Info %s" % AGENT_RO,
    "Type %s" % TASKS_RO,
], settle=4.0)
log("riscos enablessh transcript:\n%s" % _ro_text)

if not _ro_ok:
    log("FATAL: riscos enablessh: the session dropped mid-check")
    sys.exit(1)

# Everything above can appear to work while shipping an image that is
# unreachable the moment anyvm.py boots it, so check what actually has to be
# true, read back from the guest rather than assumed from the writes.
if "Obey" not in _ro_text:
    log("FATAL: riscos enablessh: %s is not an Obey file, so BootRun will "
        "skip it and the exported image will never start the agent." % TASKS_RO)
    sys.exit(1)

if "TaskWindow" not in _ro_text:
    log("FATAL: riscos enablessh: %s does not contain the TaskWindow launch "
        "line." % TASKS_RO)
    sys.exit(1)

if "not found" in _ro_text.lower():
    log("FATAL: riscos enablessh: something the check asked for is missing; "
        "see the transcript above.")
    sys.exit(1)

# Nothing to tear down: the agent was fetched from build.py's own web server
# (startWeb), which the engine owns and stops itself. This hook used to kill
# a private server it had started, which is gone.
log("riscos enablessh: agent installed on disc and registered as a Task")
