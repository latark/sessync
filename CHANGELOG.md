# Changelog

## Unreleased

- `sessync find` masks what looks like a key in its output: `sk-…`, `ghp_…`, `github_pat_…`, `AKIA…`, long base64
  and hex, values of `password=`, `token=`, `secret=`. `--no-mask` shows the text as it is. The mask is for the
  case when the output goes to a place that should not see secrets; it is not a guarantee that nothing is left.

## 3.0.2 — 2026-10-15

- A background session (`claude --bg`) is kept open under its own process, not the shared `claude daemon`:
  before, each new background session replaced the previous one's record, and the last one stayed
  "open" while the daemon lived, so `sessync forget` and lines from other Macs waited for it. Records
  left under the daemon's pid are removed by the hook.

## 3.0.1 — 2026-10-14

- Notifications go through [terminal-notifier](https://github.com/julienXX/terminal-notifier) 3 when it is
  installed (`brew install terminal-notifier`): on macOS 26 notifications sent with `osascript` are
  unreliable. Without it, sessync notifies as before. Repeated notifications replace the previous one.

## 3.0.0 — 2026-10-11

- `sessync forget <id>` deletes a session everywhere, for a conversation that was never meant to be
  kept: here and in the storage, on your other Macs with their next sync, its prompts in the prompt
  history on every Mac (with the text pasted into them), what Claude Code keeps of it outside the
  projects folder (`session-env`, `file-history`, `todos`, `debug`, `security`, its temporary
  folder), its text in the search index. The conversation and its prompts wait in the storage trash
  for `TRASH_DAYS` (30): `sessync restore <id>` brings them back on any Mac, and the other Macs get
  the session again. `--now` deletes it for good at once, its copies in the trash too. Inside a
  session, `! sessync forget` forgets that session once you leave it; until then it is not synced.
  `--cancel` changes your mind while nothing is deleted yet. `sessync status` shows sessions
  waiting for it, `sessync trash` marks forgotten ones.
- Storage format 3: marks of forgotten sessions (`forgotten/<id>`), and another Mac's prompt
  history follows its storage version even when it got shorter. sessync 2.x stops with "the storage
  is in a newer format — update sessync" instead of bringing forgotten prompts back: update every
  Mac.
- Fix: hooks start without `/usr/sbin` in `PATH`, so there sessync could not read the hardware UUID,
  and this Mac's prompt history went to a second file named without it.
- The engine's work folder — copies of what travelled, session text among them — is deleted at the
  end of every run instead of staying until the next one.

## 2.1.1 — 2026-10-11

- `claude -r` waits about a second instead of up to 20: the start hook checks only the resumed
  session against the storage (6 s at most), and the whole sync goes on in the background.
  `RESUME_CHECK=off` in the config turns the check off.
- The warning names the other Mac also when its lines went to the storage whole: the name comes from
  that Mac's prompt history, whose lines name their session.
- sessync keeps its own list of open sessions, from the hooks' input (documented by Claude Code),
  next to Claude Code's internal registry: if that one changes, open sessions are still left alone.
- A branch is made only if the session's lines carry its id the way sessync expects; otherwise the
  other version is kept as a copy, as before 2.1.0.

## 2.1.0 — 2026-10-11

- A session continued on two Macs between syncs now splits the way Claude Code's own `/branch`
  does: one branch keeps the session, the other becomes a session of its own, "<title> (Branch)",
  that `/resume`, `claude -r` and `sessync find` see. Before, the other version was a
  `<id>.jsonl.conflict1` copy that Claude Code didn't show. Every Mac makes the same branch, and a
  Mac whose branch already became a session appends to it instead of splitting again. A
  continuation of only Claude Code's service lines (a session opened and closed) is not worth a
  branch and is dropped. Other files still get `.conflictN` copies.
- The file of a session open in `claude` on this Mac is no longer changed: Claude Code works from
  what it read, and lines put under it would split the conversation inside the file, with one
  branch hidden. Lines from another Mac wait until the session closes; the end hook waits for
  `claude` to exit, then syncs.
- `claude -r` on a Mac with an older copy of the session: the start hook syncs first (Claude's first
  answer waits for hooks anyway, 20 s at most) and shows a warning with the command to open the
  latest version.
- `sessync` run by hand while a sync is running waits for it and then syncs, instead of leaving.
- `sessync conflicts` and `sessync status` list the sessions that split.
- Notifications pass their text to `osascript` as an argument, not inside the script.

## 2.0.1 — 2026-10-10

- Security: nothing is written outside `~/.claude/projects`. A path from the storage with `..` is
  skipped; a file is not written through a link to a folder elsewhere (links arrive from the storage
  as they are), checked right before each write, so a link received in the same sync counts too;
  a file that is itself a link here is not written through. Writing such things into the storage
  takes the encryption password, so this closes the way from one compromised Mac to the others.
  `sessync restore` checks the same.

## 2.0.0 — 2026-10-10

The tool is now **sessync** and is ready for other people's Macs.

- Homebrew: `brew install latark/tap/sessync`.
- One command: `sessync` (sync), `sessync find` (was `cc-find`), `sessync status`, … `install.sh`
  replaces the old `cc-sync`/`cc-find` links and hooks.
- English interface; Russian when the system language is Russian (`LANGUAGE=` in the config).
- Settings in `~/.config/sessync/config`, state in `~/.local/state/sessync/`, search index in
  `~/.cache/sessync/find` — no longer inside `~/.claude`. The status-line files keep their format.
- `sessync setup` asks where to keep the sessions: Cloudflare R2, Backblaze B2, Amazon S3, any
  S3-compatible service, or any rclone remote (Google Drive, Dropbox, OneDrive, SFTP, WebDAV…).
  No provider is assumed by default.
- Different user names on different Macs: the storage remembers the first Mac's home folder, and
  project folders (and project paths in the prompt history) are mapped between home folders.
- `sessync hooks install | remove`; the hook calls the installed command (Homebrew or
  `~/.local/bin`). `sessync setup` offers to add them.
- `sessync setup` and `sessync export` are tested in a terminal, for every kind of storage.
- `SESSYNC_PYTHON` picks the Python interpreter.
- Removed: the migration from 1.3 (rclone bisync).

## 1.4.1 — 2026-10-10

- A new base also removes tails that are off the chain.
- An attachment changed the same way on two Macs is not a conflict.
- Sessions verified after the move to 1.4 are checked by size and time from then on.
- Temporary files of interrupted runs are cleaned up after an hour.

## 1.4.0 — 2026-10-10

- Own sync engine instead of rclone bisync: a transcript is a base plus appended tails, so only new
  lines travel, Litestream-style. Tails are folded when a session goes quiet; a new base is sent when
  the tails grow as big as the base.
- Storage format 2 (in the check file); older versions stop on it.

## 1.3.0 and earlier

- rclone bisync with crypt, the trash, the mass-deletion stop (`deletes --undo/--apply`), retention
  fix, prompt-history merge, search, status line, `export` to the Passwords app.
