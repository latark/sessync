# sessync

[![tests](https://github.com/latark/sessync/actions/workflows/test.yml/badge.svg)](https://github.com/latark/sessync/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![macOS](https://img.shields.io/badge/platform-macOS-lightgrey.svg)

**Your Claude Code sessions on every Mac.** Start a conversation on one Mac, continue it on
another (`claude -r`), and find any past conversation (`sessync find`). Sessions live in storage you
choose — Cloudflare R2, Backblaze B2, Amazon S3, any S3-compatible service, or anything
[rclone](https://rclone.org) can reach — and are encrypted on your Mac before they leave it.

[Русская версия](README.ru.md)

![sessync: a session from the MacBook Air found and continued on the Mac mini](docs/demo.gif)

Claude Code keeps sessions in `~/.claude/projects/<project>/<id>.jsonl` only on the machine where
they ran, and deletes them after 30 days by default. It has no sync between computers.

## What you get

- **Automatic.** sessync syncs when `claude` starts and exits, and every 10 minutes while at least
  one `claude` is open. When the last one closes, nothing is left running: no launchd services, no
  login items.
- **Resume on any Mac.** `claude -r <id>` and `/resume` see the sessions started on your other Macs.
- **Only new lines travel.** A session file is only ever appended to, so sessync ships it the way
  [Litestream](https://litestream.io/how-it-works/) ships a database log. If you're working in a
  session of hundreds of megabytes, each sync sends kilobytes, not the whole file.
- **Nothing gets lost.** A session continued on two Macs between syncs splits the way Claude Code's
  own `/branch` does: one branch keeps the session, the other becomes a session of its own,
  "<title> (Branch)", and both are in `/resume`. A session open in `claude` is never changed under
  it, and `claude -r` warns you when the copy it opened is older than on another Mac. A truncated or
  damaged session file never replaces a full one. Deleted sessions stay in the trash for 30 days. If
  more than half of the files are about to be deleted at once, sync stops and waits for you to
  decide.
- **Search.** `sessync find words` finds the sessions where something was discussed and prints the
  command to continue each one.
- **Forget a conversation.** `sessync forget` deletes a session on every Mac and in the storage,
  together with its prompts in the history; `sessync restore` can bring it back within 30 days.
- **Sessions no longer expire after 30 days.** sessync raises Claude Code's retention
  (`cleanupPeriodDays`) to 100 years and brings back from the storage the sessions Claude Code has
  already deleted.
- **Shared prompt history** (the ↑ key) across your Macs.
- **Different user names on different Macs are fine:** project folders are renamed to match each
  Mac's home folder.

## How it compares

| | sessync | [claude-sync](https://github.com/tawanorg/claude-sync) | [claude-code-sync](https://github.com/perfectra1n/claude-code-sync) | Syncthing / Dropbox |
|---|---|---|---|---|
| What is sent | only appended lines | whole files, compressed | whole files, through git | whole files |
| Sessions over 100 MB | yes | can't be downloaded back | via Git LFS | yes |
| A truncated copy can overwrite the full one | no, it's a conflict | yes | — | yes |
| A session continued on two Macs | two sessions, like `/branch`; both in `/resume` | `.conflict` copy | merged into one file | conflict copies |
| Prompt history | merged line by line | last writer wins | optional | conflict copies |
| Deletions | 30-day trash, stop on mass deletion, open sessions left alone; `sessync forget` deletes a session everywhere, with its prompts | deleted at once | mirrored with guards | deleted at once |
| Encryption | contents and file names | contents | none (encrypt the repo yourself) | on the provider's side |
| Runs | automatically (Claude Code hooks) | by hand: `push` / `pull` | by hand or from cron | automatically |
| Search | `sessync find` | — | — | — |
| Platforms | macOS (Linux next) | macOS, Linux, Windows | macOS, Linux, Windows | all |

## Requirements

- macOS, [Homebrew](https://brew.sh) and the Claude Code CLI.
- Storage: a private bucket or any rclone remote. Cloudflare R2's free tier (10 GB, free downloads)
  is enough for years of sessions.

## Install

```bash
brew install latark/tap/sessync
```

Or from git (update with `git pull` in that folder):

```bash
git clone https://github.com/latark/sessync.git ~/projects/sessync
~/projects/sessync/install.sh
```

`install.sh` installs rclone, ripgrep and jq, links the `sessync` command into `~/.local/bin`, adds
two hooks to `~/.claude/settings.json` and raises the session retention (`cleanupPeriodDays`). The
previous `settings.json` is kept next to it as `settings.json.bak-sessync-…`. Don't move the folder
afterwards: the command is a link into it.

Then, in a regular terminal (not with `!` inside Claude: what you type must not end up in the
transcript):

```bash
sessync setup     # storage, keys, encryption password, Claude Code hooks
sessync           # first sync
sessync export    # save the keys and the password to the Passwords app, step by step
```

**Storage.** `sessync setup` offers Cloudflare R2, Backblaze B2, Amazon S3, any S3-compatible
service (by its endpoint), or any rclone remote — Google Drive, Dropbox, OneDrive, SFTP,
WebDAV/Nextcloud, a NAS. Set up an rclone remote with `rclone config` first. Make the bucket
**private** and used only for sessions, with an access key limited to that bucket: your account's
main key opens all your buckets.

**Without the encryption password, the data can't be read.** Lose it, and the storage stays
unreadable forever. On the first Mac, press Enter when asked for the password, and sessync creates
a random one. Then run `sessync export`: it opens the Passwords app and walks you through saving the
keys and the password. They go one by one into the clipboard of this Mac only — no Universal
Clipboard, marked as concealed, so Raycast, Alfred, Maccy and Paste don't keep them — and sessync
clears the clipboard at the end. The Passwords entry reaches your other devices through iCloud.

### Your other Macs

1. Install sessync the same way.
2. `sessync setup` — the same storage, the same keys and **the same password** (take them from the
   Passwords entry). With another password sessync refuses to work and writes nothing to the
   storage.
3. `sessync` — downloads all sessions.

## Usage

Usually there is nothing to do: it all happens by itself. To switch Macs, run `sessync` on the first
one (or just close `claude`), then start `claude` on the second — the sessions from the first show
up in `/resume`. If you forget, `claude -r` on the second Mac tells you when the copy it opened is
older and how to get the latest one. Or find the session you need:

```bash
sessync find lease contract     # sessions where the conversation has all these words
sessync find "exact phrase"
sessync find -p my-site deploy  # only projects with my-site in the path
sessync find                    # latest sessions in all projects
```

Each result shows the date, the folder, the title, a matching snippet and a ready command such as
`cd ~/projects/my-site && claude -r 3bb6f76a-…`. The search covers only the conversation itself —
your messages and Claude's answers. Injected service text and tool output are searched with
`--raw`. The output passes a mask: what looks like a key (`sk-…`, `ghp_…`, `github_pat_…`, `AKIA…`, long
base64 or hex, a value after `password=`, `token=`, `secret=`) is shown as `***`; `--no-mask` turns it off.

| Command | What it does |
|---|---|
| `sessync` | sync now |
| `sessync status` | what was synced and when, errors, conflicts; with `--remote`, the size in the storage |
| `sessync find …` | search all sessions |
| `sessync off` / `on` | turn automatic sync off / on |
| `sessync conflicts` | sessions that went two ways on two Macs, and copies of other files; `--delete` deletes the copies |
| `sessync trash` | deleted sessions in the storage trash |
| `sessync restore <id>` | bring a session back from the trash; a forgotten one — with its prompts |
| `sessync forget <id>` | delete a session here, in the storage and on your other Macs, with its prompts; `restore` can bring it back within 30 days, `--now` deletes it for good at once; `! sessync forget` inside a session deletes that session once you leave it |
| `sessync deletes` | what was deleted when sync stopped on a mass deletion; `--undo` brings it back, `--apply` deletes everywhere |
| `sessync setup` | storage (`--storage` to change it), keys, password |
| `sessync export` | save the keys and the password to Passwords |
| `sessync hooks install` / `remove` | add / remove the Claude Code hooks |
| `sessync log` | last lines of the log |

The interface is in Russian when your system language is Russian, and in English otherwise. To
choose, put `LANGUAGE=en` or `LANGUAGE=ru` into `~/.config/sessync/config`.

## Status line

If you have your own `statusLine` in `settings.json`, add `$(sessync statusline)` to its output.
If not, you can show just the sync state:

```json
"statusLine": { "type": "command", "command": "sessync statusline" }
```

What it shows:

- `sessions ↻ 13:26` (dim) — all good, 13:26 is the time of the last sync. Yellow if there was no
  sync for over 30 minutes.
- `sessions: <what happened>` (yellow) — an error.
- `sessions: conflict N` (red) — N copies of files that changed on two Macs (`sessync conflicts`).
- `sessions: sync off` — automatic sync is off.

Errors, new copies and sessions that went two ways also show up once as a macOS notification.

## Tell Claude about it

Add this to `~/.claude/CLAUDE.md`, so Claude in any project knows the archive is there:

```markdown
## Session history across Macs
- sessync syncs the sessions of all my Macs (encrypted storage). Find past conversations from any
  project with `sessync find word …` (`--raw` also searches tool output); continue one with
  `cd <folder> && claude -r <id>`.
- Never put session files (`~/.claude/projects/**/*.jsonl`) or `~/.claude/history.jsonl` into git
  or publish them: they hold whole conversations, including files with keys that were read.
- The storage keys and the encryption password are in the Keychain: never print their values into
  the chat or logs. They are entered only with `sessync setup` in my own terminal.
- To delete a session (here, in the storage, on all my Macs, with its prompts): `sessync forget <id>`;
  `sessync restore <id>` can bring it back within 30 days, and `--now` deletes it for good at once.
  To delete this very session, I type `! sessync forget` in it, and it goes once I leave it.
```

## Different user names

Claude Code names a project folder after the project's full path (`/Users/ann/work/site` →
`-Users-ann-work-site`). The storage remembers the home folder of the first Mac. On a Mac with
another home folder (`/Users/bob`), sessync renames project folders on the way in and out, and fixes
project paths in the prompt history the same way. The conversation itself stays as it was: old
messages mention the other path, which doesn't get in the way of resuming. Projects outside the home
folder need the same path on all Macs. Very long paths, which Claude Code shortens with a hash,
can't be renamed.

## Trash

A session deleted on any Mac is first copied into `trash/<date>/` in the same storage — without a
download, and encrypted like everything else — and only then deleted. After 30 days the copy is
deleted too (`TRASH_DAYS=…` in the config sets the period).

```bash
sessync trash               # what is in the trash and when it was deleted
sessync restore 3bb6f76a    # bring a session back with its attachments (id or part of the path)
sessync                     # the session returns to the storage and your other Macs
```

Sessions deleted with `sessync forget` are in the trash too, marked as forgotten; `sessync restore`
brings back their prompts as well.

Old versions of sessions are not kept separately: a session file is only appended to, so each new
version contains the previous one whole.

## Browsing and deleting sessions

- `claude --resume`: `Ctrl+A` — all projects, Space — preview, `Ctrl+R` — rename.
- [Agent Sessions](https://github.com/jazzyalex/agent-sessions) — a macOS app for reading old
  conversations in full. It doesn't change the sessions. On the first screen, turn off "Enable Quota
  Meter" if you don't need usage limits (otherwise the app reads Claude Code's login token from the
  Keychain). And don't enable its tmux probe: it starts `claude` in its own folder, and sessync would
  carry those service sessions to all your Macs.
- Delete a whole project: `claude purge <folder>` (a Claude Code command). It is deleted on your
  other Macs too, and a copy stays in the trash for 30 days.
- Delete one session everywhere — say, one opened for a single quick question: `sessync forget <id>`
  (the id is in `sessync find`; its first 8 characters will do if no other session starts with
  them). It asks first; `-y` skips the question. From inside the session itself type
  `! sessync forget`: it goes once you leave it (`/exit`), and until then it is not synced;
  `sessync forget --cancel <id>` changes your mind. What goes:
  - the conversation and its folder (subagents, attachments), here and in the storage;
  - its prompts in the prompt history (↑), with the text pasted into them, also in this Mac's
    history file in the storage;
  - what Claude Code keeps of it outside the projects folder: files and folders named after its id
    (such as `session-env`, `file-history`, `todos`, `debug`, `security`) and its temporary folder
    `/tmp/claude-<uid>/<project>/<id>`;
  - its text in the search index.

  Your other Macs do the same with their next sync (a session open there goes once it closes). The
  conversation and its prompts wait in the storage trash for 30 days (`TRASH_DAYS`):
  `sessync restore <id>` brings them back on any Mac, and then the session returns to the other Macs
  too; after 30 days they are gone for good. Bring a forgotten session back only with
  `sessync restore`: its mark stays in the storage for good, even after the trash is cleaned, and
  while it is there every Mac deletes a file of that session wherever it appears, without the trash
  — a copy put back by hand or from Time Machine is gone with the next sync. Claude Code's own files
  of it (`/rewind` checkpoints, the temporary folder) don't come back. For a conversation that must
  leave no copy at all: `sessync forget <id> --now` — no trash, and its earlier copies there go too.

  What stays: the project's `memory/` (each Mac has its own), its branches "… (Branch)" — separate
  sessions, forget them by their own ids — what reached Anthropic during the conversation, copies
  in your backups (Time Machine), and old versions at the provider if your bucket keeps them
  (versioning; Backblaze B2 keeps all versions unless its lifecycle is "keep only the last
  version"). Its prompts in the history file of a Mac that no longer syncs stay in the storage,
  encrypted: only that Mac rewrites its file, and no Mac shows them.
- By hand: delete the `<id>.jsonl` file and the `<id>/` folder next to it (subagents and
  attachments). The deletion reaches your other Macs and the trash keeps a copy for 30 days, but its
  prompts in the history and Claude Code's files of it stay.

## Security

In short: contents and file names are encrypted on your Mac with rclone crypt; the storage sees only
encrypted objects, their sizes and upload times. Keys and the password live in the Keychain.
Everything sessync writes to disk is readable by your user only. Details and the threat model are
in [SECURITY.md](SECURITY.md).

## Limitations

- macOS only for now. Linux is next.
- Sync is not instant: when a session starts and ends, and every 10 minutes. Run `sessync` before
  switching to another Mac.
- Continuing one session on two Macs at the same time splits it into two sessions. Nothing is lost,
  but the conversation ends up in two places.
- A deletion on one Mac reaches the others (and the trash keeps a copy for 30 days; after
  `sessync forget --now` there is no copy).
- Only sessions and prompt history are synced. Settings, plugins, skills, `/rewind` checkpoints and
  `memory/` folders are not — keep those in a git repository of your `~/.claude`.
- sessync relies on Claude Code only appending to session files. If that changes, sessync has to
  change too.

## Troubleshooting

| In the status line or `sessync status` | What to do |
|---|---|
| `not set up (sessync setup)` | run `sessync setup` in a terminal |
| `no access to the storage …` | check the network, the bucket name, the keys (`sessync setup`) and your balance with the provider |
| `the encryption password is not the one used on your other Macs` | run `sessync setup` and enter the password from Passwords |
| `settings.json can't be read` | `~/.claude/settings.json` has an error; `jq . ~/.claude/settings.json` shows where |
| `did not finish (code …)` | usually the network; sessync retries by itself, details in `sessync log` |
| `stopped: too many deletions` | `sessync deletes` shows what was deleted and where; by mistake — `--undo`, on purpose — `--apply` |
| `the storage is in a newer format` | another Mac runs a newer sessync: update this one (3.0 raised the format, so every Mac needs 3.0 or newer) |
| `sessions ↻ …` in yellow | no sync for a while: check `sessync status`, maybe there's no network |

The log is `~/.local/state/sessync/sync.log`.

## Uninstall

From the repository folder:

```bash
./uninstall.sh            # removes the hooks and the command; keys, password and data stay
./uninstall.sh --purge    # also the keys and password in the Keychain, local state, search index
```

With Homebrew: `$(brew --prefix sessync)/libexec/uninstall.sh` (add `--purge` if you like), then
`brew uninstall sessync`.

The data in the storage stays — delete the bucket in your provider's console if you don't need it.
Session retention (`cleanupPeriodDays`) stays at 100 years too; set it back to 30 if you want old
sessions deleted again.

## How it works

- Everything in the storage is encrypted with `rclone crypt`: contents and names. `lib/engine.py`
  (python3, no dependencies) does the sync on top of rclone. `memory/` folders are not synced.
- **Session files** (`*.jsonl`) are only appended to, so they travel as a log. In the storage a
  session is a **base** (a full copy at some point) and **tails** — objects with the lines appended
  after it. Only new whole lines are sent; another Mac downloads only the new tails and appends them
  to its file, or reads the slice it needs from the middle of the base (crypt can be read from any
  offset).

  ```
  Mac A    session.jsonl   ████████████████████████▒▒░
  storage                  [ base 9.4 MB           ][tail][tail]   ← each sync adds one tail
  Mac B    session.jsonl   ████████████████████████▒   + the new tail → the same bytes
  ```
- **Compaction** is done by the Mac that appended last. If a session hasn't changed for an hour, its
  tails are folded into one. Once there are 48 tails, they are folded even while the session runs.
  When the tails grow as big as the base, the whole session is sent as a new base. Each base is at
  least twice the previous one, so over its lifetime a session uploads at most twice its size.
- **Attachments** (tool output, images, conflict copies) are written once and travel whole. Symlinks
  (subagents) travel as links.
- **A session continued on two Macs** between syncs went two ways from some line on. The common
  start stays; one continuation keeps the session — the one with the conversation, then the one
  decided on before, then the longer one — and the other becomes a separate session, the way
  `/branch` makes one: the same lines with a new session id and the title "<title> (Branch)". Its
  id comes from the session and the line where the versions part, so every Mac makes the same
  branch, and a Mac whose branch is already a separate session appends its later lines to it. A
  continuation of only Claude Code's service lines (the session was opened and closed) is dropped.
  Why not merge both into one file, as some tools do: Claude Code keeps branches in one file too
  (after `/rewind`), but shows only one of them — the other could not be opened from `/resume` or
  with `claude -r`. Other files that changed on two Macs keep the bigger version under the name and
  the other as `<name>.conflict1` next to it; a session file that got shorter is a conflict too.
- **Open sessions.** Claude Code reads a session's file when it opens it, and from then on only
  appends to it. So sessync never changes the file of a session open in `claude` on this Mac (it
  knows them from its own hooks and from Claude Code's registry, `~/.claude/sessions/`): lines from
  another Mac wait until it closes. On `claude -r`, the start hook checks that one session against
  the storage — about a second, which Claude's first answer waits for — and warns if it got lines
  on another Mac that this window doesn't have; the whole sync goes on in the background.
  `RESUME_CHECK=off` in the config turns the check off.
- **Deletions.** What is deleted on this Mac is copied to the trash first. A session deleted on
  another Mac but open here is deleted here after it closes.
- **Forgetting** (`sessync forget`). Once the session is closed, the Mac puts a mark with its id into
  the storage and moves the session and its prompts to the trash; every Mac that meets the mark
  deletes its own copy and takes its prompts out of the history, and doesn't send or receive that
  session while the mark is there; the mark stays for good. `sessync restore` takes it away. With
  `--now` it is the one deletion without the trash.
- **Prompt history.** Each Mac has its own file in the storage (Mac name plus the start of its
  hardware UUID) and only that Mac writes it, appending its new prompts; the others only receive it.
  On each Mac all these files are merged line by line, without repeats. When `sessync forget` takes
  a session's prompts out of a Mac's file, that Mac sends the file whole, and the others take the
  new version.
- **Format.** The storage format is recorded in a check file. When the format is newer than the
  installed version knows, that version stops instead of writing the old way. Format 3 came with
  `sessync forget`: a 2.x version would bring forgotten prompts back, so it stops.
- **Files on this Mac:** state in `~/.local/state/sessync/` (log, status-line marks, engine state,
  sessions waiting to be forgotten), settings in `~/.config/sessync/config`, search index in
  `~/.cache/sessync/find`.
- **Tests** — `tests/run.sh`: several "Macs" and a local folder instead of the storage; they don't
  touch the Keychain, the clipboard or the real `~/.claude`.

---

sessync is an independent project, not affiliated with or endorsed by Anthropic. "Claude" and
"Claude Code" are trademarks of Anthropic. MIT license.
