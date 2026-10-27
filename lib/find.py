"""sessync find — search all Claude Code sessions, including ones from your other Macs.

  sessync find                     latest sessions in all projects
  sessync find word [word …]       sessions where the conversation has all the words (any case)
  sessync find "exact phrase"      the whole phrase
  sessync find -p my-site …        only projects with this string in the path
  sessync find -n 40 …             how many sessions to show (15 by default)
  sessync find --raw …             search raw transcripts, tool output included
  sessync find --no-mask …         show key-like strings as they are (masked by default)

Output passes a mask: strings that look like keys (sk-…, ghp_…, github_pat_…, AKIA…, long
base64/hex, password=/token=/secret= with a value) are shown as ***.

Searches the conversation itself — your messages and Claude's answers — not the injected service
text (CLAUDE.md, skill descriptions, command output): otherwise any word from there would match
everywhere. For that it keeps an index in ~/.cache/sessync/find; the first run builds it, later
runs read only what was appended. For each session: last activity, folder, title, a snippet with
the match, and the command to continue it.
"""
DOC_RU = """sessync find — поиск по всем сессиям Claude Code, включая пришедшие с других Mac.

  sessync find                     последние сессии во всех проектах
  sessync find слово [слово …]     сессии, где в переписке есть все слова (регистр не важен)
  sessync find "точная фраза"      фраза целиком
  sessync find -p my-site …        только проекты, в пути которых есть эта строка
  sessync find -n 40 …             сколько сессий показать (по умолчанию 15)
  sessync find --raw …             искать по сырым транскриптам, включая вывод инструментов
  sessync find --no-mask …         показывать похожее на ключи как есть (по умолчанию скрыто)

Вывод проходит маску: похожее на ключи (sk-…, ghp_…, github_pat_…, AKIA…, длинные base64/hex,
password=/token=/secret= со значением) показывается как ***.

Ищет по самой переписке — вашим сообщениям и ответам Claude, — а не по служебным вставкам
(CLAUDE.md, описания навыков, вывод команд): иначе любое слово оттуда находилось бы везде.
Для этого держит индекс в ~/.cache/sessync/find; первый запуск строит его с нуля, дальше дочитывает
только дописанное. Для каждой сессии: время последней активности, папка, заголовок, фрагмент
с совпадением и команда, которой её продолжить.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "sessync" / "find"
TEXT = CACHE / "text"
STATE = CACHE / "state.json"
HOME = str(Path.home()).rstrip("/")
# The storage's home folder (sessync writes it): another user name on another Mac — the folders
# recorded in transcripts are under it, and here they are under HOME
CANON = (os.environ.get("SESSYNC_HOME_CANON") or HOME).rstrip("/")
TTY = sys.stdout.isatty()
RU = os.environ.get("SESSYNC_LANG") == "ru"


def tx(en, ru):
    return ru if RU else en

# The index is a copy of the conversations: owner-only, like the transcripts themselves
os.umask(0o077)


def clean(s):
    """Text from a transcript is data: control characters (ESC sequences, bidi) must not reach
    the terminal and run there."""
    return "".join(ch for ch in (s or "") if unicodedata.category(ch) not in ("Cc", "Cf"))


MASK = "***"
# Order matters: a labelled value first, then the known key shapes, then long opaque runs.
SECRET_RES = [
    re.compile(r"(?i)\b((?:pass(?:word|wd)?|pwd|token|secret|api[_-]?key|access[_-]?key)[\"']?\s*"
               r"(?:=|\"\s*:)\s*[\"']?)[^\s\"',;&)}\]]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
]
B64_RE = re.compile(r"[A-Za-z0-9+/_-]{32,}={0,2}")


def _b64(m):
    t = m.group(0)
    # a key has all kinds of characters; a long word or a path has not
    if re.search(r"[a-z]", t) and re.search(r"[A-Z]", t) and re.search(r"[0-9]", t):
        return MASK
    return t


def mask(s):
    """Hide what looks like a key. Applied to everything `find` prints unless --no-mask."""
    s = s or ""
    s = SECRET_RES[0].sub(lambda m: m.group(1) + MASK, s)
    for rx in SECRET_RES[1:]:
        s = rx.sub(MASK, s)
    return B64_RE.sub(_b64, s)


def c(code, s):
    return f"\033[{code}m{s}\033[0m" if TTY else s


def rg_bin():
    return shutil.which("rg") or next(
        (p for p in ("/opt/homebrew/bin/rg", "/usr/local/bin/rg") if os.path.exists(p)), None)


def flat(s):
    return re.sub(r"\s+", " ", s).strip()


def texts(d):
    """Conversation lines from one transcript record: ('U' | 'A', text)."""
    t = d.get("type")
    if t == "queue-operation" and d.get("operation") == "enqueue" and isinstance(d.get("content"), str):
        yield "U", d["content"]
        return
    if t not in ("user", "assistant") or d.get("isMeta"):
        return
    m = (d.get("message") or {}).get("content")
    role = "U" if t == "user" else "A"
    if isinstance(m, str):
        if not m.lstrip().startswith("<"):
            yield role, m
        return
    if isinstance(m, list):
        for x in m:
            if isinstance(x, dict) and x.get("type") == "text" and isinstance(x.get("text"), str):
                if role == "A" or not x["text"].lstrip().startswith("<"):
                    yield role, x["text"]


def index_file(src, st, out_path, meta):
    """Reads src on from the saved position and appends the conversation text to out_path."""
    try:
        sb = src.stat()
    except OSError:
        return
    key = str(src.relative_to(ROOT))
    rec = st.get(key)
    # A new inode — the file was replaced whole (downloaded from another Mac): index it again
    if rec and (rec["ino"] != sb.st_ino or rec["off"] > sb.st_size):
        rec = None
    if rec and rec["off"] == sb.st_size:
        return
    off = rec["off"] if rec else 0
    mode = "a" if rec else "w"
    if not rec and src.parent.name != "subagents" and out_path.exists():
        out_path.unlink()
        meta.pop("first", None)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as f, open(out_path, "a" if mode == "a" or src.parent.name == "subagents" else "w",
                                    encoding="utf-8") as out:
        f.seek(off)
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # the line is still being written — next time
            off += len(raw)
            if b'"ai-title"' in raw[:200] or b'"custom-title"' in raw[:200]:
                try:
                    d = json.loads(raw)
                    meta["title"] = d.get("customTitle") or d.get("aiTitle") or meta.get("title")
                except ValueError:
                    pass
                continue
            # Quick skip of service records; keys come in any order, so look through the whole line
            if not (b'"type":"user"' in raw or b'"type":"assistant"' in raw or b'"queue-operation"' in raw):
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            # The folder the session started in: claude -r looks for the session by it,
            # while cwd in later records follows cd in commands
            if d.get("cwd") and "cwd" not in meta and src.parent.name != "subagents":
                meta["cwd"] = d["cwd"]
            if d.get("timestamp"):
                meta["last"] = d["timestamp"]
            for role, s in texts(d):
                s = flat(s)
                if not s:
                    continue
                if role == "U" and "first" not in meta and src.parent.name != "subagents":
                    meta["first"] = s[:300]
                out.write(f"{role}: {s}\n")
    st[key] = {"ino": sb.st_ino, "off": off}


def update_index():
    try:
        st = json.loads(STATE.read_text())
    except (OSError, ValueError):
        st = {}
    files = st.setdefault("files", {})
    metas = st.setdefault("meta", {})
    todo = []
    for src in ROOT.glob("*/*.jsonl"):
        todo.append((src, src))
        sub = src.with_suffix("") / "subagents"
        if sub.is_dir():
            todo += [(p, src) for p in sub.glob("*.jsonl")]
    def size(p):  # a sync may have deleted the file during the walk
        try:
            return p.stat().st_size
        except OSError:
            return -1
    pending = [t for t in todo if files.get(str(t[0].relative_to(ROOT)), {}).get("off") != size(t[0])]
    if len(pending) > 20 and TTY:
        print(c("2", tx("Updating the search index: ", "Обновляю индекс поиска: файлов ") + f"{len(pending)}…"), file=sys.stderr)
    # the main file of a session before its subagents: reindexing it clears the shared text
    pending.sort(key=lambda t: t[0].parent.name == "subagents")
    for src, main in pending:
        key = str(main.relative_to(ROOT))
        out = TEXT / main.parent.name / (main.stem + ".txt")
        meta = metas.setdefault(key, {})
        if src is main and str(src.relative_to(ROOT)) in files:
            rec = files[str(src.relative_to(ROOT))]
            try:
                sb = src.stat()
            except OSError:
                continue
            if rec["ino"] != sb.st_ino or rec["off"] > sb.st_size:
                # the text is rebuilt whole — this session's subagents too
                for k in [k for k in files if k.startswith(str(main.with_suffix("").relative_to(ROOT)) + "/")]:
                    del files[k]
                pending += [(p, main) for p in (main.with_suffix("") / "subagents").glob("*.jsonl")
                            if (p, main) not in pending]
        index_file(src, files, out, meta)
    # sessions that are gone
    alive = {str(t[0].relative_to(ROOT)) for t in todo}
    for k in [k for k in files if k not in alive]:
        del files[k]
    for k in [k for k in metas if k not in alive]:
        metas.pop(k)
        p = TEXT / Path(k).parent / (Path(k).stem + ".txt")
        p.unlink(missing_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    os.chmod(CACHE, 0o700)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False))
    os.replace(tmp, STATE)
    return metas


def search(terms, rg, base, glob):
    found = None
    for t in terms:
        if rg:
            cmd = [rg, "-l", "-i", "-F", "--no-messages", "-g", glob, "-g", "!**/memory/**", "--", t, str(base)]
        else:
            cmd = ["grep", "-rliF", f"--include={glob}", "--exclude-dir=memory", "--", t, str(base)]
        s = {Path(p) for p in subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines() if p}
        found = s if found is None else found & s
        if not found:
            break
    return found or set()


def rx_escape(s):
    return re.sub(r"([\\.+*?()|\[\]{}^$#&\-~])", r"\\\1", s)


def snippet(path, term, rg, raw):
    if not rg:
        return ""
    pat = "(?i).{0,80}" + rx_escape(term) + ".{0,100}"
    out = subprocess.run([rg, "-o", "-m", "1", "-N", "--no-filename", "-e", pat, str(path)],
                         capture_output=True, text=True).stdout
    s = out.splitlines()[0] if out else ""
    if raw:
        s = s.replace("\\n", " ").replace('\\"', '"').replace("\\t", " ").replace("\\\\", "\\")
    return flat(s)


def here(p):
    """A folder recorded on a Mac with the storage's home → the same folder on this Mac."""
    if p and CANON != HOME and (p == CANON or p.startswith(CANON + "/")):
        return HOME + p[len(CANON):]
    return p


