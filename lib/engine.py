#!/usr/bin/env python3
"""sessync engine: syncs ~/.claude/projects and the prompt history with the storage (rclone crypt).

Three kinds of files:
  - transcripts (*.jsonl) are only appended to, and travel as a log (log shipping, like
    Litestream): only the lines appended since last time;
  - everything else (session attachments, conflict copies) is written once — whole;
  - symlinks (subagents of forked sessions) — an object <name>.rclonelink holding the link target,
    as with rclone --links.

A transcript in the storage is a base (a full copy at some point) plus "tails": immutable objects
with the bytes appended after it. A tail is named
    <start:016d>-<end:016d>-<level>-<signature:12>-<Mac>
the signature is the start of the sha256 of the file's last 4 KB before the tail's end. The file is
the base plus the chain of tails from its end. If one point has two chains (appended on two Macs at
once), every Mac picks the same: the longest, then the one with fewer tails, then by name.

Only whole lines are sent; only new tails or a slice of the base by offset are received (crypt can
be read from any position). Compaction is done by the Mac that appended last:
  - tails grew as big as the base — the file goes whole as a new base, the tails are deleted
    (each base is at least twice the previous one — two file sizes in total at most);
  - the session went quiet (unchanged for QUIET seconds) or MAX_L0 level-0 tails piled up —
    they are folded into one level-1 tail (only their volume travels, not the whole session).
So an abandoned session is folded once and costs nothing after that.

Changes on both sides since the last sync are a conflict. A session (<project>/<uuid>.jsonl) went
two ways: the common start stays, and of the two continuations one keeps the name — the one with
the conversation, then the one already decided on earlier, then the longer one — while the other
becomes a separate session, the way Claude Code's own /branch makes one: the same lines with the
new session id, and a "<title> (Branch)" title. Its id comes from the session id and the first line
where the versions part, so every Mac that meets the same split makes the same branch, and a Mac
whose branch is already a separate session gives way and appends to it. A continuation of only
Claude Code's service lines (a session opened and closed, /exit) is not worth a branch and goes.
Other files keep the newer version under the name (the bigger one when the times are the same) and the
other next to it as <file>.conflictN: they are rewritten whole, so the newer one has the latest state.

The file of a session open in claude on this Mac (Claude Code's registry --sessions, sessync's
own hooks --own-open, --open) is not changed: Claude Code
has it in memory and would go on from what it read. What came from another Mac waits until the
session closes (--held-out names such sessions); this Mac's own lines go out as usual.

What is deleted on this Mac is first copied to the trash (inside the storage), then deleted. More
than --max-delete percent of deletions stop the sync until the user decides (like bisync
--max-delete, with the same log line).

sessync forget: a session asked for here (--forget DIR/<id>) gets a mark in the storage
(--tombs REMOTE/<id>) once it is closed, and then on every Mac its files are deleted here, and its
objects in the storage go to the trash — sessync restore brings it back until the trash is cleaned;
it is never sent or received again while the mark is there. Asked for with "now" in DIR/<id>: the
one deletion without the trash — the objects go at once, and its copies in the trash too. While it
is open on a Mac, nothing of it changes there.

Prompt history (--own KEY, this Mac's file): each Mac writes only its own file, so the others' are
only received, and their storage version replaces the copy here — also a shorter one, when sessync
forget took lines out of it on its Mac. This Mac's own file, rewritten here, goes whole.

--map=REMOTE:LOCAL: on a Mac with another user name, project folders (the first path component,
named after the absolute path by Claude Code) under the storage's home prefix REMOTE are LOCAL here.

  engine.py sync TREE LOCAL_DIR BASES TAILS STATE --host MAC --work DIR [options]
  engine.py ahead BASES TAILS HISTORY FILE STATE --key=KEY --host MAC
                                      claude -r: has another Mac written lines of this session?
  engine.py fetch BASE TAILS FILE     assemble a transcript from a base and tails (trash restore)
  engine.py opened SESSION            exit code 0 — the session is open in claude here
  engine.py describe ID --projects DIR       sessync forget: which session that is
  engine.py forget-local SESSION --config DIR …   sessync forget: everything of it on this Mac
  engine.py put-prompts FILE… --config DIR …      sessync restore: its prompts back

sync prints "sent received bytes-sent bytes-received"; exit code 3 — stopped because of deletions,
1 — error. Log messages are in English, or Russian with SESSYNC_LANG=ru.
"""
import argparse
import errno
import filecmp
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone

W = 4096                                             # signature window: the last 4 KB before a point
QUIET = int(os.environ.get("SESSYNC_QUIET", 3600))   # a session "went quiet" — may be folded
MAX_L0 = int(os.environ.get("SESSYNC_MAX_L0", 48))   # more level-0 tails — fold right away
STALE = 600                                          # an unfinished line older than this is cut off
RU = os.environ.get("SESSYNC_LANG") == "ru"
LINK = ".rclonelink"
SEG_RE = re.compile(r"^(\d{16})-(\d{16})-([01])-([0-9a-f]{12})-(.+)$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
FORK_NS = uuid.UUID("5e55c0de-b4a7-4c1e-9d2f-0f0f5e55c0de")   # branch ids: uuid5 in this namespace
SERVICE = ("<command-name>", "<command-message>", "<command-args>", "<local-command-")
LOG = None


class Fail(Exception):
    pass


def tx(en, ru):
    return ru if RU else en


def log(msg):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y/%m/%d %H:%M:%S") + " engine: " + msg + "\n")


def rclone(*args, out=None):
    cmd = ["rclone", *args]
    if LOG:
        cmd += ["--log-file", LOG, "--log-level", "NOTICE"]
    p = subprocess.run(cmd, stdout=out if out is not None else subprocess.DEVNULL)
    fail = os.environ.get("SESSYNC_TEST_FAIL_COPY") if os.environ.get("SESSYNC_TEST_REMOTE") else None
    if fail and args[0] == "copy" and os.path.exists(fail):   # tests: an upload that broke off after the files went
        os.remove(fail)
        p.returncode = 1
    if p.returncode != 0:
        raise Fail(f"rclone {' '.join(args[:2])}: " + tx("code", "код") + f" {p.returncode}")


def listing(remote):
    p = subprocess.run(["rclone", "lsjson", "-R", "--files-only", "--fast-list", "--use-server-modtime",
                        "--no-mimetype", remote], capture_output=True)
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace")
        if "directory not found" in err:
            return []
        raise Fail(tx("no file list of ", "нет списка файлов ") + f"{remote}: " + (err.strip().splitlines() or ["?"])[-1])
    return json.loads(p.stdout or b"[]")


def cat(path, offset, count, dst):
    with open(dst, "wb") as f:
        rclone("cat", "--offset", str(offset), "--count", str(count), path, out=f)
    if os.path.getsize(dst) != count:
        raise Fail(tx("read a different size than needed: ", "прочитано не столько, сколько нужно: ") + path)


def kb(n):
    return f"{(n + 1023) // 1024} " + tx("KB", "КБ")


def sha(b):
    return hashlib.sha256(b).hexdigest()


def file_sig(path, end):
    """Signature of point end: sha256 of the last W bytes before it."""
    a = max(0, end - W)
    with open(path, "rb") as f:
        f.seek(a)
        data = f.read(end - a)
    return sha(data) if len(data) == end - a else None


def complete_end(path, size, floor):
    """End of the last whole line after floor (or floor): an unfinished line is not sent."""
    pos = size
    with open(path, "rb") as f:
        while pos > floor:
            a = max(floor, pos - 65536)
            f.seek(a)
            i = f.read(pos - a).rfind(b"\n")
            if i >= 0:
                return a + i + 1
            pos = a
    return floor


def mtime_of(s):
    """ModTime from rclone lsjson (nanoseconds, offset) → epoch; python 3.9 can't parse it itself."""
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?", s or "")
    if not m:
        return None
    tz = timezone.utc
    if m[8] and m[8] != "Z":
        sign = 1 if m[8][0] == "+" else -1
        tz = timezone(sign * timedelta(hours=int(m[8][1:3]), minutes=int(m[8][4:6])))
    us = int((m[7] or "0")[:6].ljust(6, "0"))
    return datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6]), us, tz).timestamp()


def clone_prefix(src, dst, n):
    """The first n bytes of src into dst: on APFS an instant clone, no copying and no extra space."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if subprocess.run(["cp", "-c", src, dst], capture_output=True).returncode != 0:
        shutil.copyfile(src, dst)
    if os.path.getsize(dst) < n:
        raise Fail(tx("the file got shorter during the sync: ", "файл стал короче во время синхронизации: ") + src)
    os.truncate(dst, n)


def same_whole(path, l):
    """The file is still the version the plan saw (size and time) — a whole file is sent only as that version."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return (st.st_size, st.st_mtime_ns) == (l.size, l.mtime)


def copy_range(src, dst, a, b):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, "rb") as f, open(dst, "wb") as o:
        f.seek(a)
        n = b - a
        while n > 0:
            chunk = f.read(min(n, 1 << 20))
            if not chunk:
                raise Fail(tx("the file got shorter during the sync: ", "файл стал короче во время синхронизации: ") + src)
            o.write(chunk)
            n -= len(chunk)


def append_file(dst, src):
    with open(dst, "ab") as o, open(src, "rb") as f:
        shutil.copyfileobj(f, o, 1 << 20)


