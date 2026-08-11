#!/usr/bin/env python3
# Print the current RISC OS release version, e.g. "5.30". Empty output means
# "nothing detected" and is not an error; a non-zero exit means detection
# itself is broken (network error, HTTP error, or a payload that no longer
# matches the expected shape) and must be reported by the caller, never
# swallowed. A failure must NEVER print a plausible-but-wrong version -- the
# version is only printed after every step below has succeeded.
#
# Source of truth: RISC OS Open's Raspberry Pi download page. There is no
# releases API to ask, and no tag list -- ROOL publish one archive per
# platform at a versioned path and replace it in place.
#
# The version is taken from the ARCHIVE FILENAME, not from prose on the page,
# and that distinction is the whole design. The page mentions 5.31 six times
# (the nightly "Development" ROM) alongside 5.30 nine times, so anything
# scraping version-shaped text would happily report 5.31 -- for which no
# RISCOSPi archive is published at all. Anchoring on RISCOSPi.<ver>.zip makes
# the detected version and the downloadable artifact the same fact.
#
# ROOL DELETE SUPERSEDED MEDIA. Verified: RISCOSPi.5.30.zip answers 200 while
# the identically shaped 5.28 URL answers 404. So a new release REPLACES this
# builder's row rather than adding one, which is why ALL_RELEASES_UPDATE is
# set to "replace" below -- the same thing plan9-builder needs, because
# 9front deletes old media too. Without it every superseded release becomes
# one permanently red build against a URL that has stopped existing.
#
# stdlib only (urllib.request, re, sys, os) -- no external dependencies.

import os
import re
import sys
import urllib.request

URL = "https://www.riscosopen.org/content/downloads/raspberry-pi"
TIMEOUT = 60
USER_AGENT = "anyvm-org-upstream-watcher/1.0"

# Matches the published archive, e.g. RISCOSPi.5.30.zip, and captures 5.30.
ARCHIVE_RE = re.compile(r"RISCOSPi\.(\d+(?:\.\d+)+)\.zip", re.I)

# A new release replaces the previous row instead of adding one -- see the
# header. base-builder/watch.py reads this from the hook's environment block.
ALL_RELEASES_UPDATE = "replace"


def resolve_natural_key():
    """Return the engine's own natural_key, or fail loudly.

    watch.yml clones base-builder INTO the builder repo root, so at detection
    time it sits at "base-builder/" (relative to this hook's cwd, the builder
    repo root). A local checkout instead has it as a sibling,
    "../base-builder". Try both, in that order.

    There is deliberately NO local fallback copy. Ordering must be the single
    rule the engine uses -- a per-hook duplicate would have to be kept in sync
    by hand across every builder and would drift silently, and a hook that
    ranks versions differently from watch.py is worse than one that refuses to
    run.
    """
    for candidate in ("base-builder", os.path.join("..", "base-builder")):
        if not os.path.isdir(candidate):
            continue
        path = os.path.abspath(candidate)
        if path not in sys.path:
            sys.path.insert(0, path)
        try:
            import gendata
            return gendata.natural_key
        except ImportError:
            continue
    raise ImportError(
        "base-builder/gendata.py not importable from %s; expected it at "
        "./base-builder (CI) or ../base-builder (local checkout)"
        % os.getcwd())


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def main():
    try:
        key = resolve_natural_key()
    except ImportError as e:
        sys.stderr.write("upstream_check: %s\n" % e)
        return 1
    try:
        body = fetch(URL)
    except Exception as e:
        sys.stderr.write("upstream_check: fetch of %s failed: %s\n" % (URL, e))
        return 1

    versions = sorted(set(m.group(1) for m in ARCHIVE_RE.finditer(body)),
                      key=key)
    if not versions:
        # Not "nothing new" -- the page has always carried exactly one such
        # link, so zero matches means it was restructured and this hook can no
        # longer tell. Fail rather than report nothing and look healthy.
        sys.stderr.write(
            "upstream_check: no RISCOSPi.<version>.zip link found on %s; "
            "the page shape has changed and detection is broken\n" % URL)
        return 1

    sys.stdout.write(versions[-1] + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