def short(p):
    return "~" + p[len(HOME):] if p and (p == HOME or p.startswith(HOME + "/")) else (p or "?")


def when(m, path):
    try:
        return datetime.fromisoformat(m["last"].replace("Z", "+00:00")).astimezone()
    except (KeyError, ValueError, AttributeError):
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("terms", nargs="*")
    ap.add_argument("-p", "--project")
    ap.add_argument("-n", type=int, default=15)
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--no-mask", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    a = ap.parse_args()
    if a.help:
        print((DOC_RU if RU else __doc__).strip())
        return
    hide = (lambda x: x) if a.no_mask else mask
    rg = rg_bin()
    metas = update_index()

    hits = {}  # session key → file for the snippet
    if a.terms and a.raw:
        for p in search(a.terms, rg, ROOT, "*.jsonl"):
            main = p.parent.parent.with_suffix(".jsonl") if p.parent.name == "subagents" else p
            if main.exists():
                hits.setdefault(str(main.relative_to(ROOT)), p)
    elif a.terms:
        for p in search(a.terms, rg, TEXT, "*.txt"):
            hits[f"{p.parent.name}/{p.stem}.jsonl"] = p
    else:
        hits = {k: None for k in metas}
    if a.project:
        hits = {k: v for k, v in hits.items() if a.project.lower() in k.split("/")[0].lower()}
    if not hits:
        print(tx("Nothing found", "Ничего не найдено"))
        return

    rows = sorted(((when(metas.get(k, {}), ROOT / k), k, v) for k, v in hits.items() if (ROOT / k).exists()),
                  reverse=True)
    for last, k, hit in rows[: a.n]:
        m = metas.get(k, {})
        name = hide(clean(m.get("title") or m.get("first") or ""))
        if len(name) > 90:
            name = name[:89] + "…"
        cwd = here(m.get("cwd"))
        print(c("1", last.strftime("%Y-%m-%d %H:%M")) + "  " + c("36", hide(clean(short(cwd)))) + "  " + name)
        if hit:
            sn = hide(clean(snippet(hit, a.terms[0], rg, a.raw)))
            if sn:
                print("   " + c("2", "…" + sn[:220] + "…"))
        sid = clean(Path(k).stem)
        if cwd and os.path.isdir(cwd):
            print("   " + c("32", f"cd {clean(short(cwd))} && claude -r {sid}"))
        else:
            print("   " + c("33", f"claude -r {sid}") + c("2", tx("  (the project folder is not on this Mac)", "  (папки проекта нет на этом Mac)")))
    if len(rows) > a.n:
        print(c("2", tx("… and ", "… и ещё ") + f"{len(rows) - a.n}" + tx("; show more: -n ", "; показать больше: -n ") + f"{len(rows)}"))


if __name__ == "__main__":
    main()
