# sessync — rules for coding agents (Claude Code, Codex) when changing the code

How to **use** it (find past sessions, sync) is in README.md, section "Tell Claude about it": users
put that text into their `~/.claude/CLAUDE.md`. This file is about changing the code.

## Layout

- `bin/sessync` — bash: settings, Keychain, rclone crypt, prompt history, hooks, the background
  loop, user commands. The engine syncs the files.
- `lib/engine.py` — python3 with no dependencies on top of rclone: transcripts are a base plus
  tails (only appended bytes travel), attachments go whole, links as `.rclonelink`; conflicts,
  trash, mass-deletion guard, tail compaction, home-folder mapping. Details — its docstring.
- `lib/find.py` — `sessync find`: an index of the conversations in `~/.cache/sessync/find`, reads
  only what was appended (by offset and inode).
- `install.sh` / `uninstall.sh` — dependencies, the link in `~/.local/bin`, hooks and
  `cleanupPeriodDays` in `settings.json` (through `sessync hooks`), with a backup.
- `tests/run.sh` — scenarios on several "Macs" with a local folder instead of the storage;
  `tests/tty.py` — runs `setup` and `export` in a pseudo-terminal, answering their prompts.
- `docs/demo.py` — records `docs/demo.gif` (made-up sessions, real commands; needs `agg`).
- `.github/workflows/test.yml` — CI. The Homebrew formula is in
  [latark/homebrew-tap](https://github.com/latark/homebrew-tap) (`Formula/sessync.rb`).

## Invariants — don't break

1. **Secrets** (storage keys, the encryption password) live only in the Keychain, service
   `KEYCHAIN_SERVICE`. Write — `kc_set` (through `security -i` and stdin, not an argument), read —
   `secret`. Never print, log, or put them into files or command arguments; rclone gets them through
   `RCLONE_CONFIG_*` in the environment. `export` puts them only into the clipboard and only through
   `clip` (this Mac's clipboard without Universal Clipboard, Concealed/Transient marks for clipboard
   managers), not `pbcopy`. Input — only `setup` in the user's terminal (`[ -t 0 ]`), never through
   Claude.
2. **The archive is not lost.** Before any write — `check_key`: another password means refusal,
   otherwise a second, unreadable copy appears in the storage. `cleanupPeriodDays < 3650`
   (`fix_retention`) — raise it to `RETENTION` in settings.json (with a backup), and in the same run
   use the engine's `resync` mode (union only, deletes nothing): otherwise Claude Code's own cleanup
   at startup would reach the storage and the other Macs; this way the deleted sessions come back.
   The mass-deletion guard (`--max-delete`, 50%) is never switched off; `apply` mode only through
   `sessync deletes --apply`. What is deleted on this Mac is first copied to `ss:trash/<date>/` (the
   base and all tails), then deleted; no copy — no deletion. `sessync forget` (one session, by its
   id or `! sessync forget` typed by the user in it; confirmed or `-y` otherwise; only once it is
   closed) puts a mark into the storage (`forgotten/<id>`) before anything of it is deleted there;
   its files go to the trash, its prompts too (`trash/<date>/history/<id>/<Mac>.jsonl`), and every
   Mac drops its copy and its prompts. Nothing of it is sent or received while the mark is there
   (resync and `deletes --undo` don't bring it back); only `sessync restore` takes the mark out,
   before putting the files back. `--now` is the one exception to the trash: the user asks for no
   copy, so its objects go at once and its copies in the trash too. On this Mac the local mark
   (`$st/forget/<id>`) is written before anything is deleted; `--cancel` only while nothing is.
   Deletions in the storage are first saved to `pending` in the engine state, then done: an
   interrupted run finishes them. Only deleted files go to the trash, never old versions.
   `clean_trash` deletes folders older than `TRASH_DAYS` once a day. Other Macs' tails may be
   deleted only when their data is kept in another form: folding or a new base (the same text), or a
   conflict where the other version is kept as a branch session or a copy (only a continuation of
   Claude Code's service lines — no user or assistant message — may go: it is not worth a branch).
   Under a new base, off-chain tails go too: the file only grows (shorter is always a conflict), so
   a tail starting inside the base can never attach again, and its data is with the Mac that sent
   it. Own off-chain tails are garbage only after the file was settled in this run.
3. **Storage format.** crypt parameters and the layout — `v1/` (`projects/`, `history/`,
   `seg/<tree>/<file path>/<tail>`, `trash/<date>/…` (`history/<id>/<Mac>.jsonl` — prompts of a
   forgotten session), `forgotten/<session id>`), `v1-check/keycheck`. The check file's first line
   (`cc-sync key check v1`, from the tool's old name) never changes; the second is the format
   (`FORMAT`), the third — the storage's home folder (`home /Users/…`). A tail is named
   `<start:016d>-<end:016d>-<level>-<signature:12>-<Mac>`. Changing the layout or names means
   raising `FORMAT` and a migration: old data must stay readable, old versions must stop.
4. **Transcripts are only appended to** — tails, signatures of the last 4 KB and the search index
   by offset all depend on it. The engine sends a rewritten file whole only if it got longer and the
   storage didn't change; otherwise it's a conflict. Only whole lines are sent. The home-folder
   mapping renames the first path component only and never touches file contents (prompt history is
   merged line by line and may be rewritten). If Claude Code starts rewriting transcripts, revisit
   all of this.
5. **Files in `~/.local/state/sessync/`** are a contract with users' status lines: `error` (one
   line, in the interface language), `conflicts` (a number), `off` (a mark), `last` (tab-separated
   fields: time, epoch, sent, received, seconds, kind; only the last may be empty). Machine checks
   use marks, not texts (`deletes-stop`). Changing them — update `statusline`, both READMEs, and say
   so in CHANGELOG.
6. **Background only while `claude` is open**: SessionStart/SessionEnd hooks and `_loop`, which ends
   by itself when there is no `claude` process. No launchd, cron or login items. The state is
   visible (`status`, `statusline`), there is a switch (`off`/`on`). The lock is `lock`; whoever
   doesn't get it leaves `again`, and the holder (`sync_all`) runs once more: otherwise the tail of
   a session closed during a long upload would wait for the next run. Each Mac's prompt history is
   `ss:history/<host_id>.jsonl` (name + start of the hardware UUID: Mac names can be the same), only
   appended with its own new prompts (locally — `history/<host_id>.jsonl` in the state folder);
   `sessync forget` is the only rewrite: the Mac takes a forgotten session's lines out and sends the
   file whole. So other Macs' files are only received (`--own`), and their storage version replaces
   the copy here; format 3 stops the 2.x versions that would upload the old copy back. A forgotten
   session's lines in the file of a Mac that no longer syncs stay there, encrypted; every Mac
   filters them out (`forgotten-all`). Compaction is done only by the Mac that appended last.
   `host_id` needs `scutil` and `ioreg` from `/usr/sbin`, which hooks don't have in `PATH` —
   `sessync` adds it.
7. **Transcripts are other people's data.** Everything created on disk is owner-only (`umask 077`
   in `sessync`, `os.umask(0o077)` in the engine and find, `chmod 700` of the index), like Claude
   Code's own files (700/600). The engine's work folder holds copies of what travels and is removed
   when each run ends. Any text from transcripts goes through `clean()` before reaching the
   terminal (strips Cc and Cf: ESC/OSC, BEL, bidi), or command output and page text would run in it.
8. **Two languages.** Every message a person sees — `t`/`say` in bash, `tx` in python — has English
   and Russian. Log formats parsed by code stay as they are (`too many deletes (>50%, X of N) on
   PathN`). Tests read Russian (`SESSYNC_LANG=ru`), one section checks English.
9. **bash 3.2** (`/bin/bash` on macOS): no associative arrays, `${v,,}`, `mapfile`, `&>>`; an empty
   array under `set -u` only as `${a[@]+"${a[@]}"}`; a variable right before a non-ASCII character
   only in braces (`«${title}»`: bash 3.2 takes the bytes of `»` into the name). **python 3.9**
   (`/usr/bin/python3`): no `match`, no `X | Y` in types, no nanoseconds in `fromisoformat`;
   argparse values starting with `-` only as `--opt=value`.
10. **macOS only for now**: Keychain, `osascript`, `scutil`, `ioreg`. Keep new code portable (no
    `stat -f`, `date -j`), Linux is next.
11. **The file of a session open on this Mac is not changed** (`opened`: sessync's own list
    `~/.local/state/sessync/open/<session id>` holding the pid of its `claude`, written by the hooks
    from their documented input; Claude Code's internal registry `~/.claude/sessions/`; `--open`). Claude Code reads a transcript when it opens it and then
    only appends: lines put into the file under it would split the conversation inside one file,
    where Claude Code shows one branch and hides the other. Lines from another Mac and a losing
    version wait (`hold`) until the session closes; the SessionEnd hook passes its id
    (`--wait-closed`). The SessionStart hook on resume runs only `engine ahead` in the foreground
    (this session's objects, 6 s at most; `RESUME_CHECK=off` skips it) and may print one JSON object
    (a warning) — nothing else goes to its stdout; the sync itself runs in the background.
12. **Branches.** A session (`<project>/<uuid>.jsonl`) that went two ways becomes two sessions, as
    Claude Code's `/branch` makes them. The branch id is `uuid5(FORK_NS, "<session id>:<sha256 of
    the first line where the versions part>")` and its file is the common start, a `custom-title`
    line "<title> (Branch)", then the continuation — with only `"sessionId":"<id>"` replaced. Every
    Mac must compute the same bytes: a Mac with more of the same branch only appends, and a branch
    that already exists gives way (its Mac appends to it instead of splitting again). So
    `FORK_NS`, the id scheme and the file layout never change; the title comes only from the
    transcript (never the interface language).

## Checking

On a Mac where the command in `~/.local/bin` is a link into a working copy of this repository, an
edit is live at once: hooks and the background loop run it. Before changing the code — `sessync
off`, after `tests/run.sh` and checking — `sessync on`.

`tests/run.sh` must pass whole, also with `/usr/bin/python3` (python 3.9). New behaviour — a new
scenario in it. **The full run goes to GitHub Actions, not this Mac**: the repository is public, so
its macOS minutes are free, and `.github/workflows/test.yml` runs both pythons on every push and pull
request (~20 min). Push a branch, open a pull request and wait for its checks (`gh pr checks`); don't
run the whole `tests/run.sh` locally — it takes more than 10 minutes and loads the Mac. Locally only
what is quick to look at while writing a scenario. Tests don't touch the Keychain, the clipboard, the real `~/.claude` or
`~/.local/bin`: the `SESSYNC_TEST_REMOTE` mode (a local folder instead of the storage; secrets from
the environment or the folder `SESSYNC_TEST_KEYCHAIN`; the clipboard — a named pasteboard
`SESSYNC_TEST_PASTEBOARD`) and a temporary `HOME` for install/uninstall. On a real storage:
`sessync` twice in a row (the second time "sent 0"), `sessync status --remote`.

## Releasing

1. `VERSION` in `bin/sessync`, a section with the date in `CHANGELOG.md`.
2. CI is green on the pull request (both pythons; see Checking).
3. `git tag vX.Y.Z && git push origin vX.Y.Z`; `gh release create vX.Y.Z` with the CHANGELOG section.
4. The tap (`latark/homebrew-tap`, `Formula/sessync.rb`): `url` of the new tag, `sha256` —
   `curl -sL <url> | shasum -a 256`; `brew audit --strict --online latark/tap/sessync`,
   `brew reinstall --build-from-source latark/tap/sessync && brew test sessync`; push.
5. Commits — with the GitHub noreply address (`git config user.email` in this repository).
