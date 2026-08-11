"""anyvmd -- the anyvm remote-exec agent for RISC OS.

RISC OS ships no remote-access server of any kind.  Checked in a running
guest rather than assumed: *Modules lists 137 modules and the only
networking among them is the stack (Internet, Resolver, DHCP, EtherUSB,
MbufManager) and clients (LanManFS for SMB, ShareFS/Freeway for Acorn
Access).  No telnetd, no sshd, no ftpd.  So this agent is the server, the
same way reactos-builder has to supply anyvmtd.exe.

It speaks telnet, on port 23, because that is the transport anyvm.py and
build.py already implement end to end -- readiness probing, `-- cmd`
passthrough, the interactive-shell fallback, tar in both directions.  The
telnet here is real, not a raw socket wearing the name: IAC is unescaped
inbound and doubled outbound, and BINARY (RFC 856) is accepted in both
directions so the tar stream survives.

What it does NOT do is run a shell, because RISC OS has none in the POSIX
sense -- no pipes to a child, no `&&`.  anyvm sends one-liners built for a
shell; this agent recognises the two tar shapes and services them with
Python's tarfile, and hands everything else to the command line interpreter
with output redirected to a scrap file.  Python is not something the builder
installs: RISC OS Open's image already ships Python 2.7.2 at
$.Programming.Python.!Python27 with a complete standard library.

Nothing here may write to the console.  The agent runs from a Tasks hook
under the Wimp, and any console output opens the single-tasking output
screen, which waits for a keypress at the end -- and the BCM2835 ROM has no
USB keyboard driver, so that wait never ends.  Diagnostics go to a log FILE.
"""

import os
import select
import socket
import sys
import tarfile
import traceback

PORT = 23
BACKLOG = 4
CHUNK = 8192
POLL = 0.25

IAC = "\xff"
DONT, DO, WONT, WILL = "\xfe", "\xfd", "\xfc", "\xfb"
SB, SE = "\xfa", "\xf0"
OPT_BINARY = "\x00"

# anyvm chains this onto the tar command with `&& echo ...` for shells that
# have `&&`.  RISC OS has no such operator, so the agent prints it itself
# once tarfile has finished without raising.
TAR_DONE = "anyvm-tar-done"

LOGFILE = "<Wimp$ScrapDir>.anyvmlog"


def log(msg):
    try:
        fh = open(LOGFILE, "ab")
        try:
            fh.write("anyvmd: %s\n" % (msg,))
        finally:
            fh.close()
    except Exception:
        pass