def place_new(src, dst):
    """src under the name dst, only if dst doesn't exist yet (FileExistsError — it does)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        tmp = dst + ".sessync.tmp"
        shutil.copyfile(src, tmp)
        try:
            os.link(tmp, dst)
        finally:
            os.unlink(tmp)


def ignored(name):
    """Not synced: macOS service files and sessync temporary files."""
    return name == ".DS_Store" or name.endswith(".sessync.tmp")


def unsafe(key):
    """A path from the storage that would lead out of the folder: absolute, or with empty, "." or
    ".." parts. Only a Mac with the password can write such names; they are skipped all the same."""
    return key.startswith("/") or any(p in ("", ".", "..") for p in key.split("/"))


MTIME_SLACK = 2  # seconds: storage times can be rounded, and two Macs' clocks are not exactly the same


def kind_of(key):
    return "link" if key.endswith(LINK) else "log" if key.endswith(".jsonl") else "whole"


def newer_here(l, rf):
    """A file changed on both Macs keeps the newer version under its name: such files (subagent .meta.json,
    tool results) are rewritten whole, so the newer one has the latest state, whatever its size. Times
    closer than MTIME_SLACK (or unknown) — the bigger one, as before."""
    there = mtime_of(rf.base[1])
    here = l.mtime / 1e9
    if there is not None and abs(here - there) > MTIME_SLACK:
        return here > there
    return l.size > rf.base[0]


def session_of(rel):
    parts = rel.split("/")
    if len(parts) == 2 and parts[1].endswith(".jsonl"):
        return parts[1][:-6]
    return parts[1] if len(parts) > 2 else None


def sid_of(key):
    """The session a file belongs to, for sessync forget: <project>/<id>.jsonl, its conflict copies
    and its link, everything in <project>/<id>/."""
    parts = key.split("/")
    name = parts[1].split(".", 1)[0] if len(parts) > 1 else ""
    return name if UUID_RE.match(name) else None


def open_sessions(d):
    """Ids of sessions open in claude on this Mac now (Claude Code's registry ~/.claude/sessions/<pid>.json).
    The format is internal: if it can't be read, no session counts as open."""
    ids = set()
    try:
        names = os.listdir(d) if d else []
    except OSError:
        return ids
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                j = json.load(f)
            pid, sid = int(j["pid"]), j["sessionId"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        try:
            os.kill(pid, 0)
        except PermissionError:
            pass
        except OSError:
            continue
        if isinstance(sid, str):
            ids.add(sid)
    return ids


def own_open(d):
    """Sessions sessync's own hooks saw start (<dir>/<session id> holding the pid of its claude):
    from the hook's input, a documented interface, so this works even if Claude Code's registry
    changes. An entry whose claude has exited is gone."""
    ids = set()
    try:
        names = os.listdir(d) if d else []
    except OSError:
        return ids
    for n in names:
        p = os.path.join(d, n)
        try:
            with open(p, encoding="utf-8") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
        except PermissionError:
            pass
        except (OSError, ValueError):
            try:
                os.unlink(p)
            except OSError:
                pass
            continue
        ids.add(n)
    return ids


def fork_ok(key):
    """A session's own file, <project>/<uuid>.jsonl: its other continuation can be a session of its own."""
    parts = key.split("/")
    return len(parts) == 2 and parts[1].endswith(".jsonl") and bool(UUID_RE.match(parts[1][:-6]))


def fork_id(sid, line):
    """The id of the branch that starts with this line: the same on every Mac."""
    return str(uuid.uuid5(FORK_NS, sid + ":" + sha(line)))


def iter_lines(parts, a, b):
    """The lines of bytes [a, b) of a file made of parts [(path, start, end), …] put one after another.
    The parts, a and b end lines."""
    pos = 0
    for path, s, e in parts:
        lo, hi = max(a, pos), min(b, pos + e - s)
        if lo < hi:
            with open(path, "rb") as f:
                f.seek(s + lo - pos)
                left = hi - lo
                while left > 0:
                    line = f.readline(left)
                    if not line:
                        raise Fail(tx("the file got shorter during the sync: ", "файл стал короче во время синхронизации: ") + path)
                    left -= len(line)
                    yield line
        pos += e - s


def common_lines(f1, a1, n1, f2, a2, n2):
    """How many bytes, in whole lines, two ranges of files share from their start."""
    done, last, n = 0, 0, min(n1, n2)
    with open(f1, "rb") as x, open(f2, "rb") as y:
        x.seek(a1)
        y.seek(a2)
        while done < n:
            k = min(1 << 20, n - done)
            bx, by = x.read(k), y.read(k)
            if bx != by:
                lo, hi = 0, min(len(bx), len(by))
                while lo < hi:  # the first byte that differs
                    mid = (lo + hi) // 2
                    if bx[:mid + 1] == by[:mid + 1]:
                        lo = mid + 1
                    else:
                        hi = mid
                i = bx.rfind(b"\n", 0, lo)
                return done + i + 1 if i >= 0 else last
            i = bx.rfind(b"\n")
            if i >= 0:
                last = done + i + 1
            done += k
    return last


def conversation(line):
    """True — a line of the conversation itself; False — Claude Code's bookkeeping (titles, modes,
    /exit and other commands); None — not a JSON line."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    t = d.get("type")
    if t == "assistant":
        return True
    if t != "user" or d.get("isMeta") or not isinstance(d.get("message"), dict):
        return False
    c = d["message"].get("content")
    if isinstance(c, str):
        return not c.lstrip().startswith(SERVICE)
    return bool(c)


def talk(parts, a, b):
    """Bytes [a, b): True — hold conversation, False — only bookkeeping, None — damaged."""
    found = False
    for line in iter_lines(parts, a, b):
        k = conversation(line)
        if k is None:
            return None
        found = found or k
    return found


def clean(s):
    """Without control and format characters (ESC, newlines, bidi): the text reaches a terminal."""
    return "".join(ch for ch in s if unicodedata.category(ch) not in ("Cc", "Cf"))


def title_of(parts, end, sid):
    """The session's title, for its branch: the name given to it, else Claude Code's title, else the
    first prompt. Only from the lines, so every Mac gets the same."""
    custom = ai = first = None
    for line in iter_lines(parts, 0, end):
        if b'"custom-title"' not in line and b'"ai-title"' not in line \
                and (first is not None or b'"type":"user"' not in line):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("sessionId", sid) != sid:
            continue
        t = d.get("type")
        if t == "custom-title" and isinstance(d.get("customTitle"), str):
            custom = d["customTitle"]
        elif t == "ai-title" and isinstance(d.get("aiTitle"), str):
            ai = d["aiTitle"]
        elif t == "user" and first is None and conversation(line):
            c = d["message"]["content"]
            if isinstance(c, list):
                c = next((x.get("text") for x in c if isinstance(x, dict) and x.get("type") == "text"), None)
            first = c if isinstance(c, str) else None
    return clean(" ".join((custom or ai or first or "").split()))[:80].strip() or "Session"


class Seg:
    __slots__ = ("name", "off", "end", "lvl", "h", "host", "mtime")

    def __init__(self, name, m, mtime):
        self.name, self.off, self.end = name, int(m[1]), int(m[2])
        self.lvl, self.h, self.host, self.mtime = int(m[3]), m[4], m[5], mtime


def parse_seg(name, size, mtime):
    m = SEG_RE.match(name)
    if not m or int(m[2]) - int(m[1]) != size or size <= 0:
        return None  # foreign or still being written — not part of a chain
    return Seg(name, m, mtime)


def build_chain(B, segs):
    """The chain of tails from the end of base B: the longest, then the one with fewer tails, then by
    names. The same on every Mac. Returns (tails in order, end of the file)."""
    at = {}
    for s in segs:
        at.setdefault(s.off, []).append(s)
    best = {}
    for pos in sorted({B} | {s.off for s in segs} | {s.end for s in segs}, reverse=True):
        cur = (pos, ())
        for s in at.get(pos, ()):
            e, ch = best[s.end]
            cand = (e, (s,) + ch)
            if (cand[0], -len(cand[1])) > (cur[0], -len(cur[1])) or (
                    (cand[0], len(cand[1])) == (cur[0], len(cur[1]))
                    and [x.name for x in cand[1]] < [x.name for x in cur[1]]):
                cur = cand
        best[pos] = cur
    end, ch = best[B]
    return list(ch), end


class RFile:
    """A file in the storage: the base or the whole file (size, time) and, for a transcript, its tails."""

    def __init__(self):
        self.base = None
        self.segs = []
        self.chain = []
        self.L = 0

    def finish(self):
        if self.base is not None:
            self.chain, self.L = build_chain(self.base[0], self.segs)

    def boundary(self, p):
        return p == self.base[0] or any(s.end == p for s in self.chain)


class Local:
    __slots__ = ("kind", "path", "size", "mtime", "target")

    def __init__(self, kind, path, size, mtime, target=None):
        self.kind, self.path, self.size, self.mtime, self.target = kind, path, size, mtime, target


class Engine:
    def __init__(self, a):
        self.a = a
        self.root, self.rbase, self.rseg = a.local, a.base, a.seg
        self.rroot = os.path.realpath(self.root)
        self.host = re.sub(r"[^A-Za-z0-9.-]", "-", a.host)
        self.work = a.work
        try:
            with open(a.state, encoding="utf-8") as f:
                self.state = json.load(f)
        except FileNotFoundError:
            self.state = {}
        self.files = self.state.setdefault("files", {})
        self.pending = self.state.setdefault("pending", [])
        self.mr, self.ml = a.map.split(":", 1) if a.map else (None, None)
        self.up = self.down = self.bup = self.bdown = 0
        self.nparts = 0
        self.gets, self.puts, self.conflicts = [], [], []
        self.del_remote, self.del_local, self.insync = [], [], set()
        self.rem, self.opened = {}, set()
        self.held, self.forks = {}, []   # open sessions waiting for another Mac's lines; new branches
        self.tombs, self.forgot, self.forget_here, self.forget_there = set(), [], [], []   # sessync forget
        self.now = set()

    def save(self):
        tmp = self.a.state + ".sessync.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, self.a.state)

    @staticmethod
    def swap(key, a, b):
        """Project folder under home prefix a (the first path component) → under b."""
        first, sep, rest = key.partition("/")
        if a and (first == a or first.startswith(a + "-")):
            return b + first[len(a):] + sep + rest
        return key

    def path_of(self, key):
        key = self.swap(key, self.mr, self.ml)
        return os.path.join(self.root, key[:-len(LINK)] if key.endswith(LINK) else key)

    def inside(self, path):
        """path is really under the folder: not through a link to a folder elsewhere (links come from
        the storage as they are, and a file "under" one would be written wherever it points)."""
        d = os.path.dirname(path)
        while not os.path.lexists(d):
            d = os.path.dirname(d)
        d = os.path.realpath(d)
        return d == self.rroot or d.startswith(self.rroot + os.sep)

    def writable(self, key, path, link=False):
        """Checked right before writing, not when planning: a link received earlier in this same run
        may already stand in the path. The file itself must not be a link either — it would be
        written through."""
        if not self.inside(path):
            log(f"{key}: " + tx("its folder here is a link out of the projects folder — not written",
                               "его папка здесь — ссылка за пределы папки проектов — не записываю"))
            return False
        if not link and os.path.islink(path):
            log(f"{key}: " + tx("a link here instead of the file — left alone", "на месте файла здесь ссылка — не трогаю"))
            return False
        return True

    # ---- file lists ---------------------------------------------------------------------------
    @staticmethod
    def drop_leftover(p):
        """A temporary file of an interrupted run. A fresh one stays: sessync restore may be writing it."""
        try:
            if time.time() - os.lstat(p).st_mtime > 3600:
                os.unlink(p)
        except OSError:
            pass

    def local_files(self):
        res = {}
        for dp, dns, fns in os.walk(self.root):
            rd = os.path.relpath(dp, self.root)
            pre = "" if rd == "." else rd + "/"
            if self.a.skip_memory and rd != "." and "/" not in rd and "memory" in dns:
                dns.remove("memory")  # each Mac has its own project memory
            for fn in dns + fns:
                if ignored(fn):
                    if fn.endswith(".sessync.tmp"):
                        self.drop_leftover(os.path.join(dp, fn))
                    continue
                p = os.path.join(dp, fn)
                try:
                    st = os.lstat(p)
                except FileNotFoundError:
                    continue
                key = pre + fn
                if self.mr and self.swap(key, self.ml, "") == key and self.swap(key, self.mr, "") != key:
                    # here, a folder under the storage's home would clash with this Mac's renamed ones
                    if rd == ".":
                        log(f"{key}: " + tx("named like the storage's home folder — not synced on this Mac",
                                           "назван как домашняя папка хранилища — на этом Mac не синхронизируется"))
                        if fn in dns:
                            dns.remove(fn)
                    continue
                key = self.swap(key, self.ml, self.mr)
                if stat.S_ISLNK(st.st_mode):
                    t = os.readlink(p)
                    res[key + LINK] = Local("link", p, len(os.fsencode(t)), st.st_mtime_ns, t)
                elif stat.S_ISREG(st.st_mode) and not fn.endswith(LINK):
                    res[key] = Local(kind_of(fn), p, st.st_size, st.st_mtime_ns)
        return res

    def remote(self):
        rem = {}
        for it in listing(self.rbase):
            p = it["Path"]
            if unsafe(p):
                log(f"{p}: " + tx("a path out of the folder in the storage — skipped", "путь за пределы папки в хранилище — пропущен"))
                continue
            parts = p.split("/")
            if ignored(parts[-1]) or (self.a.skip_memory and len(parts) > 2 and parts[1] == "memory"):
                continue
            rem.setdefault(p, RFile()).base = (it["Size"], it.get("ModTime", ""))
        for it in listing(self.rseg):
            rel, _, name = it["Path"].rpartition("/")
            s = parse_seg(name, it["Size"], it.get("ModTime", "")) if rel and not unsafe(rel) else None
            if s:
                rem.setdefault(rel, RFile()).segs.append(s)
        for rf in rem.values():
            rf.finish()
        return rem

    def pieces(self, key, rf, a, b):
        """Where bytes [a, b) of the file are: (path, offset, count, whole object, key for the batch)."""
        out, B = [], rf.base[0]
        if a < B:
            out.append((f"{self.rbase}/{key}", a, min(b, B) - a, a == 0 and b >= B, ("b", key)))
        for s in rf.chain:
            lo, hi = max(a, s.off), min(b, s.end)
            if lo < hi:
                out.append((f"{self.rseg}/{key}/{s.name}", lo - s.off, hi - lo,
                            lo == s.off and hi == s.end, ("s", f"{key}/{s.name}")))
        if sum(p[2] for p in out) != b - a:
            raise Fail(tx("part of the file is missing in the storage: ", "в хранилище нет части файла ") + key)
        return out

    def read_range(self, key, rf, a, b):
        data = b""
        for path, off, cnt, _, _ in self.pieces(key, rf, a, b):
            self.nparts += 1
            dst = os.path.join(self.work, "verify", str(self.nparts))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            cat(path, off, cnt, dst)
            with open(dst, "rb") as f:
                data += f.read()
        return data

    def flush_pending(self):
        """Deletions decided earlier (folded tails, deleted files): bases first, then tails.
        They are saved in the state before deleting — an interrupted run finishes them next time."""
        for kind, root in (("b", self.rbase), ("s", self.rseg)):
            names = [p for k, p in self.pending if k == kind]
            if not names:
                continue
            lst = os.path.join(self.work, f"delete-{kind}.txt")
            with open(lst, "w", encoding="utf-8") as f:
                f.write("\n".join(names) + "\n")
            rclone("delete", root, "--files-from-raw", lst, "--fast-list")
            self.pending[:] = [x for x in self.pending if x[0] != kind]
            self.save()

    # ---- transcripts: checks ------------------------------------------------------------------
    def local_valid(self, path, st, l):
        """The local file is the same one, only appended to (not rewritten or replaced)."""
        if l.size == st.get("size") and l.mtime == st.get("mtime"):
            return True
        return l.size >= st["s"] and file_sig(path, st["s"]) == st["sig"]

    def remote_valid(self, key, st, rf):
        """The first s bytes in the storage are the same as at the last sync."""
        s, B = st["s"], rf.base[0]
        if s == B and st.get("base") in ("self", [B, rf.base[1]]):
            st["base"] = [B, rf.base[1]]
            return True
        if s == 0:
            return True
        if any(x.end == s and x.h == st["sig"][:12] for x in rf.chain):
            return True
        if rf.L < s:
            return False
        # tails were folded or the base replaced — compare the 4 KB before the point (a ranged read)
        if sha(self.read_range(key, rf, max(0, s - W), s)) != st["sig"]:
            return False
        if s == B:
            st["base"] = [B, rf.base[1]]  # a verified base is not read again
        return True

    def same_at(self, key, path, rf, c):
        """The local file and the storage have the same 4 KB before c — so they share everything up to c."""
        lsig = file_sig(path, c)
        for x in rf.chain:
            if x.end == c:
                return lsig is not None and x.h == lsig[:12]
        return lsig is not None and sha(self.read_range(key, rf, max(0, c - W), c)) == lsig

    @staticmethod
    def logstate(s, sig, size, mtime, base):
        return {"s": s, "sig": sig, "size": size, "mtime": mtime, "base": base}

    def chain_drop(self, key, rf, after=None):
        """Tails no longer needed. Under a new base — all of them, off-chain ones too: the file only
        grows, so nothing will attach to a start that is inside the base now. After a conflict —
        the chain from the point where the versions part."""
        if after is None:
            return [["s", f"{key}/{s.name}"] for s in rf.segs]
        return [["s", f"{key}/{s.name}"] for s in rf.chain if s.off >= after]

    @staticmethod
    def l0_run(rf):
        run = []
        for s in reversed(rf.chain):
            if s.lvl != 0:
                break
            run.insert(0, s)
        return run

    def tail_put(self, key, path, rf, s, Ll, l):
        """What to send when [s, Ll) was appended here and the storage didn't change."""
        B = rf.base[0]
        if Ll - B >= B:
            return (key, path, ("base", 0, Ll), l, self.chain_drop(key, rf))
        run = self.l0_run(rf)
        if len(run) + 1 >= MAX_L0:
            return (key, path, ("seg", run[0].off, Ll, 1), l, [["s", f"{key}/{x.name}"] for x in run])
        return (key, path, ("seg", s, Ll, 0), l, [])

    def maybe_fold(self, key, path, l, rf, s):
        """The session went quiet: level-0 tails become one level-1 tail (or a new base)."""
        if not rf.chain or rf.chain[-1].host != self.host or time.time() - l.mtime / 1e9 < QUIET:
            return
        run = self.l0_run(rf)
        if len(run) < 2:
            return
        B = rf.base[0]
        if s - B >= B:
            self.puts.append((key, path, ("base", 0, s), None, self.chain_drop(key, rf)))
        else:
            self.puts.append((key, path, ("seg", run[0].off, s, 1), None,
                              [["s", f"{key}/{x.name}"] for x in run]))

    def get(self, key, rf, a, how, extra=None):
        self.gets.append((key, rf, a, how, extra))

    def incoming(self, key, rf, a):
        """Lines from another Mac for this file — unless its session is open here."""
        if session_of(key) in self.opened:
            self.hold(key, rf, a)
        else:
            self.get(key, rf, a, "append")

    def hold(self, key, rf, have):
        """The session is open in claude here: Claude Code has it in memory and would go on from what
        it read, so its file isn't changed now; what came from another Mac is applied after it closes."""
        sid = session_of(key)
        if sid not in self.held:
            hosts = [s.host for s in (rf.chain if rf else []) if s.end > have and s.host != self.host]
            self.held[sid] = re.sub(r"-[0-9A-Fa-f]{8}$", "", hosts[-1]) if hosts else ""
            log(f"{key}: " + tx("open here — what came from another Mac waits until the session closes",
                               "открыта здесь — пришедшее с другого Mac применю, когда сессия закроется"))
        return False

    def known(self, key, fid):
        """A branch with this id exists already — in the storage or here."""
        fkey = f"{key.split('/')[0]}/{fid}.jsonl"
        return fkey in self.rem or os.path.lexists(self.path_of(fkey))

    # ---- decisions per file -------------------------------------------------------------------
    def plan_log(self, key, st, l, rf, opened):
        if self.a.own and key != self.a.own:
            self.plan_theirs(key, st, l, rf)
            return
        mode, deletes = self.a.mode, not self.a.no_deletes
        path = self.path_of(key)
        rexists = rf is not None and rf.base is not None
        if l is None:
            if st is None or mode == "resync" or not deletes:
                self.get(key, rf, 0, "new")
            elif self.remote_valid(key, st, rf) and rf.L == st["s"]:
                self.del_remote.append(key)
            else:
                log(f"{key}: " + tx("deleted here, but appended on another Mac — bringing it back", "удалён здесь, но на другом Mac дописан — возвращаю"))
                self.get(key, rf, 0, "new")
            return
        if not rexists:
            if st is not None and mode != "resync" and deletes and self.local_valid(path, st, l) \
                    and complete_end(path, l.size, st["s"]) == st["s"]:
                self.delete_here(key, opened)
                return
            Ll = complete_end(path, l.size, 0)
            if Ll > 0:
                # tails without a base — leftovers of an interrupted deletion: not for the new base
                orphans = [["s", f"{key}/{x.name}"] for x in rf.segs] if rf else []
                self.puts.append((key, path, ("base", 0, Ll), l, orphans))
            return
        if st is None:  # new here, or the state was lost: the common part is where the last 4 KB match
            c = min(l.size, rf.L)
            if c == 0 or self.same_at(key, path, rf, c):
                st = self.logstate(c, file_sig(path, c), -1, -1,
                                   [rf.base[0], rf.base[1]] if c == rf.base[0] else None)
            else:
                self.conflicts.append((key, path, l, rf, 0))
                return
            self.files[key] = st
        s = st["s"]
        lv = self.local_valid(path, st, l)
        rv = self.remote_valid(key, st, rf)
        if lv and rv:
            quick = l.size == st.get("size") and l.mtime == st.get("mtime")
            Ll = s if quick or l.size <= s else complete_end(path, l.size, s)
            dl, dr = Ll - s, rf.L - s
            if dl == 0 and dr == 0:
                if not quick and l.size == s:  # verified by signature — size and time will do from now on
                    st.update(size=l.size, mtime=l.mtime)
                self.insync.add(key)
                self.maybe_fold(key, path, l, rf, s)
            elif dr == 0:
                self.puts.append(self.tail_put(key, path, rf, s, Ll, l))
            elif dl == 0:
                if l.size == s:
                    self.incoming(key, rf, s)
                elif time.time() - l.mtime / 1e9 < STALE:
                    log(f"{key}: " + tx("a line is being written right now — will receive next time", "строка дописывается прямо сейчас — получу в следующий раз"))
                else:  # a cut-off line has been hanging for long — a change here too: conflict
                    self.conflicts.append((key, path, l, rf, s))
            else:
                c = min(Ll, rf.L)
                if self.same_at(key, path, rf, c):
                    # our own, sent in an interrupted run (or the same text) — not a conflict
                    st.update(s=c, sig=file_sig(path, c), size=-1, mtime=-1)
                    if Ll > c:
                        self.puts.append(self.tail_put(key, path, rf, c, Ll, l))
                    elif rf.L > c and l.size == c:
                        self.incoming(key, rf, c)
                else:
                    self.conflicts.append((key, path, l, rf, s))
        elif not lv and rv and rf.L == s and (complete_end(path, l.size, 0) > rf.L or key == self.a.own):
            # rewritten here and longer now, unchanged in the storage — send it whole.
            # Shorter is damage rather than an edit: a conflict, and the longer version stays the main one.
            # This Mac's own prompt history is the exception: only this Mac writes it, and sessync forget
            # takes lines out of it
            log(f"{key}: " + tx("rewritten on this Mac — sending it whole", "файл переписан на этом Mac — отправляю целиком"))
            self.puts.append((key, path, ("base", 0, complete_end(path, l.size, 0)), l, self.chain_drop(key, rf)))
        else:
            self.conflicts.append((key, path, l, rf, 0))

    def plan_theirs(self, key, st, l, rf):
        """Another Mac's prompt history: only that Mac writes it, so here it is only received, and the
        storage's version is the right one — also when it got shorter (sessync forget took lines out)."""
        if rf is None or rf.base is None:
            return  # nothing to receive; a copy here stays as it is
        if l is None:
            self.get(key, rf, 0, "new")
            return
        path = self.path_of(key)
        if st is None:  # the copy here is the start of the storage's file — or not, then it is replaced
            c = l.size
            if c <= rf.L and (c == 0 or self.same_at(key, path, rf, c)):
                st = self.files[key] = self.logstate(c, file_sig(path, c), -1, -1,
                                                     [rf.base[0], rf.base[1]] if c == rf.base[0] else None)
            else:
                self.get(key, rf, 0, "replace")
                return
        s = st["s"]
        if l.size == s and self.local_valid(path, st, l) and self.remote_valid(key, st, rf):
            if rf.L == s:
                st.update(size=l.size, mtime=l.mtime)
                self.insync.add(key)
            else:
                self.get(key, rf, s, "append")
        else:
            self.get(key, rf, 0, "replace")

    def plan_whole(self, key, kind, st, l, rf, opened):
        """Attachments and links: change only whole — like bisync with --compare size."""
        mode, deletes = self.a.mode, not self.a.no_deletes
        rexists = rf is not None and rf.base is not None

        def local_same():
            return l.target == st.get("target") if kind == "link" else (l.size, l.mtime) == (st.get("size"), st.get("mtime"))

        def remote_same():
            if rf.base[0] != st.get("rsize"):
                return False
            if st.get("rmtime") is None:  # sent by us, the storage's time not seen yet
                st["rmtime"] = rf.base[1]
            return st["rmtime"] == rf.base[1]

        if l is None:
            if st is None or mode == "resync" or not deletes:
                self.get(key, rf, 0, "new")
            elif remote_same():
                self.del_remote.append(key)
            else:
                self.get(key, rf, 0, "new")
            return
        if not rexists:
            if st is not None and mode != "resync" and deletes and local_same():
                self.delete_here(key, opened)
            else:
                self.puts.append((key, l.path, (kind, l.size), l, []))
            return
        if st is None:
            if l.size == rf.base[0]:  # like after bisync --resync: the same size — the same file
                self.files[key] = self.wholestate(kind, l, rf.base)
                return
            lc = rc = True
        else:
            lc, rc = not local_same(), not remote_same()
        if lc and rc:
            if kind == "whole" and newer_here(l, rf):
                self.get(key, rf, 0, "conflict-local")
            else:
                self.get(key, rf, 0, "conflict-remote")
        elif lc:
            self.puts.append((key, l.path, (kind, l.size), l, []))
        elif rc:
            self.get(key, rf, 0, "replace")

    @staticmethod
    def wholestate(kind, l, base):
        st = {"k": kind[0], "rsize": base[0], "rmtime": base[1]}
        if kind == "link":
            st["target"] = l.target
        else:
            st["size"], st["mtime"] = l.size, l.mtime
        return st

    def delete_here(self, key, opened):
        if session_of(key) in opened:
            log(f"{key}: " + tx("deleted on another Mac, but the session is open here — will delete when it closes", "удалён на другом Mac, но сессия открыта здесь — удалю, когда закроется"))
        else:
            self.del_local.append(key)

    # ---- sync ---------------------------------------------------------------------------------
    def run(self):
        """The work folder holds copies of what travels — the text of sessions: gone when the run ends."""
        shutil.rmtree(self.work, ignore_errors=True)
        os.makedirs(self.work)
        try:
            return self.sync()
        finally:
            shutil.rmtree(self.work, ignore_errors=True)

    def sync(self):
        self.flush_pending()
        if self.a.wait_closed:  # from the hook of a session that is closing: claude exits right after it
            t0 = time.time()
            while self.a.wait_closed in self.open_now() and time.time() - t0 < 30:
                time.sleep(0.5)
        loc = self.local_files()
        rem = self.rem = self.remote()
        opened = self.opened = self.open_now() | set(self.a.open)
        known = len(self.files)
        forget = self.forget_marks(opened)

        for key in sorted(set(loc) | set(rem) | set(self.files)):
            st, l, rf = self.files.get(key), loc.get(key), rem.get(key)
            sid = sid_of(key)
            if sid in forget:
                if sid not in opened:  # while it is open, nothing of it changes, nothing is sent
                    self.forget_key(key, sid, l, rf)
                continue
            if l is None and (rf is None or rf.base is None):
                self.files.pop(key, None)
                self.insync.add(key)
                continue
            kind = kind_of(key)
            if kind == "log":
                self.plan_log(key, st, l, rf, opened)
            else:
                self.plan_whole(key, kind, st, l, rf, opened)

        # Mass-deletion guard — like bisync --max-delete, with the same log line
        pct = self.a.max_delete
        if self.a.mode != "apply" and known:
            for side, keys in (("Path1", self.del_remote), ("Path2", self.del_local)):
                if len(keys) * 100 > pct * known:
                    log(f"ERROR : too many deletes (>{pct}%, {len(keys)} of {known}) on {side}. "
                        "Run with --force if desired.")
                    if side == "Path1" and self.a.deleted_out:
                        with open(self.a.deleted_out, "a", encoding="utf-8") as f:
                            f.write("".join(k + "\n" for k in keys))
                    self.save()
                    self.outputs()
                    return 3

        for key, path, l, rf, p in self.conflicts:
            Ll = complete_end(path, l.size, 0)
            if fork_ok(key):  # decided once both versions are here
                self.get(key, rf, p, "conflict", (Ll, l))
            elif Ll > rf.L:
                self.get(key, rf, p, "conflict-local", (Ll, l))
            elif session_of(key) in opened:
                self.hold(key, rf, Ll)
            else:
                self.get(key, rf, p, "conflict-remote")

        self.do_gets()
        self.do_puts()
        self.do_deletes(rem)
        self.do_forget(rem)
        # own tails left out of the chain (a losing branch, leftovers of folding) are garbage
        for key in self.insync:
            rf = rem.get(key)
            for s in (rf.segs if rf else []):
                if s.host == self.host and (rf.base is None or s not in rf.chain):
                    self.pending.append(["s", f"{key}/{s.name}"])
        self.save()
        self.flush_pending()
        for sid, kind in self.forgot:
            if kind == "mark":
                log(tx("forgotten: ", "сессия забыта: ") + sid + tx(" — in the trash until it is cleaned (sessync restore)",
                                                                    " — в корзине, пока её не очистят (sessync restore)"))
            elif kind == "now":
                log(tx("forgotten for good: ", "сессия забыта насовсем: ") + sid
                    + tx(" — here, in the storage and in its trash", " — здесь, в хранилище и в его корзине"))
        self.outputs()
        print(self.up, self.down, self.bup, self.bdown)
        return 0

    def open_now(self):
        """Sessions open in claude on this Mac: Claude Code's registry and sessync's own hooks."""
        return open_sessions(self.a.sessions) | own_open(self.a.own_open)

    # ---- sessync forget -----------------------------------------------------------------------
    def forget_marks(self, opened):
        """Sessions to forget: asked for on this Mac (--forget DIR/<id>) and forgotten on any Mac (a
        mark in --tombs, the storage). A session asked for here gets its mark in the storage once it
        is closed, before anything of it is deleted: an interrupted run is finished by any Mac."""
        if not self.a.tombs:
            return set()
        try:
            asked = {n for n in os.listdir(self.a.forget) if UUID_RE.match(n)} if self.a.forget else set()
        except FileNotFoundError:
            asked = set()
        for sid in asked:
            with open(os.path.join(self.a.forget, sid), encoding="utf-8") as f:
                if "now" in f.read().split():
                    self.now.add(sid)
        self.tombs = {it["Path"] for it in listing(self.a.tombs) if UUID_RE.match(it["Path"])}
        new = sorted(asked - self.tombs - opened)
        if new:
            d = os.path.join(self.work, "tombs")
            os.makedirs(d)
            for sid in new:
                with open(os.path.join(d, sid), "w", encoding="utf-8") as f:
                    f.write(f"{self.host} {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
            rclone("copy", d, self.a.tombs, "--no-check-dest")
            self.tombs.update(new)
        self.forgot = [(sid, "now" if sid in self.now else "mark" if sid in asked else "tomb")
                       for sid in sorted((asked | self.tombs) - opened)]
        return asked | self.tombs

    def forget_key(self, key, sid, l, rf):
        """A file of a forgotten session: deleted here, its objects in the storage go to the trash —
        or at once, asked for "now" here. Never sent, never received."""
        if l is not None:
            self.forget_here.append(key)
        if rf is not None and sid in self.now:
            if rf.base is not None:
                self.pending.append(["b", key])
            self.pending.extend(["s", f"{key}/{s.name}"] for s in rf.segs)
        elif rf is not None:
            self.forget_there.append(key)
        self.files.pop(key, None)

    def do_forget(self, rem):
        if self.forget_there:
            self.to_trash(self.forget_there, rem)
            log(tx("forgotten sessions (sessync forget), to the trash and out of the storage: ",
                   "забытые сессии (sessync forget), в корзину и из хранилища: ")
                + str(len(self.forget_there)) + tx(" files", " файлов"))
        n = 0
        for key in self.forget_here:
            path = self.path_of(key)
            try:
                os.unlink(path)
                n += 1
            except FileNotFoundError:
                pass
            # empty folders of the session (<id>/subagents/) go with it
            d, top = os.path.dirname(path), os.path.join(self.root, key.split("/")[0])
            while d != top and d.startswith(top + os.sep):
                try:
                    os.rmdir(d)
                except OSError:
                    break
                d = os.path.dirname(d)
        now = [sid for sid, kind in self.forgot if kind == "now"]
        if now and self.a.trash:
            # "now": its copies in the trash of every day go too — the base, the tails, the session's
            # folder, its prompts
            inc = []
            for sid in now:
                inc += ["--include", f"/*/projects/*/{sid}.*", "--include", f"/*/projects/*/{sid}/**",
                        "--include", f"/*/seg/projects/*/{sid}.*/**", "--include", f"/*/seg/projects/*/{sid}/**",
                        "--include", f"/*/history/{sid}/**"]
            rclone("delete", self.a.trash.rsplit("/", 1)[0], *inc, "--fast-list")
        if n or now:
            self.save()
        if n:
            log(tx("forgotten sessions (sessync forget), deleted here: ",
                   "забытые сессии (sessync forget), удалено здесь: ") + str(n) + tx(" files", " файлов"))

    def outputs(self):
        """For sessync: open sessions that wait for another Mac's lines (id, that Mac), new branches
        (id, title), forgotten sessions settled here (id, mark or now — asked for here, tomb — on
        another Mac), all the forgotten ones (id)."""
        for path, rows in ((self.a.held_out, self.held.items()), (self.a.forks_out, self.forks),
                           (self.a.forgot_out, self.forgot)):
            if path:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("".join(f"{x}\t{y}\n" for x, y in rows))
        if self.a.forgotten_out and self.a.tombs:
            with open(self.a.forgotten_out, "w", encoding="utf-8") as f:
                f.write("".join(sid + "\n" for sid in sorted(self.tombs)))

    def do_gets(self):
        if not self.gets:
            return
        whole = {"b": [], "s": []}
        plan = []
        for key, rf, a, how, extra in self.gets:
            if not self.inside(self.path_of(key)):
                log(f"{key}: " + tx("its folder here is a link out of the projects folder — not written",
                                   "его папка здесь — ссылка за пределы папки проектов — не записываю"))
                continue
            files = []
            for path, off, cnt, is_whole, ref in self.pieces(key, rf, a, rf.L):
                if is_whole:
                    whole[ref[0]].append(ref[1])
                    files.append(os.path.join(self.work, "in", ref[0], ref[1]))
                else:
                    self.nparts += 1
                    files.append((path, off, cnt, os.path.join(self.work, "in", "p", str(self.nparts))))
            plan.append((key, rf, a, how, extra, files))
        for kind, root in (("b", self.rbase), ("s", self.rseg)):
            if whole[kind]:
                lst = os.path.join(self.work, f"get-{kind}.txt")
                with open(lst, "w", encoding="utf-8") as f:
                    f.write("\n".join(whole[kind]) + "\n")
                rclone("copy", root, os.path.join(self.work, "in", kind), "--files-from-raw", lst,
                       "--no-traverse", "--transfers", "8")
        for key, rf, a, how, extra, files in plan:
            parts = []
            for f in files:
                if isinstance(f, tuple):
                    os.makedirs(os.path.dirname(f[3]), exist_ok=True)
                    cat(f[0], f[1], f[2], f[3])
                    f = f[3]
                parts.append(f)
            if len(parts) == 1 and not isinstance(files[0], tuple):
                delta = parts[0]  # one whole object — no extra copy
            else:
                delta = os.path.join(self.work, "delta", key)
                if parts:  # the base as a clone (instant on APFS), tails appended
                    clone_prefix(parts[0], delta, os.path.getsize(parts[0]))
                else:
                    os.makedirs(os.path.dirname(delta), exist_ok=True)
                    open(delta, "wb").close()
                for f in parts[1:]:
                    append_file(delta, f)
            if os.path.getsize(delta) != rf.L - a:
                raise Fail(tx("received a different size than needed: ", "получено не столько, сколько нужно: ") + key)
            kind = kind_of(key)
            done = (self.apply_log(key, rf, a, how, extra, delta) if kind == "log"
                    else self.apply_whole(key, kind, rf, how, delta))
            if done:
                self.down += 1
                self.bdown += rf.L - a
                log(f"↓ {key}: {kb(rf.L - a)}" + ((tx(" from byte ", " с байта ") + str(a)) if a else ""))
                if self.down % 50 == 0:
                    self.save()
        self.save()

    def conflict_name(self, path):
        n = 1
        while os.path.lexists(f"{path}.conflict{n}"):
            n += 1
        return f"{path}.conflict{n}"

    def resolve(self, key, path, rf, a, extra, delta):
        """A session went two ways (see the module docstring). Both versions are here: this Mac's
        file up to Ll, and the storage's — the same first a bytes, then delta. False — left for later."""
        Ll, l = extra
        L, sid = rf.L, session_of(key)
        here, there = [(path, 0, Ll)], [(path, 0, a), (delta, 0, L - a)]
        c = a + common_lines(path, a, Ll - a, delta, 0, L - a)
        mine, theirs = c < Ll, c < L
        if not mine and not theirs:  # the same lines: nothing to decide
            self.files[key] = self.logstate(L, file_sig(path, L), -1, -1, [rf.base[0], rf.base[1]])
            return False
        win, fid, drop = mine, None, False   # win — this Mac's version keeps the name
        if mine and theirs:
            km, kt = talk(here, c, Ll), talk(there, c, L)
            if km is None or kt is None:  # not lines of JSON — damage: the longer one, a copy of the other
                if Ll <= L and sid in self.opened:
                    return self.hold(key, rf, Ll)
                return self.apply_log(key, rf, a, "conflict-local" if Ll > L else "conflict-remote", extra, delta)
            if km != kt:
                win, drop = km, True   # the conversation wins over bookkeeping, which goes
            elif not km:
                win, drop = Ll > L, True
            else:
                fl = fork_id(sid, next(iter_lines(here, c, Ll)))
                fr = fork_id(sid, next(iter_lines(there, c, L)))
                kl, kr = self.known(key, fl), self.known(key, fr)
                win = kr if kl != kr else Ll > L   # a branch made a session already gives way
                fid = fr if win else fl
        if (not win and sid in self.opened) or (fid and fid in self.opened):
            return self.hold(key, rf, Ll)
        if fid:
            loser, end = (there, L) if win else (here, Ll)
            if not self.make_fork(key, loser, c, end, fid):
                copy = self.conflict_name(path)
                self.copy_version(loser, end, copy)
                log(f"{key}: " + tx("went two ways; the other version saved as ", "разошлась на две версии; другая сохранена как ") + os.path.basename(copy))
        elif drop:
            log(f"{key}: " + tx("the other version differed only in Claude Code's service lines — they go",
                               "другая версия отличалась только служебными строками Claude Code — они убраны"))
        if win:
            st = self.files.get(key)
            if st is not None and a == st["s"] and a >= rf.base[0] and rf.boundary(a):
                self.puts.append((key, path, ("seg", a, Ll, 0), l, self.chain_drop(key, rf, a)))
            else:
                self.puts.append((key, path, ("base", 0, Ll), l, self.chain_drop(key, rf)))
        else:
            tmp = path + ".sessync.tmp"
            clone_prefix(path, tmp, a)
            append_file(tmp, delta)
            os.replace(tmp, path)
            when = self.remote_when(rf)
            if when:
                os.utime(path, (time.time(), when))
            self.files[key] = self.logstate(L, file_sig(path, L), L, os.stat(path).st_mtime_ns,
                                            [rf.base[0], rf.base[1]])
        return True

    def make_fork(self, key, parts, c, end, fid):
        """The other continuation as a session of its own, as Claude Code's /branch makes one: the
        same lines with the branch's session id, and its title right after the common start — so a
        Mac that has more of the same branch only appends to it. False — the branch here went on
        differently, or it can't be written."""
        sid = session_of(key)
        fkey = f"{key.split('/')[0]}/{fid}.jsonl"
        fpath = self.path_of(fkey)
        if not self.writable(fkey, fpath):
            return False
        title = title_of(parts, c, sid)
        old, new = f'"sessionId":"{sid}"'.encode(), f'"sessionId":"{fid}"'.encode()
        mark = json.dumps({"type": "custom-title", "customTitle": f"{title} (Branch)", "sessionId": fid},
                          ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        tmp = os.path.join(self.work, "fork", fid + ".jsonl")
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        moved = 0
        with open(tmp, "wb") as out:
            for line in iter_lines(parts, 0, c):
                moved += line.count(old)
                out.write(line.replace(old, new))
            out.write(mark)
            for line in iter_lines(parts, c, end):
                moved += line.count(old)
                out.write(line.replace(old, new))
        if not moved:  # the lines don't name their session the way this code expects: no branch
            log(f"{key}: " + tx("the lines don't carry the session id as expected", "в строках нет id сессии в ожидаемом виде"))
            return False
        n = os.path.getsize(tmp)
        if not os.path.lexists(fpath):
            place_new(tmp, fpath)
            if fkey not in self.rem:  # new everywhere: it goes out in this same run
                self.puts.append((fkey, fpath, ("base", 0, n), Local("log", fpath, n, os.stat(fpath).st_mtime_ns), []))
        else:
            have = os.path.getsize(fpath)
            if os.path.islink(fpath) or common_lines(fpath, 0, have, tmp, 0, n) != min(have, n):
                return False
            if have < n:  # more of the same branch here
                fd = os.open(fpath, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
                try:
                    if os.fstat(fd).st_size != have:
                        return False
                    with open(tmp, "rb") as f:
                        f.seek(have)
                        while True:
                            chunk = f.read(1 << 20)
                            if not chunk:
                                break
                            os.write(fd, chunk)
                finally:
                    os.close(fd)
        self.forks.append((fid, title))
        log(f"{key}: " + tx("went two ways on two Macs — the other branch is the session ",
                           "разошлась на двух Mac — другая ветка теперь сессия ") + fid)
        return True

    @staticmethod
    def copy_version(parts, end, dst):
        tmp = dst + ".sessync.tmp"
        with open(tmp, "wb") as out:
            for line in iter_lines(parts, 0, end):
                out.write(line)
        os.rename(tmp, dst)

    @staticmethod
    def remote_when(rf):
        return max([mtime_of(rf.base[1]) or 0] + [mtime_of(s.mtime) or 0 for s in rf.chain]) or None

    def apply_log(self, key, rf, a, how, extra, delta):
        path = self.path_of(key)
        if not self.writable(key, path):
            return False
        if how == "conflict":
            return self.resolve(key, path, rf, a, extra, delta)
        L, base = rf.L, [rf.base[0], rf.base[1]]
        when = self.remote_when(rf)
        if how == "append":
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
            try:
                if os.fstat(fd).st_size != a:
                    log(f"{key}: " + tx("changed during the sync — will receive next time", "файл изменился во время синхронизации — получу в следующий раз"))
                    return False
                with open(delta, "rb") as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        os.write(fd, chunk)
                os.fsync(fd)
                after = os.fstat(fd)
            finally:
                os.close(fd)
            self.files[key] = self.logstate(L, file_sig(path, L), L if after.st_size == L else -1,
                                            after.st_mtime_ns, base)
        elif how == "new":
            try:
                place_new(delta, path)  # don't overwrite a file that appeared meanwhile
            except FileExistsError:
                log(f"{key}: " + tx("appeared during the sync — will compare next time", "файл появился во время синхронизации — сверю в следующий раз"))
                return False
            if when:
                os.utime(path, (time.time(), when))
            self.files[key] = self.logstate(L, file_sig(path, L), L, os.stat(path).st_mtime_ns, base)
        elif how == "replace":  # another Mac's prompt history: the storage's version takes the copy's place
            tmp = path + ".sessync.tmp"
            clone_prefix(delta, tmp, L)
            os.replace(tmp, path)
            if when:
                os.utime(path, (time.time(), when))
            self.files[key] = self.logstate(L, file_sig(path, L), L, os.stat(path).st_mtime_ns, base)
        elif how == "conflict-remote":
            # shorter here: this version becomes a copy next to it, the storage version takes the main name
            copy = self.conflict_name(path)
            clone_prefix(path, copy, os.path.getsize(path))
            tmp = path + ".sessync.tmp"
            clone_prefix(path, tmp, a)
            append_file(tmp, delta)
            os.replace(tmp, path)
            if when:
                os.utime(path, (time.time(), when))
            self.files[key] = self.logstate(L, file_sig(path, L), L, os.stat(path).st_mtime_ns, base)
            log(f"{key}: " + tx("conflict — shorter here, saved as ", "конфликт — здесь была короче, сохранена как ") + os.path.basename(copy))
        elif how == "conflict-local":
            # longer here: the storage version becomes a copy next to it, and this one goes to the storage
            Ll, l = extra
            copy = self.conflict_name(path)
            tmp = copy + ".sessync.tmp"
            clone_prefix(path, tmp, a)
            append_file(tmp, delta)
            os.rename(tmp, copy)
            st = self.files.get(key)
            if st is not None and a == st["s"] and a >= rf.base[0] and rf.boundary(a):
                self.puts.append((key, path, ("seg", a, Ll, 0), l, self.chain_drop(key, rf, a)))
            else:
                self.puts.append((key, path, ("base", 0, Ll), l, self.chain_drop(key, rf)))
            log(f"{key}: " + tx("conflict — longer here, the storage version saved as ", "конфликт — здесь длиннее, версия хранилища сохранена как ") + os.path.basename(copy))
        return True

    def apply_whole(self, key, kind, rf, how, delta):
        path = self.path_of(key)
        if not self.writable(key, path, link=kind == "link"):
            return False
        if kind == "link":
            with open(delta, "rb") as f:
                target = os.fsdecode(f.read())
            if os.path.lexists(path):
                if not os.path.islink(path):
                    log(f"{key}: " + tx("a regular file here instead of the link — left alone", "на месте ссылки здесь обычный файл — не трогаю"))
                    return False
                os.unlink(path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.symlink(target, path)
            self.files[key] = {"k": "l", "target": target, "rsize": rf.base[0], "rmtime": rf.base[1]}
            return True
        if how.startswith("conflict") and os.path.isfile(path) and filecmp.cmp(delta, path, shallow=False):
            # identical: changed the same way on both Macs, or an interrupted run didn't record what it sent
            st = os.stat(path)
            self.files[key] = {"k": "w", "size": st.st_size, "mtime": st.st_mtime_ns,
                               "rsize": rf.base[0], "rmtime": rf.base[1]}
            return False
        if how == "new":
            try:
                place_new(delta, path)
            except FileExistsError:
                log(f"{key}: " + tx("appeared during the sync — will compare next time", "файл появился во время синхронизации — сверю в следующий раз"))
                return False
        elif how == "replace":
            os.replace(delta, path)
        elif how == "conflict-remote":
            copy = self.conflict_name(path)
            os.rename(path, copy)
            place_new(delta, path)
            log(f"{key}: " + tx("conflict — older here, saved as ", "конфликт — здесь старее, сохранено как ") + os.path.basename(copy))
        elif how == "conflict-local":
            copy = self.conflict_name(path)
            place_new(delta, copy)
            st = os.stat(path)
            self.puts.append((key, path, ("whole", st.st_size), Local("whole", path, st.st_size, st.st_mtime_ns), []))
            log(f"{key}: " + tx("conflict — newer here, the storage version saved as ", "конфликт — здесь новее, версия хранилища сохранена как ") + os.path.basename(copy))
            return True
        when = mtime_of(rf.base[1])
        if when:
            os.utime(path, (time.time(), when))
        st = os.stat(path)
        self.files[key] = {"k": "w", "size": st.st_size, "mtime": st.st_mtime_ns,
                           "rsize": rf.base[0], "rmtime": rf.base[1]}
        return True

    def do_puts(self):
        if not self.puts:
            return
        staged = []
        swap = os.environ.get("SESSYNC_TEST_REWRITE") if os.environ.get("SESSYNC_TEST_REMOTE") else None
        if swap and os.path.exists(swap.split(":", 1)[0]):  # tests: a file rewritten after the plan, before the upload
            os.replace(*swap.split(":", 1))
        for key, path, what, l, drop in self.puts:
            out = os.path.join(self.work, "out", "b", key)
            if what[0] == "whole" and not same_whole(path, l):
                # rewritten whole while this run went (a subagent's .meta.json): not a broken file, just a newer
                # version — sent next time; before, the whole run stopped with "got shorter" (code 1)
                log(f"{key}: " + tx("changed during the sync — will send next time", "изменился во время синхронизации — отправлю в следующий раз"))
                continue
            if what[0] in ("base", "whole"):
                clone_prefix(path, out, what[-1])
                if what[0] == "whole" and not same_whole(path, l):  # changed while being copied
                    os.remove(out)
                    log(f"{key}: " + tx("changed during the sync — will send next time", "изменился во время синхронизации — отправлю в следующий раз"))
                    continue
                if what[0] == "whole":  # the storage keeps the file's own time: conflicts compare it
                    os.utime(out, ns=(l.mtime, l.mtime))
                staged.append((key, path, what, l, drop, file_sig(out, what[-1]) if what[0] == "base" else None))
            elif what[0] == "link":
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "wb") as f:
                    f.write(os.fsencode(l.target))
                staged.append((key, path, what, l, drop, None))
            else:
                _, a, b, lvl = what
                name = f"{a:016d}-{b:016d}-{lvl}-{file_sig(path, b)[:12]}-{self.host}"
                copy_range(path, os.path.join(self.work, "out", "s", key, name), a, b)
                staged.append((key, path, what, l, drop, None))
        for kind, root in (("b", self.rbase), ("s", self.rseg)):
            src = os.path.join(self.work, "out", kind)
            if os.path.isdir(src):
                try:
                    rclone("copy", src, root, "--no-check-dest", "--transfers", "8")
                except Fail:
                    if kind == "b":
                        self.landed_whole(staged)
                    raise
        for key, path, what, l, drop, sig in staged:
            st = self.files.get(key)
            if what[0] == "base":
                self.files[key] = self.logstate(what[2], sig, l.size if l else -1, l.mtime if l else -1, "self")
                n = what[2]
            elif what[0] == "seg":
                if l is not None:  # folding without new data doesn't change the state
                    self.files[key] = self.logstate(what[2], file_sig(path, what[2]), l.size, l.mtime,
                                                    st.get("base") if st else None)
                n = what[2] - what[1]
            else:
                self.files[key] = self.wholestate(what[0], l, (what[1], None))
                n = what[1]
            self.pending.extend(drop)
            self.bup += n
            if l is not None:
                self.up += 1
                log(f"↑ {key}: {kb(n)}" + {"base": tx(", whole (base)", ", целиком (основа)"),
                                           "seg": tx(", appended", ", дописанное"),
                                           "whole": "", "link": tx(", link", ", ссылка")}[what[0]])
            else:
                log(f"{key}: " + tx("tails folded: ", "свёрнуто хвостов: ") + str(len(drop))
                    + (tx(" into a new base", " в новую основу") if what[0] == "base" else ""))
        self.save()

    def landed_whole(self, staged):
        """The upload failed on some files, but the others are in the storage. A file that is rewritten
        whole (subagent .meta.json, tool results) and sent here must be recorded as sent: otherwise its
        next change here meets this Mac's own upload as another Mac's change — a conflict with a copy.
        Sent means the storage has it with the size and time this Mac gave it (the time of the upload
        itself, not of the server, which a listing shows). Session files need no such step: a part sent
        by an interrupted run is recognised by its content."""
        whole = {key: (what, l) for key, path, what, l, drop, sig in staged if what[0] == "whole"}
        if not whole:
            return
        lst = os.path.join(self.work, "landed.txt")
        with open(lst, "w") as f:
            f.write("".join(k + "\n" for k in whole))
        p = subprocess.run(["rclone", "lsjson", "-R", "--files-only", "--no-mimetype", "--files-from-raw", lst, self.rbase],
                           capture_output=True)
        if p.returncode != 0:
            return
        n = 0
        for e in json.loads(p.stdout or b"[]"):
            what, l = whole.get(e.get("Path"), (None, None))
            there = mtime_of(e.get("ModTime"))
            if what and e.get("Size") == what[1] and there is not None and abs(there - l.mtime / 1e9) < 0.001:
                self.files[e["Path"]] = self.wholestate("whole", l, (what[1], None))
                n += 1
        if n:
            self.save()
            log(tx("the upload failed, but files that reached the storage are recorded as sent: ",
                   "отправка оборвалась, но файлы, дошедшие до хранилища, записаны как отправленные: ") + str(n))

    def to_trash(self, keys, rem):
        """First a copy to the trash inside the storage (no download), then the deletion: the bases
        and all the tails, saved in pending before they are deleted."""
        if not self.a.trash:
            raise Fail(tx("deletion without a trash", "удаление без корзины"))
        bases = [k for k in keys if rem[k].base is not None]
        segs = [f"{k}/{x.name}" for k in keys for x in rem[k].segs]
        for root, dst, names, kind in ((self.rbase, f"{self.a.trash}/{self.a.tree}", bases, "b"),
                                       (self.rseg, f"{self.a.trash}/seg/{self.a.tree}", segs, "s")):
            if names:
                lst = os.path.join(self.work, f"trash-{kind}.txt")
                with open(lst, "w", encoding="utf-8") as f:
                    f.write("\n".join(names) + "\n")
                rclone("copy", root, dst, "--files-from-raw", lst, "--no-traverse")
        for k in keys:
            self.files.pop(k, None)
        self.pending.extend(["b", k] for k in bases)
        self.pending.extend(["s", x] for x in segs)
        self.save()

    def do_deletes(self, rem):
        if self.del_remote:
            self.to_trash(self.del_remote, rem)
            log(tx("to the trash and out of the storage: ", "в корзину и из хранилища: ") + str(len(self.del_remote)) + tx(" files deleted on this Mac", " файлов, удалённых на этом Mac"))
        for k in self.del_local:
            try:
                os.unlink(self.path_of(k))
            except FileNotFoundError:
                pass
            self.files.pop(k, None)
        if self.del_local:
            self.save()
            log(tx("deleted here: ", "удалено здесь: ") + str(len(self.del_local)) + tx(" files deleted on another Mac", " файлов, удалённых на другом Mac"))


def quick(args, deadline):
    """An rclone call for the claude -r check: short timeouts, no retries — no answer is better than a
    long wait. None — no answer in time, or an error."""
    left = deadline - time.time()
    if left < 0.3:
        return None
    try:
        p = subprocess.run(["rclone", *args, "--retries", "1", "--low-level-retries", "1",
                            "--contimeout", "3s", "--timeout", "4s"], capture_output=True, timeout=left)
    except subprocess.TimeoutExpired:
        return None
    return p.stdout if p.returncode == 0 else None


def history_host(history, sid, me, deadline):
    """The Mac with the latest prompt in this session: each line of a Mac's prompt history names its
    session. For when the new lines came in a base, whose name doesn't say which Mac sent it."""
    out = quick(["lsf", "--files-only", history], deadline)
    best, name = 0, ""
    for fn in (out or b"").decode("utf-8", "replace").split():
        h = fn[:-6]
        if not fn.endswith(".jsonl") or h == me or me.startswith(h + "-"):  # this Mac's, also by its old name
            continue
        for line in (quick(["cat", "--tail", "262144", f"{history}/{fn}"], deadline) or b"").splitlines():
            if sid.encode() not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("sessionId") == sid and isinstance(d.get("timestamp"), (int, float)) \
                    and d["timestamp"] > best:
                best, name = d["timestamp"], h
    return name


def cmd_ahead(a):
    """claude -r has just opened a session: has another Mac written lines of it that this Mac hasn't
    got? Only this session's objects are listed — a second or so, not a whole sync; 6 s at most.
    Prints "ahead<TAB><that Mac, if known>", or nothing."""
    deadline = time.time() + 6
    mr, ml = a.map.split(":", 1) if a.map else (None, None)
    key = Engine.swap(a.key, ml, mr)
    if unsafe(key) or not key.endswith(".jsonl"):
        return 0
    try:
        with open(a.state, encoding="utf-8") as f:
            st = json.load(f).get("files", {}).get(key)
    except (OSError, ValueError):
        st = None
    out = quick(["lsjson", "--files-only", "--no-mimetype", f"{a.base}/{key}"], deadline)
    items = json.loads(out) if out else []
    if len(items) != 1:
        return 0
    rf = RFile()
    rf.base = (items[0]["Size"], items[0].get("ModTime", ""))
    out = quick(["lsjson", "--files-only", "--no-mimetype", f"{a.seg}/{key}"], deadline)
    for it in json.loads(out) if out else []:
        s = parse_seg(it["Path"], it["Size"], it.get("ModTime", ""))
        if s:
            rf.segs.append(s)
    rf.finish()
    me = re.sub(r"[^A-Za-z0-9.-]", "-", a.host)
    s = st.get("s") if isinstance(st, dict) else None
    if s is None:  # never synced here: compare with the file
        try:
            have = complete_end(a.file, os.path.getsize(a.file), 0)
        except OSError:
            have = 0
        if rf.L <= have:
            return 0
        tails = [x for x in rf.chain if x.end > have]
    else:
        if rf.L <= s:
            return 0
        tails = [x for x in rf.chain if x.end > s]
        if rf.base[0] <= s and tails and all(x.host == me for x in tails):
            return 0  # this Mac's own lines, sent earlier
    who = [x.host for x in tails if x.host != me]
    name = who[-1] if who else history_host(a.history, key.rsplit("/", 1)[-1][:-6], me, deadline)
    print("ahead\t" + re.sub(r"-[0-9A-Fa-f]{8}$", "", name))
    return 0


def cmd_fetch(base, segdir, out):
    """A transcript from a base and tails — for restoring from the trash."""
    items = listing(base)
    if len(items) != 1:
        raise Fail(tx("no base: ", "нет основы: ") + base)
    segs = [s for s in (parse_seg(it["Path"], it["Size"], "") for it in listing(segdir)) if s]
    chain, L = build_chain(items[0]["Size"], segs)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = out + ".sessync.tmp"
    try:
        with open(tmp, "wb") as f:
            rclone("cat", base, out=f)
            for s in chain:
                rclone("cat", f"{segdir}/{s.name}", out=f)
        if os.path.getsize(tmp) != L:
            raise Fail(tx("assembled a different size than needed: ", "собрано не столько, сколько нужно: ") + out)
        os.link(tmp, out)
    finally:
        os.unlink(tmp)


def cmd_describe(ident, projects):
    """sessync forget: the sessions here whose id starts with ident — id, folder, last change, title.
    A whole id that isn't here is printed alone: the session may be in the storage only."""
    if not re.fullmatch(r"[0-9a-f-]{8,36}", ident):
        return 1
    rows = []
    try:
        folders = sorted(os.listdir(projects))
    except OSError:
        folders = []
    for proj in folders:
        d = os.path.join(projects, proj)
        if os.path.islink(d) or not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            sid = fn[:-6]
            if fn.endswith(".jsonl") and sid.startswith(ident) and UUID_RE.match(sid):
                p = os.path.join(d, fn)
                st = os.lstat(p)
                if not stat.S_ISREG(st.st_mode):
                    continue
                end = complete_end(p, st.st_size, 0)
                rows.append((sid, clean(proj), time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                             title_of([(p, 0, end)], end, sid) if end else ""))
    if not rows and UUID_RE.match(ident):
        rows.append((ident, "", "", ""))
    for r in rows:
        print("\t".join(x.replace("\t", " ") for x in r))
    return 0


def project_to(line, a, b):
    """A prompt history line with its project folder under home a moved under home b (bytes)."""
    a, b = a.rstrip("/"), b.rstrip("/")
    if a == b or b'"' + a.encode() not in line:
        return line
    try:
        d = json.loads(line)
    except ValueError:
        return line
    p = d.get("project") if isinstance(d, dict) else None
    if isinstance(p, str) and (p == a or p.startswith(a + "/")):
        d["project"] = b + p[len(a):]
        return (json.dumps(d, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    return line


def drop_lines(path, sids):
    """Lines of these sessions ("sessionId") out of a prompt history file. Only whole lines; the file
    is read again right before it is replaced, so a prompt written meanwhile is kept. Returns the
    dropped lines."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return []
    for _ in range(5):
        whole = data[:data.rfind(b"\n") + 1]
        keep, gone = [], []
        for line in whole.splitlines(True):
            if b'"sessionId"' in line:
                try:
                    d = json.loads(line)
                except ValueError:
                    d = None
                if isinstance(d, dict) and d.get("sessionId") in sids:
                    gone.append(line)
                    continue
            keep.append(line)
        if not gone:
            return []
        with open(path, "rb") as f:
            data = f.read()
        if data.startswith(whole):  # Claude Code only appends: what it added meanwhile stays
            tmp = path + ".sessync.tmp"
            with open(tmp, "wb") as f:
                f.write(b"".join(keep) + data[len(whole):])
            shutil.copymode(path, tmp)
            os.replace(tmp, path)
            return gone
    raise Fail(tx("the file keeps changing: ", "файл всё время меняется: ") + path)


def cmd_forget_local(a):
    """sessync forget, on this Mac: the session's files in the projects folder, what Claude Code keeps
    for it elsewhere (files and folders named after its id: session-env, file-history, todos, debug,
    security, its temporary folder), its prompts in the prompt history (and the pasted text they
    alone refer to), its lines in this Mac's own history file for the storage, its text in the
    search index. The session must be closed. --keep FILE: its prompts are appended there first, with
    the project folders under the storage's home (--home here, --canon there), for sessync restore.
    Prints how many files and prompts went."""
    sid = a.sid
    if not UUID_RE.match(sid):
        return 1
    n = 0

    def rm(p):
        nonlocal n
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                for _, _, fns in os.walk(p):
                    n += len(fns)
                shutil.rmtree(p)
            else:
                os.unlink(p)
                n += 1
        except FileNotFoundError:
            pass

    def dirs(root):
        """Folders right inside root — not links: nothing is deleted through a link elsewhere."""
        try:
            with os.scandir(root) as it:
                return [e.path for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:
            return []

    cfg = a.config
    projects = os.path.join(cfg, "projects")
    for d in dirs(projects):
        for fn in os.listdir(d):
            if fn == sid or fn.startswith(sid + "."):   # <id>.jsonl, its copies and link, the <id>/ folder
                rm(os.path.join(d, fn))
    for d in dirs(cfg):
        if os.path.basename(d) in ("projects", "plugins"):
            continue
        for fn in os.listdir(d):
            if sid in fn:
                rm(os.path.join(d, fn))
    if a.tmp:
        for d in dirs(a.tmp):
            rm(os.path.join(d, sid))
    if a.cache:
        for d in dirs(os.path.join(a.cache, "text")):
            rm(os.path.join(d, sid + ".txt"))
    hist = os.path.realpath(os.path.join(cfg, "history.jsonl"))
    gone = drop_lines(hist, {sid})
    if a.keep and gone:
        with open(a.keep, "ab") as f:
            f.write(b"".join(project_to(x, a.home or "", a.canon or "") for x in gone))
    hashes = set()
    for line in gone:
        try:
            for v in (json.loads(line).get("pastedContents") or {}).values():
                h = v.get("contentHash") if isinstance(v, dict) else None
                if isinstance(h, str) and re.fullmatch(r"[0-9a-f]{8,64}", h):
                    hashes.add(h)
        except (ValueError, AttributeError):
            pass
    if hashes:
        try:
            with open(hist, "rb") as f:
                rest = f.read()
        except FileNotFoundError:
            rest = b""
        for h in hashes:
            if f'"{h}"'.encode() not in rest:
                rm(os.path.join(cfg, "paste-cache", h + ".txt"))
    if a.history_dir and a.own:
        drop_lines(os.path.join(a.history_dir, a.own), {sid})
    print(n, len(gone))
    log(tx(f"forgotten here: {sid} — files {n}, prompts {len(gone)}",
           f"забыта здесь: {sid} — файлов {n}, промптов {len(gone)}"))
    return 0


def cmd_put_prompts(a):
    """sessync restore: a forgotten session's prompts back into the prompt history — without repeats,
    all by time, project folders from under the storage's home (--canon) to under this one (--home).
    Prints how many came back."""
    hist = os.path.realpath(os.path.join(a.config, "history.jsonl"))
    new = []
    for p in a.files:
        with open(p, "rb") as f:
            new += [project_to(x if x.endswith(b"\n") else x + b"\n", a.canon, a.home)
                    for x in f.read().splitlines(True) if x.strip()]
    try:
        with open(hist, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        data = b""
    have = set(data.splitlines(True))
    new = [x for x in dict.fromkeys(new) if x not in have]
    if new:
        def ts(x):
            try:
                return float(json.loads(x).get("timestamp") or 0)
            except (ValueError, AttributeError, TypeError):
                return 0.0
        whole = data[:data.rfind(b"\n") + 1]
        lines = sorted(whole.splitlines(True) + new, key=ts)
        with open(hist, "rb") as f:
            now = f.read()
        tmp = hist + ".sessync.tmp"
        with open(tmp, "wb") as f:   # what Claude Code appended meanwhile stays at the end
            f.write(b"".join(lines) + (now[len(whole):] if now.startswith(whole) else b""))
        if os.path.exists(hist):
            shutil.copymode(hist, tmp)
        os.replace(tmp, hist)
    print(len(new))
    return 0


def main():
    global LOG
    os.umask(0o077)
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("sync")
    for name in ("tree", "local", "base", "seg", "state"):
        s.add_argument(name)
    s.add_argument("--host", required=True)
    s.add_argument("--work", required=True)
    s.add_argument("--mode", choices=("normal", "resync", "apply"), default="normal")
    s.add_argument("--no-deletes", action="store_true")
    s.add_argument("--skip-memory", action="store_true")
    s.add_argument("--trash")
    s.add_argument("--sessions")
    s.add_argument("--max-delete", type=int, default=50)
    s.add_argument("--map", metavar="REMOTE:LOCAL")  # with "=": the names start with "-"
    s.add_argument("--deleted-out")
    s.add_argument("--open", action="append", default=[], metavar="SESSION")  # open, even if not registered yet
    s.add_argument("--wait-closed", metavar="SESSION")   # wait (30 s at most) until it is closed
    s.add_argument("--held-out")
    s.add_argument("--forks-out")
    s.add_argument("--own-open", metavar="DIR")
    s.add_argument("--forget", metavar="DIR")      # sessions to forget, asked for here: DIR/<session id>
    s.add_argument("--tombs", metavar="REMOTE")    # forgotten sessions, marks in the storage: REMOTE/<session id>
    s.add_argument("--forgot-out")
    s.add_argument("--forgotten-out")
    s.add_argument("--own", metavar="KEY")         # prompt history: this Mac's file; the others' are only received
    s.add_argument("--log")
    k = sub.add_parser("ahead")
    for name in ("base", "seg", "history", "file", "state"):
        k.add_argument(name)
    k.add_argument("--key", required=True)  # given as --key=…: project folders start with "-"
    k.add_argument("--host", required=True)
    k.add_argument("--map", metavar="REMOTE:LOCAL")
    k.add_argument("--log")
    f = sub.add_parser("fetch")
    for name in ("base", "segdir", "out"):
        f.add_argument(name)
    f.add_argument("--log")
    o = sub.add_parser("opened")   # exit code 0 — the session is open in claude on this Mac
    o.add_argument("sid")
    o.add_argument("--sessions")
    o.add_argument("--own-open", metavar="DIR")
    o.add_argument("--log")
    d = sub.add_parser("describe")
    d.add_argument("ident")
    d.add_argument("--projects", required=True)
    d.add_argument("--log")
    g = sub.add_parser("forget-local")
    g.add_argument("sid")
    g.add_argument("--config", required=True)
    g.add_argument("--history-dir")
    g.add_argument("--own")
    g.add_argument("--cache")
    g.add_argument("--tmp")
    g.add_argument("--keep")
    g.add_argument("--home")
    g.add_argument("--canon")
    g.add_argument("--log")
    r = sub.add_parser("put-prompts")
    r.add_argument("files", nargs="+")
    r.add_argument("--config", required=True)
    r.add_argument("--home", required=True)
    r.add_argument("--canon", required=True)
    r.add_argument("--log")
    a = ap.parse_args()
    if not a.cmd:
        ap.error(tx("a command is needed: sync, ahead, fetch, opened, describe, forget-local or put-prompts",
                    "нужна команда: sync, ahead, fetch, opened, describe, forget-local или put-prompts"))
    LOG = a.log
    try:
        if a.cmd == "fetch":
            cmd_fetch(a.base, a.segdir, a.out)
            return 0
        if a.cmd == "ahead":
            return cmd_ahead(a)
        if a.cmd == "opened":
            return 0 if a.sid in open_sessions(a.sessions) | own_open(a.own_open) else 1
        if a.cmd == "describe":
            return cmd_describe(a.ident, a.projects)
        if a.cmd == "forget-local":
            return cmd_forget_local(a)
        if a.cmd == "put-prompts":
            return cmd_put_prompts(a)
        os.makedirs(os.path.dirname(a.state), exist_ok=True)
        return Engine(a).run()
    except (Fail, OSError) as e:
        log(f"ERROR : {e}")
        print(f"engine: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
