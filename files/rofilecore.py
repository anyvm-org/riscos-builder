#!/usr/bin/env python3
"""Read a RISC OS FileCore volume out of a raw disc image, and patch a file
in place.

RISC OS on the Pi has no USB keyboard driver, so there is no console to type
an installer into (see conf/riscos-5.30-armv7.conf). The only way to get the
anyvm agent into the guest is to modify the disc before the first boot, and
the only modification that is safe without a FileCore writer is overwriting
an existing file's data IN PLACE at its existing byte length -- no allocation
map to update, no directory entry to resize.

FORMAT, derived from ROOL's 5.30 image and checked entry by entry against
the files it decodes.

A directory block is 512-byte aligned and starts 4 bytes BEFORE its "SBPr"
magic.  Relative to the magic at M:

    M+0   "SBPr"
    M+4   NameLen    M+8  Size    M+12 Entries   M+16 NamesSize   M+20 Parent
    M+24  the directory's own name, NameLen bytes + CR, padded to a word
          entries, 28 bytes each, at M+24+align4(NameLen+1)
          the name heap, immediately after the entries

    entry: load, exec, length, indDiscAdd, attr, nameLen, nameOffset
           (seven little-endian words; nameOffset is relative to the heap)

Only the LOW BYTE of attr is meaningful -- the upper three carry stale junk,
so never compare the whole word.  Bit 3 set means directory.  A file's type
is (load >> 8) & 0xFFF when (load >> 20) == 0xFFF.

indDiscAdd is (fragment << 8) | sector, with 512-byte sectors.  Fragment ids
are allocation ids, NOT positions, so there is no closed-form fragment ->
offset formula.  They are calibrated instead: a directory's own address is
the indDiscAdd of its entry in its parent, and every child block records its
parent's address, so walking down from the root pairs addresses with real
byte offsets.  That resolves every fragment that contains a directory; a
file in a fragment that holds only files stays unresolved, and for those
locate() falls back to searching the image for the file's known first bytes.
"""

import argparse
import collections
import mmap
import os
import struct
import sys

ROOT_GUESS_ENTRIES = ("!Boot", "Apps")
SECTOR = 512
ENTRY = 28


def align4(n):
    return (n + 3) & ~3


class Dir(object):
    __slots__ = ("mag", "block", "name", "parent", "ents")

    def __init__(self, mag, block, name, parent, ents):
        self.mag = mag
        self.block = block
        self.name = name
        self.parent = parent
        self.ents = ents


def _parse_at(m, mag):
    try:
        namelen, size, entries, namessize, parent = struct.unpack_from("<5I", m, mag + 4)
    except Exception:
        return None
    if not (0 < namelen < 64) or not (0 < entries < 4000):
        return None
    if not (0 < size <= (1 << 22)):
        return None
    dirname = m[mag + 24:mag + 24 + namelen]
    if not all(32 <= c < 127 for c in dirname):
        return None
    ebase = mag + 24 + align4(namelen + 1)
    hbase = ebase + ENTRY * entries
    if hbase + namessize > len(m):
        return None
    ents = []
    for i in range(entries):
        e = ebase + ENTRY * i
        load, exc, length, ind, attr, nlen, noff = struct.unpack_from("<7I", m, e)
        if nlen > 64 or noff > namessize + 64:
            return None
        nm = m[hbase + noff: hbase + noff + nlen]
        if not all(32 <= c < 127 for c in nm):
            return None
        ents.append(dict(name=nm.decode("latin-1"), load=load, length=length,
                         ind=ind, attr=attr & 0xFF, isdir=bool(attr & 8),
                         entry_off=e))
    return Dir(mag, mag - 4, dirname.decode("latin-1"), parent, ents)


def _map_size(fh):
    """Bytes to map for `fh`, whether it is a file or a block device.

    mmap(fd, 0) means "the whole file", but a block device reports st_size 0,
    so length 0 is rejected with EINVAL -- which is exactly what happens when
    this tool is pointed at the /dev/nbd0 that the prepareImage hook attaches
    the qcow2 to. Seeking to the end works for both: on a block device lseek
    reports the device size."""
    size = os.fstat(fh.fileno()).st_size
    if size:
        return size
    size = os.lseek(fh.fileno(), 0, os.SEEK_END)
    os.lseek(fh.fileno(), 0, os.SEEK_SET)
    if not size:
        raise SystemExit("cannot determine the size of %s" % fh.name)
    return size