class Telnet(object):
    """The connection, with telnet framing handled in both directions.

    Reads return payload only: IAC IAC collapses to one 0xFF, negotiation is
    answered and removed.  Writes double every 0xFF.  Both matter -- a tar
    stream is binary, and a single unescaped 0xFF would desynchronise it."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = ""       # decoded payload, waiting to be read
        self.raw = ""       # bytes received but not yet decodable
        self.eof = False

    # -- input ------------------------------------------------------------
    def _negotiate(self, cmd, opt):
        """Accept BINARY both ways, refuse everything else.

        The host opens with IAC WILL BINARY / IAC DO BINARY and warns loudly
        if the guest does not agree, because without it a real telnetd's pty
        would map NL to CR-NL in the outbound archive -- damage the tar
        checksum catches but cannot undo."""
        if opt == OPT_BINARY:
            if cmd == WILL:
                return IAC + DO + opt
            if cmd == DO:
                return IAC + WILL + opt
            return ""
        if cmd == WILL:
            return IAC + DONT + opt
        if cmd == DO:
            return IAC + WONT + opt
        return ""

    def _fill(self):
        """Pull one chunk off the wire and decode as much of it as is whole.

        An IAC sequence can straddle a recv boundary -- IAC alone at the end
        of one chunk, its command byte at the start of the next. Anything not
        yet decodable stays in self.raw for the following chunk. Consuming it
        early would silently drop a 0xFF, which in a tar stream means a
        corrupt archive rather than a visible error."""
        try:
            data = self.sock.recv(CHUNK)
        except socket.error:
            self.eof = True
            return
        if not data:
            self.eof = True
            # Whatever is left cannot be completed; treat it as payload.
            self.buf += self.raw
            self.raw = ""
            return
        data = self.raw + data
        self.raw = ""
        out = []
        reply = []
        i, n = 0, len(data)
        while i < n:
            ch = data[i]
            if ch != IAC:
                out.append(ch)
                i += 1
                continue
            if i + 1 >= n:
                self.raw = data[i:]
                break
            nxt = data[i + 1]
            if nxt == IAC:
                out.append(IAC)
                i += 2
            elif nxt in (WILL, WONT, DO, DONT):
                if i + 2 >= n:
                    self.raw = data[i:]
                    break
                reply.append(self._negotiate(nxt, data[i + 2]))
                i += 3
            elif nxt == SB:
                j = i + 2
                while j + 1 < n and not (data[j] == IAC and data[j + 1] == SE):
                    j += 1
                if j + 1 >= n:
                    self.raw = data[i:]
                    break
                i = j + 2
            else:
                i += 2
        if reply:
            try:
                self.sock.sendall("".join(reply))
            except socket.error:
                pass
        self.buf += "".join(out)

    def read(self, want):
        """Exactly `want` bytes of payload, or fewer at end of stream. This is
        the fileobj tarfile extracts from."""
        while len(self.buf) < want and not self.eof:
            self._fill()
        out, self.buf = self.buf[:want], self.buf[want:]
        return out

    def read_line(self):
        """One command line, without its terminator. None at end of stream.

        Telnet line endings are CR LF, and a bare CR is sent as CR NUL, so
        both trailing bytes have to go."""
        while True:
            for sep in ("\n", "\r"):
                pos = self.buf.find(sep)
                if pos >= 0:
                    line = self.buf[:pos]
                    rest = self.buf[pos + 1:]
                    if rest[:1] in ("\n", "\x00"):
                        rest = rest[1:]
                    self.buf = rest
                    return line.rstrip("\r")
            if self.eof:
                line, self.buf = self.buf, ""
                return line or None
            self._fill()

    # -- output -----------------------------------------------------------
    def write(self, data):
        if IAC in data:
            data = data.replace(IAC, IAC + IAC)
        try:
            self.sock.sendall(data)
        except socket.error:
            self.eof = True

    def flush(self):
        pass


def unquote(word):
    if len(word) >= 2 and word[0] == "'" and word[-1] == "'":
        return word[1:-1]
    return word


def parse_tar_command(line):
    """Recognise the two one-liners anyvm sends:

        mkdir -p 'DIR' && cd 'DIR' && tar -xf -      (push, host -> guest)
        cd 'DIR' && tar -cf - .                      (pull, guest -> host)

    Returns ('extract', dir), ('create', dir) or None.  The tar spelling is
    matched loosely because anyvm varies it per guest."""
    parts = [p.strip() for p in line.split("&&")]
    directory = None
    action = None
    for part in parts:
        if part.startswith("mkdir "):
            continue
        if part.startswith("cd "):
            directory = unquote(part[3:].strip())
            continue
        words = part.split()
        if not words or words[0] != "tar":
            return None
        flags = "".join(w.lstrip("-") for w in words[1:]
                        if w.startswith("-") or w in ("xf", "cf", "x", "c"))
        if "x" in flags:
            action = "extract"
        elif "c" in flags:
            action = "create"
        else:
            return None
    if action is None or directory is None:
        return None
    return action, directory


def safe_name(name):
    """tar streams name their members './x'.  Left alone, tarfile joins that
    onto the destination and then tries to makedirs a path ending in '/.',
    which fails with EEXIST on RISC OS.  Strip the prefix, and refuse
    absolute or parent-relative names while we are here."""
    while name.startswith("./"):
        name = name[2:]
    name = name.rstrip("/")
    if not name or name.startswith("/") or os.pardir in name.split("/"):
        return None
    return name


def do_extract(chan, directory):
    if not os.path.isdir(directory):
        try:
            os.makedirs(directory)
        except OSError:
            log("could not create %s: %s" % (directory, sys.exc_info()[1]))
            return
    tf = None
    try:
        tf = tarfile.open(fileobj=chan, mode="r|")
        count = 0
        for member in tf:
            name = safe_name(member.name)
            if name is None:
                continue
            member.name = name
            tf.extract(member, directory)
            count += 1
        log("extracted %d entries into %s" % (count, directory))
        # The marker anyvm waits for. A shell would have printed it from the
        # chained `&& echo`; there is no shell here, so say it explicitly --
        # and only now, after tarfile returned without raising.
        chan.write(TAR_DONE + "\r\n")
    except Exception:
        log("extract failed:\n%s" % (traceback.format_exc(),))
    finally:
        if tf is not None:
            try:
                tf.close()
            except Exception:
                pass


def do_create(chan, directory):
    """Stream the directory back as a ustar archive, IAC-doubled on the way
    out by chan.write."""
    tf = None
    try:
        tf = tarfile.open(fileobj=chan, mode="w|", format=tarfile.USTAR_FORMAT)
        names = sorted(os.listdir(directory)) if os.path.isdir(directory) else []
        if not names:
            log("nothing to send from %s" % (directory,))
        for name in names:
            tf.add(os.path.join(directory, name), arcname="./" + name)
        tf.close()
        tf = None
        log("sent %s" % (directory,))
    except Exception:
        log("create failed:\n%s" % (traceback.format_exc(),))
    finally:
        if tf is not None:
            try:
                tf.close()
            except Exception:
                pass


def run_command(chan, line):
    """Everything that is not a tar transfer goes to the command line
    interpreter.  RISC OS cannot pipe a child's output, so it is redirected
    to a scrap file and sent back once the command has finished."""
    scrap = "<Wimp$ScrapDir>.anyvmout"
    try:
        os.system("%s { > %s }" % (line, scrap))
    except Exception:
        log("command failed:\n%s" % (traceback.format_exc(),))
    try:
        fh = open(scrap, "rb")
    except IOError:
        return
    try:
        while True:
            data = fh.read(CHUNK)
            if not data:
                break
            chan.write(data)
    finally:
        try:
            fh.close()
        except Exception:
            pass
        try:
            os.remove(scrap)
        except OSError:
            pass


def serve(sock):
    """One connection, many commands.

    build.py's telnet_exec sends several command lines down a single socket
    and reads the output after each, so a one-command-per-connection agent
    would look like a guest that hangs up after the first probe."""
    chan = Telnet(sock)
    while not chan.eof:
        line = chan.read_line()
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        log("command: %s" % (line,))
        parsed = parse_tar_command(line)
        if parsed is None:
            run_command(chan, line)
            continue
        action, directory = parsed
        if action == "extract":
            do_extract(chan, directory)
        else:
            # The pull is the end of the session by construction: anyvm reads
            # until the socket closes, so there is nothing to come back to.
            do_create(chan, directory)
            break


def main():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except socket.error:
        pass
    listener.bind(("", PORT))
    listener.listen(BACKLOG)
    log("listening on port %d" % (PORT,))
    while True:
        # select() rather than a blocking accept() so the agent keeps
        # yielding: it runs inside a TaskWindow, where a SWI that never
        # returns would freeze the whole desktop.
        try:
            ready = select.select([listener], [], [], POLL)[0]
        except Exception:
            continue
        if not ready:
            continue
        try:
            sock, peer = listener.accept()
        except socket.error:
            log("accept failed: %s" % (sys.exc_info()[1],))
            continue
        log("connection from %s" % (peer,))
        try:
            serve(sock)
        except Exception:
            log("session failed:\n%s" % (traceback.format_exc(),))
        try:
            sock.close()
        except socket.error:
            pass


main()