class Volume(object):
    def __init__(self, path):
        self.path = path
        self.fh = open(path, "rb")
        self.m = mmap.mmap(self.fh.fileno(), _map_size(self.fh),
                           access=mmap.ACCESS_READ)
        self.dirs = []
        idx = 0
        while True:
            i = self.m.find(b"SBPr", idx)
            if i < 0:
                break
            d = _parse_at(self.m, i)
            if d is not None and (d.block % SECTOR) == 0:
                self.dirs.append(d)
            idx = i + 1
        self.by_parent = collections.defaultdict(list)
        for d in self.dirs:
            self.by_parent[d.parent].append(d)
        self.frag_base = {}
        self.paths = {}
        self._walk()

    def close(self):
        self.m.close()
        self.fh.close()

    def _find_root(self):
        """The root is its own parent and holds the usual top-level names."""
        best = None
        for d in self.dirs:
            if d.name != "$":
                continue
            names = set(e["name"] for e in d.ents)
            if not all(n in names for n in ROOT_GUESS_ENTRIES):
                continue
            if best is None or len(d.ents) > len(best.ents):
                best = d
        return best

    def _walk(self):
        root = self._find_root()
        if root is None:
            raise SystemExit("no FileCore root directory found in %s" % self.path)
        self._note(root.parent, root.block)
        stack = [(root, root.parent, "$")]
        seen = set()
        while stack:
            d, addr, path = stack.pop()
            if addr in seen:
                continue
            seen.add(addr)
            kids = {}
            for c in self.by_parent.get(addr, []):
                kids.setdefault(c.name, c)
            for e in d.ents:
                p = path + "." + e["name"]
                if e["isdir"]:
                    c = kids.get(e["name"])
                    if c is None:
                        continue
                    self._note(e["ind"], c.block)
                    self.paths[p] = e
                    stack.append((c, e["ind"], p))
                else:
                    self.paths[p] = e

    def _note(self, ind, off):
        self.frag_base.setdefault(ind >> 8, off - SECTOR * (ind & 0xFF))

    def offset_of(self, ent):
        base = self.frag_base.get(ent["ind"] >> 8)
        if base is None:
            return None
        return base + SECTOR * (ent["ind"] & 0xFF)

    def locate(self, path, expect_prefix=None):
        """Return (offset, length) for a file, or raise.

        Preference order: the calibrated fragment address, then -- for a file
        whose fragment holds no directory and so was never calibrated -- a
        search for expect_prefix at a sector boundary.  The search must be
        unambiguous; several copies of the same boot script exist on a booted
        image (one per ROxxxHook template plus the live Choices copy), and
        guessing between them is exactly how a patch silently lands on a file
        that never runs."""
        ent = self.paths.get(path)
        if ent is None:
            raise SystemExit("no such object: %s" % path)
        if ent["isdir"]:
            raise SystemExit("%s is a directory" % path)
        off = self.offset_of(ent)
        if off is not None:
            return off, ent["length"]
        if not expect_prefix:
            raise SystemExit(
                "%s is in an uncalibrated fragment (&%06X); pass its first "
                "bytes so it can be found by content"
                % (path, ent["ind"] >> 8))
        hits = []
        idx = 0
        while True:
            i = self.m.find(expect_prefix, idx)
            if i < 0:
                break
            if i % SECTOR == 0:
                hits.append(i)
            idx = i + 1
        if len(hits) != 1:
            raise SystemExit(
                "%s: expected exactly one sector-aligned copy of %r, found %d "
                "(%s)" % (path, expect_prefix[:40], len(hits), hits[:8]))
        return hits[0], ent["length"]

    def read(self, path, expect_prefix=None):
        off, length = self.locate(path, expect_prefix)
        return self.m[off:off + length]


def patch_in_place(image, off, length, body, expect_prefix):
    """Overwrite a file's data without changing its length.

    The tail is padded with an Obey comment (`|` then spaces) so whatever the
    original file had beyond our text can never be interpreted as a command."""
    with open(image, "rb") as fh:
        fh.seek(off)
        cur = fh.read(length)
    if expect_prefix and not cur.startswith(expect_prefix):
        raise SystemExit("refusing to patch at %d: expected %r, found %r"
                         % (off, expect_prefix[:40], cur[:40]))
    body = body.replace(b"\r\n", b"\n")
    if not body.endswith(b"\n"):
        body += b"\n"
    if len(body) > length:
        raise SystemExit("script is %d bytes but the slot is only %d"
                         % (len(body), length))
    pad = length - len(body)
    if pad == 1:
        body += b"\n"
    elif pad >= 2:
        body += b"|" + b" " * (pad - 2) + b"\n"
    assert len(body) == length
    with open(image, "r+b") as fh:
        fh.seek(off)
        fh.write(body)
    return pad


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("image")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ls", help="list a directory")
    p.add_argument("path", nargs="?", default="$")

    p = sub.add_parser("cat", help="print a file")
    p.add_argument("path")
    p.add_argument("--prefix", default=None)

    p = sub.add_parser("where", help="print a file's byte offset and length")
    p.add_argument("path")
    p.add_argument("--prefix", default=None)

    p = sub.add_parser("patch", help="overwrite a file in place")
    p.add_argument("path")
    p.add_argument("source")
    p.add_argument("--prefix", required=True,
                   help="bytes the file must start with, as a safety check")

    a = ap.parse_args()
    vol = Volume(a.image)
    prefix = getattr(a, "prefix", None)
    prefix = prefix.encode("latin-1") if prefix else None

    if a.cmd == "ls":
        base = a.path.rstrip(".")
        depth = base.count(".") + 1
        rows = [(p, e) for p, e in vol.paths.items()
                if p.startswith(base + ".") and p.count(".") == depth]
        if not rows and base not in ("$",):
            raise SystemExit("no such directory: %s" % base)
        for p, e in sorted(rows):
            ft = (e["load"] >> 8) & 0xFFF if (e["load"] >> 20) == 0xFFF else None
            off = vol.offset_of(e)
            print("%-4s %-40s len=%-9d off=%s"
                  % ("DIR" if e["isdir"] else "&%03X" % (ft or 0),
                     p.rsplit(".", 1)[-1], e["length"],
                     off if off is not None else "?"))
    elif a.cmd == "cat":
        sys.stdout.write(vol.read(a.path, prefix).decode("latin-1"))
    elif a.cmd == "where":
        off, length = vol.locate(a.path, prefix)
        print("%d %d" % (off, length))
    elif a.cmd == "patch":
        off, length = vol.locate(a.path, prefix)
        vol.close()
        with open(a.source, "rb") as fh:
            body = fh.read()
        pad = patch_in_place(a.image, off, length, body, prefix)
        print("patched %s at %d (%d byte slot, %d padding)"
              % (a.path, off, length, pad))
        return
    vol.close()


if __name__ == "__main__":
    main()
