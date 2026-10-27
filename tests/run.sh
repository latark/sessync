#!/bin/bash
# Checks sessync, its search, install.sh and uninstall.sh without a real storage: the "storage" is a
# local folder, each "Mac" is a set of folders instead of ~/.claude, ~/.config and ~/.local/state,
# secrets come from the environment. Doesn't touch the Keychain, the real ~/.claude or ~/.local/bin.
# Needs rclone, jq, python3. Most checks read the Russian interface (SESSYNC_LANG=ru), one section
# the English one.
#   tests/run.sh          (KEEP=1 — keep the temporary folder, for debugging)
set -u
here=$(cd "$(dirname "$0")/.." && pwd -P)
T=$(mktemp -d "${TMPDIR:-/tmp}/sessync-test.XXXXXX")
[ -n "${KEEP:-}" ] || trap 'rm -rf "$T"' EXIT; echo "T=$T"
export SESSYNC_TEST_REMOTE="$T/s3" SESSYNC_LANG=ru
export SESSYNC_TEST_S3_ACCESS_KEY=test SESSYNC_TEST_S3_SECRET_KEY=test
export SESSYNC_TEST_PASSPHRASE=correct-horse-battery-staple
export SESSYNC_CLAUDE_TMP="$T/claude-tmp"   # sessync forget: Claude Code's temporary folders
unset CLAUDE_CODE_SESSION_ID                 # run from inside Claude Code: not its session
# rclone storage: tests must not read the real ~/.config/rclone/rclone.conf
export RCLONE_CONFIG="$T/rclone-user.conf"; : > "$RCLONE_CONFIG"
mkdir -p "$T/s3/bucket"
fails=0

check() {  # check "description" command… — ok if the command succeeds
  local name=$1; shift
  if "$@" >/dev/null 2>&1; then echo "ok    $name"; else echo "FAIL  $name"; fails=$((fails + 1)); fi
}
not() { ! "$@"; }
setconf() { mkdir -p "$T/$1/config/sessync"; printf 'BUCKET=%s\n' "$2" > "$T/$1/config/sessync/config"; }
mac() {  # mac A — a "Mac" with default settings
  mkdir -p "$T/$1/projects"
  printf '{"cleanupPeriodDays": 36500}\n' > "$T/$1/settings.json"
  setconf "$1" bucket
}
ss() {  # ss A command… — sessync on "Mac" A
  local m=$1; shift
  CLAUDE_CONFIG_DIR="$T/$m" XDG_CONFIG_HOME="$T/$m/config" XDG_STATE_HOME="$T/$m/state" \
    XDG_CACHE_HOME="$T/$m/cache" SESSYNC_HOST="host$m" "$here/bin/sessync" "$@"
}
run() { ss "$@"; }
has() { local m=$1 pat=$2; shift 2; ss "$m" "$@" 2>&1 | grep -q -- "$pat"; }   # output contains
stf() { echo "$T/$1/state/sessync/$2"; }                                           # state file
at() {  # at A "commands" — with A's storage env (rclone remotes ss:, ssk:)
  CLAUDE_CONFIG_DIR="$T/$1" XDG_CONFIG_HOME="$T/$1/config" XDG_STATE_HOME="$T/$1/state" \
    bash -c "source '$here/bin/sessync' version >/dev/null; load_env && $2"
}
say() {  # say A project id text — append a conversation line to a session
  local f="$T/$1/projects/$2/$3.jsonl"
  mkdir -p "$(dirname "$f")"
  printf '{"type":"user","message":{"role":"user","content":"%s"},"cwd":"/tmp/%s","timestamp":"2026-10-10T10:00:%02dZ","sessionId":"%s"}\n' \
    "$4" "$2" "$((RANDOM % 60))" "$3" >> "$f"
}
lines() { wc -l < "$1" | tr -d ' '; }
contains() { grep -qF -- "$2" "$1"; }
absent() { ! grep -rqF -- "$2" "$1"; }

echo "== Первый Mac загружает, второй получает"
mac A; mac B
for i in 1 2 3 4; do say A -tmp-p1 "s$i" "привет из A номер $i"; done
say A -tmp-p1 s5 "уникальнаяфраза для поиска"
mkdir -p "$T/A/projects/-tmp-p1/memory" && echo "память A" > "$T/A/projects/-tmp-p1/memory/MEMORY.md"
mkdir -p "$T/A/projects/-tmp-p1/s1/tool-results"   # вложения сессий
for i in 1 2 3; do echo "вывод команды $i" > "$T/A/projects/-tmp-p1/s1/tool-results/r$i.txt"; done
echo '{"display":"промпт A","timestamp":1}' > "$T/A/history.jsonl"
echo '{"display":"промпт B","timestamp":2}' > "$T/B/history.jsonl"
check "A: первая синхронизация" run A now
check "в хранилище нет открытого текста" absent "$T/s3" "привет из A"
check "в хранилище нет открытых имён" absent "$T/s3" "-tmp-p1"
check "B: первая синхронизация" run B now
check "B получил сессии" test -f "$T/B/projects/-tmp-p1/s4.jsonl"
check "memory/ не синхронизируется" test ! -e "$T/B/projects/-tmp-p1/memory"
check "B получил вложения сессий" test -f "$T/B/projects/-tmp-p1/s1/tool-results/r3.txt"
check "B: история промптов объединена" contains "$T/B/history.jsonl" "промпт A"
run A now >/dev/null
check "A: история промптов объединена" contains "$T/A/history.jsonl" "промпт B"

echo "== Дописанное на одном Mac приходит на другой"
say B -tmp-p1 s1 "ответ с B"
check "B: отправка" run B now
check "A: получение" run A now
check "A: в s1 две строки" test "$(lines "$T/A/projects/-tmp-p1/s1.jsonl")" = 2
check "без изменений ничего не передаётся" has A 'отправлено файлов 0, получено 0' now

echo "== Одна сессия изменилась на обоих Mac"
say A -tmp-p1 s2 "коротко"
say B -tmp-p1 s2 "длинная версия с другого Mac, она длиннее"
run B now >/dev/null
run A now >/dev/null
check "под основным именем — более длинная версия" contains "$T/A/projects/-tmp-p1/s2.jsonl" "длинная версия"
check "другая версия сохранена копией" contains "$T/A/projects/-tmp-p1/s2.jsonl.conflict1" "коротко"
check "статус показывает конфликт" has A 'Конфликтов: 1' status
check "строка статуса показывает конфликт" has A 'конфликт 1' statusline
check "conflicts --delete убирает копию" run A conflicts --delete
check "копии больше нет" test ! -e "$T/A/projects/-tmp-p1/s2.jsonl.conflict1"

echo "== Защиты"
mac C
before=$(find "$T/s3" -type f | wc -l)
wrongpass() { SESSYNC_TEST_PASSPHRASE=wrong-password-123 ss C now; }
check "C с другим паролем: отказ" not wrongpass
check "C с другим паролем: причина названа" contains "$(stf C error)" "пароль шифрования не тот"
check "C с другим паролем: в хранилище ничего не записано" test "$(find "$T/s3" -type f | wc -l)" = "$before"
check "в контрольном файле — домашняя папка первого Mac" sh -c "$(declare -f at); T='$T' here='$here'; at A 'rclone cat ssk:keycheck' | grep -q '^home /'"
newer() { at A 'printf "cc-sync key check v1\nformat 9\n" | rclone rcat ssk:keycheck 2>/dev/null'; }
check "хранилище в формате новее: отказ" sh -c "$(declare -f at ss newer); T='$T' here='$here'; newer; ! ss A now"
check "хранилище в формате новее: причина названа" contains "$(stf A error)" "новее этой версии"
at A "printf 'cc-sync key check v1\nformat 2\nhome %s\n' \"\$HOME\" | rclone rcat ssk:keycheck 2>/dev/null"
check "после возврата формата — снова работает" run A now

echo "== Срок хранения исправляется сам, удалённое Claude Code возвращается из архива"
printf '{"model": "opus", "cleanupPeriodDays": 30}\n' > "$T/B/settings.json"
rm "$T/B/projects/-tmp-p1/s3.jsonl"   # так Claude Code чистит старые сессии при старте
check "B со сроком 30 дней: синхронизация проходит" run B now
check "срок поднят до 36500, остальное на месте" jq -e '.cleanupPeriodDays == 36500 and .model == "opus"' "$T/B/settings.json"
check "прежний settings.json сохранён" sh -c "ls '$T/B'/settings.json.bak-sessync-* >/dev/null"
check "удалённая сессия вернулась из архива" test -f "$T/B/projects/-tmp-p1/s3.jsonl"
check "в статусе — восстановление" has B 'с восстановлением' status
run A now >/dev/null
check "на другом Mac сессия тоже на месте" test -f "$T/A/projects/-tmp-p1/s3.jsonl"
mkdir -p "$T/D/projects"; setconf D bucket
check "новый Mac без settings.json: синхронизация проходит" run D now
check "новый Mac: срок хранения записан" jq -e '.cleanupPeriodDays == 36500' "$T/D/settings.json"
check "новый Mac: архив скачан" test -f "$T/D/projects/-tmp-p1/s3.jsonl"
mkdir -p "$T/E/projects"; printf '{"cleanupPeriodDays": 36500}\n' > "$T/E/settings.json"
check "E без настроек: отказ" not run E now
check "E: подсказка про setup" contains "$(stf E error)" "sessync setup"
check "выключение" run A off
check "выключено: синхронизация не идёт" has A 'выключена' now
check "выключено: видно в строке статуса" has A 'выкл' statusline
check "включение" run A on
check "строка статуса: время синхронизации" has A '↻' statusline

echo "== Корзина: удалённое хранится TRASH_DAYS и возвращается"
ln -s "$T/A/projects/-tmp-p1/s1.jsonl" "$T/A/projects/-tmp-p1/link.jsonl"   # как ссылки субагентов
run A now >/dev/null; run B now >/dev/null
check "ссылка приходит на другой Mac ссылкой" test -L "$T/B/projects/-tmp-p1/link.jsonl"
rm "$T/A/projects/-tmp-p1/s4.jsonl" "$T/A/projects/-tmp-p1/link.jsonl"
check "A: удаление синхронизируется" run A now
check "удалённая сессия — в корзине" has A 's4.jsonl' trash
run B now >/dev/null
check "на другом Mac она тоже удалена" test ! -e "$T/B/projects/-tmp-p1/s4.jsonl"
check "restore возвращает сессию" run B restore s4.jsonl
check "сессия вернулась целиком" contains "$T/B/projects/-tmp-p1/s4.jsonl" "привет из A номер 4"
check "restore возвращает ссылку ссылкой" sh -c "$(declare -f ss); T='$T' here='$here'; ss B restore link.jsonl && test -L '$T/B/projects/-tmp-p1/link.jsonl'"
check "restore: несуществующего нет" not run B restore нетакого
run B now >/dev/null; run A now >/dev/null
check "вернувшаяся сессия снова на другом Mac" test -f "$T/A/projects/-tmp-p1/s4.jsonl"
say A -tmp-p1 s7 "давно удалённая"
run A now >/dev/null
rm "$T/A/projects/-tmp-p1/s7.jsonl"
past() { SESSYNC_TEST_TODAY=2020-01-01 ss A now; }
check "удаление «в прошлом»" past
check "старая копия в корзине есть" has A 's7.jsonl' trash
run A now >/dev/null   # новый день — очистка корзины
check "копия старше TRASH_DAYS удалена" not has A 's7.jsonl' trash
check "свежая копия осталась" has A 's4.jsonl' trash

echo "== Массовое удаление: синхронизация ждёт решения пользователя"
mkdir -p "$T/s3/bucket2"
for m in F G; do mac $m; setconf $m bucket2; done
for i in 1 2 3 4; do say F -tmp-p2 "d$i" "сессия $i"; done
run F now >/dev/null; run G now >/dev/null
delmany() { rm "$T/F/projects/-tmp-p2/d1.jsonl" "$T/F/projects/-tmp-p2/d2.jsonl" "$T/F/projects/-tmp-p2/d3.jsonl"; }
delmany
check "удалено больше половины: синхронизация остановлена" not run F now
check "в ошибке — подсказка sessync deletes" contains "$(stf F error)" "sessync deletes"
check "deletes показывает, что удалено" has F 'd1.jsonl' deletes
check "deletes --undo" run F deletes --undo
check "удалённое вернулось из хранилища" test -f "$T/F/projects/-tmp-p2/d1.jsonl"
check "ошибки больше нет" test ! -e "$(stf F error)"
delmany
run F now >/dev/null
check "deletes --apply" run F deletes --apply
check "удалённое — в корзине" has F 'd3.jsonl' trash
check "другой Mac тоже спрашивает" not run G now
check "и называет, где удалили" has G 'на другом Mac' deletes
check "другой Mac: deletes --apply" run G deletes --apply
check "на другом Mac удалено" test ! -e "$T/G/projects/-tmp-p2/d1.jsonl" -a -f "$T/G/projects/-tmp-p2/d4.jsonl"
check "deletes без остановки: подтверждать нечего" has G 'нечего' deletes

echo "== Отправляется и получается только дописанное"
mkdir -p "$T/s3/bucket4"
for m in P Q R; do mac $m; setconf $m bucket4; done
segs() { at "$1" "rclone lsf -R --files-only --format sp ss:seg/${2:-projects}" | sort; }
nsegs() { segs "$@" | grep -c .; }
same() { cmp -s "$T/$1/projects/$3" "$T/$2/projects/$3"; }
uplog() { grep 'готово' "$(stf "$1" sync.log)" | tail -1; }
L=-tmp-p4/long.jsonl
pad=$(printf '%0300d' 0)
for i in $(seq 1 200); do say P -tmp-p4 long "строка $i $pad"; done
run P now >/dev/null; run Q now >/dev/null
check "Q получил сессию байт в байт" same P Q $L
check "в хранилище пока только основа, без хвостов" test "$(nsegs P)" = 0
ino=$(stat -f %i "$T/Q/projects/$L")
say P -tmp-p4 long "дописано на P"
run P now >/dev/null
check "дописанное ушло одним хвостом" test "$(nsegs P)" = 1
check "в хвосте — только дописанная строка" test "$(segs P | cut -d';' -f1)" = "$(tail -n1 "$T/P/projects/$L" | wc -c | tr -d ' ')"
check "в журнале — килобайты, а не вся сессия" sh -c "printf '%s' \"\$1\" | grep -q 'отправлено 1 (1 КБ), получено 0 (0 КБ)'" _ "$(uplog P)"
run Q now >/dev/null
check "Q дописал хвост к своему файлу" same P Q $L
check "Q: тот же файл, а не замена целиком" test "$(stat -f %i "$T/Q/projects/$L")" = "$ino"
check "без изменений: ничего не передаётся" has Q 'отправлено файлов 0, получено 0' now

for i in 1 2 3; do say P -tmp-p4 long "ещё $i"; run P now >/dev/null; done
check "хвостов уровня 0 — четыре" test "$(segs P | grep -c -- '-0-')" = 4
run P now >/dev/null
check "не затихла — не сворачивается" test "$(nsegs P)" = 4
export SESSYNC_QUIET=0
run Q now >/dev/null
check "свёртку делает только Mac, дописавший последним" test "$(nsegs Q)" = 4
run P now >/dev/null
check "затихшая сессия свёрнута в один хвост уровня 1" sh -c "test \"\$(printf '%s\n' \"\$1\" | grep -c .)\" = 1 && printf '%s' \"\$1\" | grep -q -- '-1-'" _ "$(segs P)"
unset SESSYNC_QUIET
run Q now >/dev/null
check "после свёртки у Q тот же файл" same P Q $L
export SESSYNC_MAX_L0=3
for i in 1 2 3; do say P -tmp-p4 long "активно $i"; run P now >/dev/null; done
unset SESSYNC_MAX_L0
check "много хвостов в активной сессии — сворачиваются сразу" test "$(segs P | grep -c -- '-0-')" = 0 -a "$(nsegs P)" = 2
run R now >/dev/null
check "новый Mac собрал сессию из основы и хвостов" same P R $L
run Q now >/dev/null
check "и Q получил то же" same P Q $L
size0=$(at P "rclone lsf --format s ss:projects/$L")
# чужой хвост вне цепочки (остаток гонки двух Mac): на файл не влияет, под новой основой убирается
at P "printf x | rclone rcat ss:seg/projects/$L/0000000000000001-0000000000000002-0-000000000000-hostZ 2>/dev/null"
run Q now >/dev/null
check "хвост вне цепочки на файл не влияет" same P Q $L
for i in $(seq 1 220); do say P -tmp-p4 long "много нового $i $pad"; done
run P now >/dev/null
check "хвосты догнали основу — новая основа целиком" test "$(at P "rclone lsf --format s ss:projects/$L")" -gt "$size0"
check "под новой основой убраны все хвосты, и вне цепочки тоже" test "$(nsegs P)" = 0
run Q now >/dev/null; run R now >/dev/null
check "после новой основы у всех тот же файл" sh -c "cmp -s '$T/P/projects/$L' '$T/Q/projects/$L' && cmp -s '$T/P/projects/$L' '$T/R/projects/$L'"

echo "== Недописанная строка, конфликт, открытая сессия, потерянное состояние"
printf '{"type":"user","message":{"role":"user","content":"полстроки' >> "$T/P/projects/$L"
run P now >/dev/null
check "недописанная строка не отправляется" test "$(nsegs P)" = 0
printf ' и конец"},"timestamp":"2026-10-10T11:00:00Z"}\n' >> "$T/P/projects/$L"
run P now >/dev/null; run Q now >/dev/null
check "дописанная до конца — отправляется" same P Q $L
say Q -tmp-p4 long "коротко с Q"
say P -tmp-p4 long "с P длиннее, чем с Q, — эта версия останется основной"
run Q now >/dev/null; run P now >/dev/null; run Q now >/dev/null; run R now >/dev/null
check "конфликт: у всех под основным именем более длинная версия" sh -c "cmp -s '$T/P/projects/$L' '$T/Q/projects/$L' && cmp -s '$T/P/projects/$L' '$T/R/projects/$L'"
check "конфликт: в основной — строка P" contains "$T/Q/projects/$L" "с P длиннее"
check "конфликт: версия Q сохранена копией" contains "$T/Q/projects/$L.conflict1" "коротко с Q"
check "конфликт: копия есть и на P" contains "$T/P/projects/$L.conflict1" "коротко с Q"
check "конфликт: одна копия, не две" test ! -e "$T/Q/projects/$L.conflict2" -a ! -e "$T/P/projects/$L.conflict2"
run P now >/dev/null; run Q now >/dev/null; run P now >/dev/null
check "conflicts --delete на одном Mac" run P conflicts --delete
run P now >/dev/null; run Q now >/dev/null
check "копия конфликта удалена и на другом Mac" test ! -e "$T/Q/projects/$L.conflict1"
say P -tmp-p4 gone "сессия, которую удалят"
run P now >/dev/null; run Q now >/dev/null
mkdir -p "$T/Q/sessions"
printf '{"pid":%s,"sessionId":"gone"}\n' $$ > "$T/Q/sessions/$$.json"
rm "$T/P/projects/-tmp-p4/gone.jsonl"
run P now >/dev/null; run Q now >/dev/null
check "удалена на другом Mac, но открыта здесь — не удаляется" test -f "$T/Q/projects/-tmp-p4/gone.jsonl"
rm "$T/Q/sessions/$$.json"
run Q now >/dev/null
check "закрыли — удаляется" test ! -e "$T/Q/projects/-tmp-p4/gone.jsonl"
rm "$(stf Q engine/projects.json)"
check "состояние потеряно: без конфликтов и повторной отправки" has Q 'отправлено файлов 0, получено 0' now
check "состояние потеряно: копий конфликта нет" test -z "$(find "$T/Q/projects" -name '*.conflict*')"
run Q now >/dev/null
check "сверенное заново дальше сверяется по размеру и времени" python3 -I -c '
import json, sys
f = json.load(open(sys.argv[1]))["files"]
sys.exit(any(v.get("size") == -1 for v in f.values() if "s" in v))' "$(stf Q engine/projects.json)"

echo "== Вложения сессий: целиком"
A1=-tmp-p4/long/tool-results/t1.txt
mkdir -p "$T/P/projects/-tmp-p4/long/tool-results"
echo "вложение" > "$T/P/projects/$A1"
for i in 2 3 4 5; do echo "вложение $i" > "$T/P/projects/-tmp-p4/long/tool-results/t$i.txt"; done
run P now >/dev/null; run Q now >/dev/null
check "вложение пришло" same P Q $A1
echo "вложение, изменённое на P" > "$T/P/projects/$A1"
run P now >/dev/null; run Q now >/dev/null
check "изменённое вложение пришло" contains "$T/Q/projects/$A1" "изменённое на P"
A2=-tmp-p4/long/tool-results/t2.txt
echo "одинаково на обоих" > "$T/P/projects/$A2"; echo "одинаково на обоих" > "$T/Q/projects/$A2"
run P now >/dev/null; run Q now >/dev/null; run P now >/dev/null
check "одинаково изменённое на обоих Mac — не конфликт" test ! -e "$T/Q/projects/$A2.conflict1" -a ! -e "$T/P/projects/$A2.conflict1"
check "и дальше спокойно" has Q 'отправлено файлов 0, получено 0' now
echo old > "$T/P/projects/-tmp-p4/old.sessync.tmp"; touch -t 202601010000 "$T/P/projects/-tmp-p4/old.sessync.tmp"
echo new > "$T/P/projects/-tmp-p4/new.sessync.tmp"
run P now >/dev/null; run Q now >/dev/null
check "старый временный файл прерванного запуска убран" test ! -e "$T/P/projects/-tmp-p4/old.sessync.tmp"
check "свежий не тронут (его может писать restore)" test -e "$T/P/projects/-tmp-p4/new.sessync.tmp"
check "временные файлы не синхронизируются" test ! -e "$T/Q/projects/-tmp-p4/new.sessync.tmp"
rm "$T/P/projects/-tmp-p4/new.sessync.tmp"
echo "версия Q, она больше версии P" > "$T/Q/projects/$A1"
echo "версия P" > "$T/P/projects/$A1"
run Q now >/dev/null; run P now >/dev/null; run P now >/dev/null; run Q now >/dev/null
check "конфликт вложения: у обоих большая версия" sh -c "grep -q 'версия Q' '$T/P/projects/$A1' && grep -q 'версия Q' '$T/Q/projects/$A1'"
check "конфликт вложения: меньшая — копией у обоих" sh -c "grep -q 'версия P' '$T/P/projects/$A1.conflict1' && grep -q 'версия P' '$T/Q/projects/$A1.conflict1'"
rm "$T/P/projects/$A1" "$T/P/projects/$A1.conflict1"
run P now >/dev/null; run Q now >/dev/null
check "удалённое вложение удалено и на другом Mac" test ! -e "$T/Q/projects/$A1" -a ! -e "$T/Q/projects/$A1.conflict1"
check "и лежит в корзине" has P 't1.txt' trash
printf 'обрезано\n' > "$T/Q/projects/$L"   # транскрипт стал короче — порча, а не правка
run Q now >/dev/null; run P now >/dev/null
check "обрезанный транскрипт не затирает полный" test "$(lines "$T/P/projects/$L")" -gt 100
check "обрезанная версия сохранена копией" contains "$T/Q/projects/$L.conflict1" "обрезано"
check "на Q снова полная версия" same P Q $L
run Q conflicts --delete >/dev/null; run Q now >/dev/null; run P now >/dev/null

echo "== Сессия разошлась на двух Mac: другая ветка — отдельная сессия, открытая сессия не меняется"
mac K1; mac K2; mac K3
meta() {  # meta A project id — одни служебные строки Claude Code: сессию открыли и закрыли через /exit
  printf '{"type":"user","message":{"role":"user","content":"<command-name>/exit</command-name>"},"sessionId":"%s"}\n{"type":"last-prompt","lastPrompt":"x","sessionId":"%s"}\n' \
    "$3" "$3" >> "$T/$1/projects/$2/$3.jsonl"
}
others() { ls "$T/$1/projects/$2" | grep '\.jsonl$' | grep -vx "$3.jsonl"; }   # другие сессии проекта
waitrun() { local i; for i in $(seq 1 150); do pgrep -qf 'bin/sessync _run' || return 0; sleep 0.2; done; }
S1=11111111-1111-4111-8111-111111111111
k=$T/K1/projects/-tmp-k
say K1 -tmp-k $S1 "общее начало"
printf '{"type":"ai-title","aiTitle":"Ограничение запросов","sessionId":"%s"}\n' $S1 >> "$k/$S1.jsonl"
run K1 now >/dev/null; run K2 now >/dev/null
say K1 -tmp-k $S1 "ветка K1"
say K2 -tmp-k $S1 "ветка K2, она длиннее ветки K1"
run K1 now >/dev/null; run K2 now >/dev/null
F=$(others K2 -tmp-k $S1)
f=$T/K2/projects/-tmp-k/$F
check "под именем сессии — более длинная ветка" contains "$T/K2/projects/-tmp-k/$S1.jsonl" "ветка K2"
check "другая ветка — отдельная сессия, одна" test -n "$F" -a "$(printf '%s\n' "$F" | grep -c .)" = 1
check "в ней общее начало и ветка K1" sh -c "grep -q 'общее начало' '$f' && grep -q 'ветка K1' '$f'"
check "в ветке её собственный id" sh -c "! grep -q '\"sessionId\":\"$S1\"' '$f' && grep -q '\"sessionId\":\"${F%.jsonl}\"' '$f'"
check "заголовок ветки — «… (Branch)»" contains "$f" '"customTitle":"Ограничение запросов (Branch)"'
check "копий .conflict нет" test -z "$(find "$T/K2/projects/-tmp-k" -name '*.conflict*')"
say K1 -tmp-k $S1 "K1 продолжает свою ветку, не зная о развилке, и пишет много текста"
say K1 -tmp-k $S1 "и ещё одну длинную строку, так что его версия теперь длиннее версии K2"
run K1 now >/dev/null; run K1 now >/dev/null
check "K1: его ветка уже сессия — под именем остаётся версия K2" contains "$k/$S1.jsonl" "ветка K2"
check "K1: в основной нет строк K1 после развилки" not contains "$k/$S1.jsonl" "ветка K1"
check "K1: новые строки K1 дописаны в ветку" contains "$k/$F" "K1 продолжает"
check "K1: ветка по-прежнему одна" test "$(others K1 -tmp-k $S1)" = "$F"
run K2 now >/dev/null; run K3 now >/dev/null
check "у всех одна и та же сессия" sh -c "cmp -s '$k/$S1.jsonl' '$T/K2/projects/-tmp-k/$S1.jsonl' && cmp -s '$k/$S1.jsonl' '$T/K3/projects/-tmp-k/$S1.jsonl'"
check "и одна и та же ветка" sh -c "cmp -s '$k/$F' '$f' && cmp -s '$k/$F' '$T/K3/projects/-tmp-k/$F'"
check "дальше спокойно" has K1 'отправлено файлов 0, получено 0' now
check "conflicts называет ветку и команду" has K2 "claude -r ${F%.jsonl}" conflicts
check "status называет разошедшуюся сессию" has K2 'Последняя разошедшаяся сессия' status

S2=22222222-2222-4222-8222-222222222222
say K1 -tmp-k2 $S2 "начало второй"
run K1 now >/dev/null; run K2 now >/dev/null
say K2 -tmp-k2 $S2 "настоящее продолжение на K2"
meta K1 -tmp-k2 $S2
run K2 now >/dev/null; run K1 now >/dev/null; run K2 now >/dev/null
check "одни служебные строки — не повод для ветки" test -z "$(others K1 -tmp-k2 $S2)"
check "на K1 — переписка с K2" contains "$T/K1/projects/-tmp-k2/$S2.jsonl" "настоящее продолжение"
check "служебные строки K1 убраны" not contains "$T/K1/projects/-tmp-k2/$S2.jsonl" "command-name"
check "K1 и K2 одинаковы" same K1 K2 -tmp-k2/$S2.jsonl

S3=33333333-3333-4333-8333-333333333333
k3=$T/K1/projects/-tmp-k3/$S3.jsonl
say K1 -tmp-k3 $S3 "начало третьей"
run K1 now >/dev/null; run K2 now >/dev/null
mkdir -p "$T/K1/sessions"; printf '{"pid":%s,"sessionId":"%s"}\n' $$ $S3 > "$T/K1/sessions/$$.json"
say K2 -tmp-k3 $S3 "дописано на K2, пока на K1 сессия открыта"
run K2 now >/dev/null; run K1 now >/dev/null
check "открытая здесь сессия не меняется" not contains "$k3" "дописано на K2"
check "о ней сказано: ждёт закрытия" grep -q "^$S3	" "$(stf K1 held)"
rm "$T/K1/sessions/$$.json"; run K1 now >/dev/null
check "закрыли — дописанное пришло" contains "$k3" "дописано на K2"
printf '{"pid":%s,"sessionId":"%s"}\n' $$ $S3 > "$T/K1/sessions/$$.json"
say K1 -tmp-k3 $S3 "K1 пишет в открытой сессии"
say K2 -tmp-k3 $S3 "K2 пишет своё, и это гораздо длиннее того, что пишет K1"
run K2 now >/dev/null; run K1 now >/dev/null
check "открыта, а побеждает другая версия — файл не тронут" sh -c "grep -q 'K1 пишет' '$k3' && ! grep -q 'K2 пишет' '$k3'"
check "и ветки пока нет" test -z "$(others K1 -tmp-k3 $S3)"
rm "$T/K1/sessions/$$.json"; run K1 now >/dev/null
check "закрыли — под именем версия K2" sh -c "grep -q 'K2 пишет' '$k3' && ! grep -q 'K1 пишет' '$k3'"
check "а строки K1 — в отдельной сессии-ветке" sh -c "grep -l 'K1 пишет' '$T/K1/projects/-tmp-k3/'*.jsonl | grep -vq '$S3'"

S4=44444444-4444-4444-8444-444444444444
k4=$T/K1/projects/-tmp-k4/$S4.jsonl
say K1 -tmp-k4 $S4 "начало четвёртой"
run K1 now >/dev/null; run K2 now >/dev/null
say K2 -tmp-k4 $S4 "продолжение на K2"
run K2 now >/dev/null
hook() {  # hook A start|end json — хук Claude Code с его входом; ждёт фоновую синхронизацию
  printf '%s' "$3" | ss "$1" hook "$2" > "$T/hook.out" 2>&1; waitrun
}
resume() { hook "$1" start "{\"session_id\":\"$2\",\"source\":\"${4:-resume}\",\"transcript_path\":\"$T/$1/projects/$3/$2.jsonl\"}"; }
resume K1 $S4 -tmp-k4
check "claude -r старой версии: предупреждение" jq -e --arg s "$S4" '.systemMessage | contains("claude -r " + $s) and contains("hostK2")' "$T/hook.out"
check "и подсказка для Claude" jq -e '.hookSpecificOutput.additionalContext | contains("older")' "$T/hook.out"
check "файл открытой сессии не тронут" not contains "$k4" "продолжение на K2"
run K1 now >/dev/null
check "после выхода — свежая версия" contains "$k4" "продолжение на K2"
resume K1 $S4 -tmp-k4
check "свежая — без предупреждения" test ! -s "$T/hook.out"
resume K1 $S4 -tmp-k4 startup
check "новая сессия — хук молчит" test ! -s "$T/hook.out"
say K1 -tmp-k4 $S4 "дописано на K1 и ещё не отправлено"
resume K1 $S4 -tmp-k4
check "свои неотправленные строки — не повод для предупреждения" test ! -s "$T/hook.out"
run K1 now >/dev/null; run K2 now >/dev/null
pad=$(printf '%0900d' 0)
say K2 -tmp-k4 $S4 "длинная строка K2 уходит новой основой $pad"
printf '{"display":"вопрос с K2","timestamp":1790000000000,"project":"/tmp/-tmp-k4","sessionId":"%s"}\n' $S4 >> "$T/K2/history.jsonl"
run K2 now >/dev/null
check "строки ушли новой основой, без хвостов" test -z "$(at K2 "rclone lsf -R --files-only ss:seg/projects/-tmp-k4" 2>/dev/null)"
resume K1 $S4 -tmp-k4
check "имя другого Mac — из его истории промптов" jq -e '.systemMessage | contains("(hostK2)")' "$T/hook.out"
echo RESUME_CHECK=off >> "$T/K1/config/sessync/config"
resume K1 $S4 -tmp-k4
check "RESUME_CHECK=off: без проверки и ожидания" test ! -s "$T/hook.out"
sed -i '' '/RESUME_CHECK/d' "$T/K1/config/sessync/config"
run K1 now >/dev/null
say K2 -tmp-k4 $S4 "ещё с K2, пока на K1 сессия закрывается"
run K2 now >/dev/null
sleep 3 & spid=$!
printf '{"pid":%s,"sessionId":"%s"}\n' $spid $S4 > "$T/K1/sessions/$spid.json"
printf '{"session_id":"%s"}' $S4 | ss K1 hook end
for i in $(seq 1 75); do grep -q 'пока на K1 сессия закрывается' "$k4" && break; sleep 0.2; done
check "хук конца ждёт выхода claude и получает дописанное" contains "$k4" "пока на K1 сессия закрывается"
wait $spid 2>/dev/null; rm -f "$T/K1/sessions/$spid.json"; waitrun

S6=66666666-6666-4666-8666-666666666666
k6=$T/K1/projects/-tmp-k6/$S6.jsonl
say K1 -tmp-k6 $S6 "начало шестой"
run K1 now >/dev/null; run K2 now >/dev/null
sleep 60 & cpid=$!
SESSYNC_TEST_CLAUDE_PID=$cpid resume K1 $S6 -tmp-k6 startup
check "хук старта записал открытую сессию в свой список" test "$(cat "$(stf K1 open/$S6)" 2>/dev/null)" = "$cpid"
say K2 -tmp-k6 $S6 "с K2, пока сессия открыта на K1"
run K2 now >/dev/null; run K1 now >/dev/null
check "открыта по своему списку sessync (реестра Claude Code нет) — не меняется" not contains "$k6" "пока сессия открыта"
hook K1 end "{\"session_id\":\"$S6\",\"reason\":\"clear\"}"
check "/clear: сессия закрыта, хотя claude работает дальше" test ! -e "$(stf K1 open/$S6)"
check "и дописанное пришло" contains "$k6" "пока сессия открыта"
SESSYNC_TEST_CLAUDE_PID=$cpid resume K1 $S6 -tmp-k6 startup
kill $cpid; wait $cpid 2>/dev/null
say K2 -tmp-k6 $S6 "с K2 после выхода claude"
run K2 now >/dev/null; run K1 now >/dev/null
check "claude вышел — запись в списке не держит сессию" contains "$k6" "после выхода claude"
check "и убрана" test ! -e "$(stf K1 open/$S6)"

S7=77777777-7777-4777-8777-777777777777
k7=$T/K1/projects/-tmp-k7
line7() { printf '{"type": "user", "message": {"role": "user", "content": "%s"}, "sessionId": "%s"}\n' "$3" $S7 >> "$T/$1/projects/-tmp-k7/$S7.jsonl"; }
mkdir -p "$k7" "$T/K2/projects/-tmp-k7"; line7 K1 x "начало седьмой"
run K1 now >/dev/null; run K2 now >/dev/null
line7 K1 x "ветка K1"; line7 K2 x "ветка K2, длиннее"
run K1 now >/dev/null; run K2 now >/dev/null
check "строки не в ожидаемом формате — не ветка, а копия" sh -c "test -z \"\$(ls '$T/K2/projects/-tmp-k7' | grep -vx '$S7.jsonl' | grep -v conflict)\" && grep -q 'ветка K1' '$T/K2/projects/-tmp-k7/$S7.jsonl.conflict1'"
run K2 conflicts --delete >/dev/null; run K2 now >/dev/null; run K1 now >/dev/null

mkdir "$(stf K1 lock)"
(sleep 2; rmdir "$(stf K1 lock)") &
check "sessync в терминале ждёт идущую синхронизацию и синхронизирует" "${SESSYNC_PYTHON:-python3}" -I "$here/tests/tty.py" \
  --expect 'Жду, пока закончится' --expect 'Готово за' -- env CLAUDE_CONFIG_DIR="$T/K1" XDG_CONFIG_HOME="$T/K1/config" \
  XDG_STATE_HOME="$T/K1/state" XDG_CACHE_HOME="$T/K1/cache" SESSYNC_HOST=hostK1 "$here/bin/sessync" now
wait

echo "== Корзина и возврат сессии с хвостами"
say P -tmp-p4 segd "основа"
run P now >/dev/null
for i in $(seq 1 30); do say P -tmp-p4 segd "основа $i $pad"; done
run P now >/dev/null
say P -tmp-p4 segd "хвост 1"; run P now >/dev/null
say P -tmp-p4 segd "хвост 2"; run P now >/dev/null
check "у сессии есть хвосты" test "$(segs P | grep -c segd)" -ge 2
cp "$T/P/projects/-tmp-p4/segd.jsonl" "$T/segd.saved"
rm "$T/P/projects/-tmp-p4/segd.jsonl"
run P now >/dev/null
check "удалённая сессия с хвостами — в корзине" has P 'segd.jsonl' trash
check "хвосты удалённой сессии убраны из хранилища" test "$(segs P | grep -c segd)" = 0
check "restore собирает сессию из основы и хвостов" run P restore segd.jsonl
check "собрана байт в байт" cmp -s "$T/segd.saved" "$T/P/projects/-tmp-p4/segd.jsonl"
run P now >/dev/null; run Q now >/dev/null
check "возвращённая сессия ушла на другой Mac" cmp -s "$T/segd.saved" "$T/Q/projects/-tmp-p4/segd.jsonl"
check "status --remote показывает хвосты" has P 'хвосты' status --remote

echo "== История промптов: свой файл только дописывается"
for i in $(seq 1 40); do echo "{\"display\":\"промпт P $i $pad\",\"timestamp\":$i}" >> "$T/P/history.jsonl"; done
run P now >/dev/null; run Q now >/dev/null
check "Q получил историю P" contains "$T/Q/history.jsonl" "промпт P 40"
echo '{"display":"новый промпт P","timestamp":100}' >> "$T/P/history.jsonl"
run P now >/dev/null
check "ушёл только новый промпт" test "$(segs P history | grep hostP | cut -d';' -f1)" = "$(tail -n1 "$T/P/history.jsonl" | wc -c | tr -d ' ')"
run Q now >/dev/null
check "Q получил новый промпт" contains "$T/Q/history.jsonl" "новый промпт P"
echo '{"display":"промпт Q","timestamp":101}' >> "$T/Q/history.jsonl"
run Q now >/dev/null; run P now >/dev/null
check "P получил промпт Q" contains "$T/P/history.jsonl" "промпт Q"
check "свой промпт не задвоился" test "$(grep -c 'новый промпт P' "$T/P/history.jsonl")" = 1
mv "$(stf P engine/history.json)" "$T/history-state.saved"
mkdir -p "$T/P/state/sessync/history.old"; mv "$T"/P/state/sessync/history/* "$T/P/state/sessync/history.old/"
run P now >/dev/null
check "переустановка: свой файл истории продолжается, без конфликта" test -z "$(find "$T/P/state/sessync/history" -name '*.conflict*')"
check "переустановка: промпты не задвоились" test "$(grep -c 'новый промпт P' "$T/P/history.jsonl")" = 1

echo "== sessync forget: везде и с промптами, TRASH_DAYS — в корзине, restore возвращает; --now — насовсем"
mkdir -p "$T/s3/bucket9"
for m in FA FB; do mac $m; setconf $m bucket9; done
G1=aaaaaaaa-1111-4111-8111-111111111111   # забываем закрытую
G2=aaaaaaaa-2222-4222-8222-222222222222   # остаётся
G3=bbbbbbbb-3333-4333-8333-333333333333   # есть только в корзине
G4=cccccccc-4444-4444-8444-444444444444   # открыта на FA
G5=dddddddd-5555-4555-8555-555555555555   # текущая: ! sessync forget, потом хук конца
G6=eeeeeeee-6666-4666-8666-666666666666   # FB дописал её, ещё не зная
G7=ffffffff-7777-4777-8777-777777777777   # синхронизация выключена
G8=abababab-8888-4888-8888-888888888888   # передумали
G9=cdcdcdcd-9999-4999-8999-999999999999   # выключена и открыта: уходит после выхода
G11=fefefefe-1111-4111-8111-111111111111  # --now
G12=dcdcdcdc-1212-4212-8212-121212121212  # срок корзины истёк
f9=$T/FA/projects/-tmp-f
hl() {  # hl A session prompt [hash] — промпт в истории A (hash — вставленный текст в paste-cache)
  local pasted=""
  [ -n "${4:-}" ] && pasted="\"1\":{\"id\":1,\"type\":\"text\",\"contentHash\":\"$4\"}"
  printf '{"display":"%s","pastedContents":{%s},"timestamp":%s000,"project":"/tmp/f","sessionId":"%s"}\n' \
    "$3" "$pasted" "$(date +%s)" "$2" >> "$T/$1/history.jsonl"
}
inbucket() { at FA "rclone lsf -R --files-only ss:projects; rclone lsf -R --files-only ss:seg/projects" | grep -q "$1"; }
intrash() { at FA "rclone lsf -R --files-only ss:trash" | grep -q "$1"; }
opn() { mkdir -p "$T/$1/sessions"; printf '{"pid":%s,"sessionId":"%s"}\n' $$ "$2" > "$T/$1/sessions/$2.json"; }
cls() { rm -f "$T/$1/sessions/$2.json"; }
say FA -tmp-f $G1 "секретная разовая переписка"
say FA "-tmp-f/$G1/subagents" agent-1 "субагент секретной"
say FA -tmp-f $G2 "обычная переписка"
hl FA $G1 "секретный вопрос" 0123456789abcdef
hl FA $G2 "обычный вопрос"
mkdir -p "$T/FA/paste-cache" "$T/FA/session-env/$G1" "$T/FA/file-history/$G1" "$T/FA/security" "$T/claude-tmp/-tmp-f/$G1"
echo "вставленный секрет" > "$T/FA/paste-cache/0123456789abcdef.txt"
echo v1 > "$T/FA/file-history/$G1/v1"; echo '{}' > "$T/FA/security/security_warnings_state_$G1.json"
echo страница > "$T/claude-tmp/-tmp-f/$G1/page.html"
run FA now >/dev/null; run FB now >/dev/null
check "FB получил сессию и её промпт" sh -c "test -f '$T/FB/projects/-tmp-f/$G1.jsonl' && grep -q 'секретный вопрос' '$T/FB/history.jsonl'"
ss FA find секретная >/dev/null   # индекс поиска с её текстом
check "без id и не в сессии: спрашивает, какую" has FA 'Какую сессию' forget
check "не id: отказ" has FA 'не id сессии' forget 'zz;rm'
check "начало id у двух сессий: просит длиннее" has FA 'несколько сессий' forget aaaaaaaa -y
check "без терминала и без -y: просит подтвердить" has FA "sessync forget $G1 -y" forget $G1
check "и ничего не тронуто" test -f "$f9/$G1.jsonl" -a ! -e "$(stf FA forget/$G1)"
check "forget по началу id" has FA 'Готово: удалена здесь и в хранилище' forget aaaaaaaa-1111 -y
check "здесь: переписка и папка сессии удалены" test ! -e "$f9/$G1.jsonl" -a ! -e "$f9/$G1"
check "здесь: файлы Claude Code о ней удалены" sh -c "test ! -e '$T/FA/session-env/$G1' -a ! -e '$T/FA/file-history/$G1' -a ! -e '$T/FA/security/security_warnings_state_$G1.json' -a ! -e '$T/claude-tmp/-tmp-f/$G1'"
check "здесь: её промпт и вставленный текст удалены, остальное на месте" sh -c "! grep -q 'секретный вопрос' '$T/FA/history.jsonl' && grep -q 'обычный вопрос' '$T/FA/history.jsonl' && test ! -e '$T/FA/paste-cache/0123456789abcdef.txt'"
check "другая сессия на месте" test -f "$f9/$G2.jsonl"
check "поиск её не находит" has FA 'Ничего не найдено' find секретная
check "в индексе поиска её текста нет" absent "$T/FA/cache/sessync/find/text" "секретная"
check "в хранилище её нет" not inbucket $G1
check "она в корзине — её можно вернуть" intrash $G1
check "её промпт — тоже в корзине" sh -c "$(declare -f at); T='$T' here='$here'; at FA 'rclone lsf -R --files-only ss:trash' | grep -q 'history/$G1/hostFA.jsonl'"
check "sessync trash помечает забытую" has FA 'забыта (sessync forget)' trash
check "в хранилище — метка забытой сессии" sh -c "$(declare -f at); T='$T' here='$here'; at FA 'rclone lsf ss:forgotten' | grep -qx $G1"
check "история FA в хранилище — уже без её промпта" sh -c "$(declare -f at); T='$T' here='$here'; ! at FA 'rclone cat ss:history/hostFA.jsonl; rclone cat ss:seg/history/hostFA.jsonl' | grep -q 'секретный вопрос'"
check "запрос выполнен — метка на этом Mac снята" test ! -e "$(stf FA forget/$G1)"
check "рабочая папка движка после запуска убрана (в ней копии переписки)" test ! -e "$(stf FA engine/work-projects)"
check "дальше спокойно" has FA 'отправлено файлов 0, получено 0' now
run FB now >/dev/null
check "FB: сессия удалена" test ! -e "$T/FB/projects/-tmp-f/$G1.jsonl" -a ! -e "$T/FB/projects/-tmp-f/$G1"
check "FB: её промпта в истории нет, остальное есть" sh -c "! grep -q 'секретный вопрос' '$T/FB/history.jsonl' && grep -q 'обычный вопрос' '$T/FB/history.jsonl'"
check "FB: копия истории FA — версия хранилища, без конфликта" sh -c "! grep -q 'секретный вопрос' '$T/FB/state/sessync/history/hostFA.jsonl' && test -z \"\$(find '$T/FB/state/sessync/history' -name '*.conflict*')\""
check "FB: дальше спокойно" has FB 'отправлено файлов 0, получено 0' now
run FA now >/dev/null
check "FA: промпт не вернулся" not contains "$T/FA/history.jsonl" "секретный вопрос"
check "restore возвращает забытую с её промптами" has FA 'Её промпты вернулись в историю: 1' restore $G1
check "переписка и субагент на месте" sh -c "grep -q 'секретная разовая' '$f9/$G1.jsonl' && test -f '$f9/$G1/subagents/agent-1.jsonl'"
check "промпт в истории" contains "$T/FA/history.jsonl" "секретный вопрос"
check "метки в хранилище больше нет" not sh -c "$(declare -f at); T='$T' here='$here'; at FA 'rclone lsf ss:forgotten' | grep -qx $G1"
run FA now >/dev/null; run FB now >/dev/null
check "FB: вернулась и туда" contains "$T/FB/projects/-tmp-f/$G1.jsonl" "секретная разовая"
check "FB: и её промпт" contains "$T/FB/history.jsonl" "секретный вопрос"
check "забыть снова" has FA 'Готово' forget $G1 -y
run FB now >/dev/null
check "FB: снова удалена, и промпт тоже" sh -c "test ! -e '$T/FB/projects/-tmp-f/$G1.jsonl' && ! grep -q 'секретный вопрос' '$T/FB/history.jsonl'"
check "в контрольном файле — формат 3 (версии 2.x останавливаются)" sh -c "$(declare -f at); T='$T' here='$here'; at FA 'rclone cat ssk:keycheck' | grep -qx 'format 3'"

say FA -tmp-f $G3 "удалённая обычным способом"
run FA now >/dev/null; rm "$f9/$G3.jsonl"; run FA now >/dev/null
check "обычное удаление — в корзине" intrash $G3
check "--now сессии, которой здесь нет, — по полному id" has FA 'на этом Mac нет' forget $G3 --now -y
check "--now: её копии в корзине удалены" not intrash $G3

say FA -tmp-f $G11 "насовсем и сразу"
hl FA $G11 "вопрос насовсем"
run FA now >/dev/null
check "--now без id внутри сессии всё равно просит подтвердить" sh -c "$(declare -f ss); T='$T' here='$here'; export CLAUDE_CODE_SESSION_ID=$G11; ss FA forget --now | grep -q 'sessync forget $G11 --now -y'"
check "--now" has FA 'удалена насовсем' forget $G11 --now -y
check "--now: ни в хранилище, ни в корзине, промпта нигде нет" sh -c "$(declare -f at inbucket intrash); T='$T' here='$here'; ! inbucket $G11 && ! intrash $G11 && ! grep -q 'вопрос насовсем' '$T/FA/history.jsonl'"
check "--now: вернуть нечего" not run FA restore $G11

say FA -tmp-f $G12 "срок вышел"
run FA now >/dev/null
SESSYNC_TEST_TODAY=2020-01-01 ss FA forget $G12 -y >/dev/null
check "забытая «в прошлом» — в корзине" intrash $G12
run FA now >/dev/null   # новый день — очистка корзины
check "срок корзины вышел — удалена насовсем" not intrash $G12
check "и вернуть её нельзя" not run FA restore $G12

say FA -tmp-f $G4 "открытая сессия"
run FA now >/dev/null; run FB now >/dev/null
opn FA $G4
check "открытая: удалю, когда выйдете" has FA 'когда вы из неё выйдете' forget $G4 -y
say FA -tmp-f $G4 "дописано после forget"
run FA now >/dev/null; run FB now >/dev/null
check "пока открыта — файл не тронут" contains "$f9/$G4.jsonl" "дописано после forget"
check "и дописанное не отправляется" not contains "$T/FB/projects/-tmp-f/$G4.jsonl" "дописано после forget"
check "status: ждёт удаления" has FA 'Ждут удаления' status
cls FA $G4; run FA now >/dev/null
check "закрыли — удалена" test ! -e "$f9/$G4.jsonl"
run FB now >/dev/null
check "и на FB" test ! -e "$T/FB/projects/-tmp-f/$G4.jsonl"

say FA -tmp-f $G5 "текущая сессия"
hl FA $G5 "вопрос текущей сессии"
run FA now >/dev/null
opn FA $G5
check "без id внутри сессии — сама сессия, после выхода" sh -c "$(declare -f ss); T='$T' here='$here'; export CLAUDE_CODE_SESSION_ID=$G5; ss FA forget | grep -q 'когда вы из неё выйдете'"
cls FA $G5
hook FA end "{\"session_id\":\"$G5\"}"
check "хук конца: удалена здесь" test ! -e "$f9/$G5.jsonl"
check "и её промпт" not contains "$T/FA/history.jsonl" "вопрос текущей сессии"
check "и в хранилище её нет" not inbucket $G5
G10=edededed-1010-4010-8010-101010101010
say FA -tmp-f $G10 "текущая, а переменной с id нет"
sleep 60 & cpid=$!
SESSYNC_TEST_CLAUDE_PID=$cpid resume FA $G10 -tmp-f startup   # хук старта: сессия в списке открытых
check "без id и без CLAUDE_CODE_SESSION_ID — по процессу claude" sh -c "$(declare -f ss); T='$T' here='$here'; SESSYNC_TEST_CLAUDE_PID=$cpid ss FA forget | grep -q 'когда вы из неё выйдете'"
check "и это та самая сессия" test -e "$(stf FA forget/$G10)"
kill $cpid; wait $cpid 2>/dev/null
run FA now >/dev/null
check "claude вышел — удалена" test ! -e "$f9/$G10.jsonl"

# Фоновая сессия (claude --bg): хук идёт под `claude bg-spare`, тот — под `claude bg-pty-host` и общим
# `claude daemon`, который живёт дольше сессий. В список открытых — pid bg-spare, не демона
G11=edededed-1111-4011-8011-111111111111 G12=edededed-1212-4012-8012-121212121212
say FA -tmp-f $G11 "фоновая сессия"
sleep 60 & dpid=$!; sleep 60 & hpid=$!; sleep 60 & spid=$!; sleep 60 & ipid=$!
procs="$T/procs.tsv"
printf '%s\t%s\t%s\t%s\n' \
  $spid $hpid "claude bg-spare" "claude bg-spare --bg-spare /tmp/x.claim.sock" \
  $hpid $dpid "claude bg-pty-host" "claude bg-pty-host --bg-pty-host /tmp/x.pty.sock 200 50" \
  $dpid 1 "/Users/u/.local/bin/claude" "/Users/u/.local/bin/claude daemon run --origin transient" \
  $ipid 1 "/Users/u/.local/bin/claude" "/Users/u/.local/bin/claude --agent hq" > "$procs"
echo $dpid > "$(stf FA open)/$G12"   # запись версии 3.0.1: фоновая сессия под pid демона
SESSYNC_TEST_PROCS=$procs SESSYNC_TEST_PPID=$spid resume FA $G11 -tmp-f startup
check "фоновая сессия: в списке открытых pid bg-spare" test "$(cat "$(stf FA open)/$G11")" = $spid
check "старая запись с pid демона убрана" test ! -e "$(stf FA open)/$G12"
SESSYNC_TEST_PROCS=$procs SESSYNC_TEST_PPID=$hpid resume FA $G12 -tmp-f startup
check "хук прямо под службой фоновых сессий — без записи" test ! -e "$(stf FA open)/$G12"
SESSYNC_TEST_PROCS=$procs SESSYNC_TEST_PPID=$ipid resume FA $G12 -tmp-f startup
check "обычный claude — его pid" test "$(cat "$(stf FA open)/$G12")" = $ipid
kill $spid; wait $spid 2>/dev/null
check "сессия остановлена, демон жив — forget удаляет сразу" has FA 'Готово' forget $G11 -y
check "и файла нет" test ! -e "$f9/$G11.jsonl"
kill $dpid $hpid $ipid; wait $dpid $hpid $ipid 2>/dev/null

say FA -tmp-f $G6 "начало G6"
run FA now >/dev/null; run FB now >/dev/null
say FB -tmp-f $G6 "FB дописал, ещё не зная"
hl FB $G6 "вопрос FB в G6"
check "forget на FA" has FA 'Готово' forget $G6 -y
run FB now >/dev/null
check "FB: дописанное не вернуло её в хранилище" not inbucket $G6
check "FB: удалена и там" test ! -e "$T/FB/projects/-tmp-f/$G6.jsonl"
check "FB: свой промпт этой сессии тоже удалён и не отправлен" not contains "$T/FB/history.jsonl" "вопрос FB в G6"
run FA now >/dev/null
check "FA: промпт FB не пришёл" not contains "$T/FA/history.jsonl" "вопрос FB в G6"

say FA -tmp-f $G7 "сессия при выключенной синхронизации"
run FA now >/dev/null
run FA off >/dev/null
check "выключено: удалена на этом Mac" has FA 'Синхронизация выключена' forget $G7 -y
check "файла нет" test ! -e "$f9/$G7.jsonl"
check "в хранилище пока есть" inbucket $G7
check "отменить, когда файлы уже удалены, — нельзя" has FA 'Отменить уже нельзя' forget --cancel $G7
opn FA $G9
say FA -tmp-f $G9 "выключено и открыта"
check "выключено и открыта: после выхода" has FA 'когда вы из неё выйдете' forget $G9 -y
cls FA $G9
printf '{"session_id":"%s"}' $G9 | ss FA hook end
for i in $(seq 1 75); do test -e "$f9/$G9.jsonl" || break; sleep 0.2; done
check "выключено: хук конца удалил её на этом Mac" test ! -e "$f9/$G9.jsonl"
run FA on >/dev/null
check "включили — из хранилища удалена" not inbucket $G7
check "и лежит в корзине" intrash $G7

say FA -tmp-f $G8 "передумали"
opn FA $G8
ss FA forget $G8 -y >/dev/null
check "отмена" has FA 'Отменено' forget --cancel $G8
cls FA $G8
run FA now >/dev/null; run FB now >/dev/null
check "отменённая осталась и синхронизируется" test -f "$T/FB/projects/-tmp-f/$G8.jsonl"
check "отменять нечего" has FA 'Отменять нечего' forget --cancel $G8
check "английский: forget" sh -c "$(declare -f ss); T='$T' here='$here'; SESSYNC_LANG=en ss FA forget --cancel $G8 | grep -q 'Nothing to undo'"

echo "== Другое имя пользователя на другом Mac"
mkdir -p "$T/s3/bucket7"
for m in U V; do mac $m; setconf $m bucket7; done
hu() { HOME=/Users/alice ss U "$@"; }   # первый Mac: /Users/alice
hv() { HOME=/home/bob ss V "$@"; }      # второй: другое имя и другая система путей
mkdir -p "$T/U/projects/-Users-alice-work-app"
printf '{"type":"user","message":{"role":"user","content":"путь alice"},"cwd":"/Users/alice/work/app","timestamp":"2026-10-10T10:00:00Z","sessionId":"u1"}\n' \
  > "$T/U/projects/-Users-alice-work-app/u1.jsonl"
echo '{"display":"промпт alice","timestamp":1,"project":"/Users/alice/work/app"}' > "$T/U/history.jsonl"
check "U: синхронизация" hu now
check "V: синхронизация" hv now
check "V: папка проекта названа по своей домашней папке" test -f "$T/V/projects/-home-bob-work-app/u1.jsonl"
check "V: под чужим именем папки нет" test ! -e "$T/V/projects/-Users-alice-work-app"
check "V: в истории промптов — свой путь проекта" contains "$T/V/history.jsonl" '"project":"/home/bob/work/app"'
printf '{"type":"user","message":{"role":"user","content":"ответ bob"},"cwd":"/home/bob/work/app","timestamp":"2026-10-10T11:00:00Z","sessionId":"u1"}\n' \
  >> "$T/V/projects/-home-bob-work-app/u1.jsonl"
mkdir -p "$T/V/projects/-home-bob-other"; say V -home-bob-other v1 "проект bob"
echo '{"display":"промпт bob","timestamp":2,"project":"/home/bob/other"}' >> "$T/V/history.jsonl"
check "V: отправка" hv now
check "U: получение" hu now
check "U: дописанное пришло в ту же сессию" contains "$T/U/projects/-Users-alice-work-app/u1.jsonl" "ответ bob"
check "U: новый проект V — под домашней папкой U" test -f "$T/U/projects/-Users-alice-other/v1.jsonl"
check "U: промпт V — с путём U" contains "$T/U/history.jsonl" '"project":"/Users/alice/other"'
check "в хранилище — имена по домашней папке хранилища" sh -c "$(declare -f at); T='$T' here='$here'; at U 'rclone lsf ss:projects' | grep -q '^-Users-alice-other/'"
check "поиск на V: путь проекта — свой" sh -c "$(declare -f ss); T='$T' here='$here'; HOME=/home/bob ss V find путь | grep -q '~/work/app'"
check "status на V называет пересчёт папок" sh -c "$(declare -f ss); T='$T' here='$here'; HOME=/home/bob ss V status | grep -q '/Users/alice в хранилище = /home/bob'"

echo "== Хранилище rclone: любое, что понимает rclone"
for m in W X; do mac $m; printf 'STORAGE=rclone\nREMOTE=%s\n' "$T/rclone-remote/sessync" > "$T/$m/config/sessync/config"; done
mkdir -p "$T/rclone-remote"
say W -tmp-rc r1 "через rclone"
check "W: синхронизация через rclone" run W now
run X now >/dev/null
check "X: получил сессию" contains "$T/X/projects/-tmp-rc/r1.jsonl" "через rclone"
check "в хранилище rclone — шифротекст" absent "$T/rclone-remote" "через rclone"
check "status называет хранилище rclone" has X 'rclone' status

echo "== Ничего не пишется за пределы папки проектов"
mkdir -p "$T/s3/bucket8" "$T/outside"
for m in Y Z; do mac $m; setconf $m bucket8; done
say Y -tmp-p8 y1 "обычная сессия"
ln -s "$T/outside" "$T/Y/projects/-tmp-p8/sub"   # ссылка на папку вне projects
run Y now >/dev/null; run Z now >/dev/null
check "ссылка на папку пришла ссылкой" test -L "$T/Z/projects/-tmp-p8/sub"
# файл «под ссылкой» в хранилище — так мог бы сделать только Mac с паролем
at Y "printf 'чужое' | rclone rcat ss:projects/-tmp-p8/sub/evil.txt 2>/dev/null"
check "файл под ссылкой: синхронизация проходит" run Z now
check "через ссылку ничего не записано" test ! -e "$T/outside/evil.txt"
check "в журнале названа причина" grep -q 'ссылка за пределы папки проектов' "$(stf Z sync.log)"
check "обычная сессия рядом пришла" test -f "$T/Z/projects/-tmp-p8/y1.jsonl"
mac Z2; setconf Z2 bucket8   # новый Mac: ссылка и файл под ней приходят за одну синхронизацию
check "ссылка и файл под ней за один раз: синхронизация проходит" run Z2 now
check "за один раз: через ссылку тоже ничего не записано" test ! -e "$T/outside/evil.txt"
ln -s "$T/outside/target.txt" "$T/Z2/projects/-tmp-p8/y2.jsonl"   # на месте будущего файла — ссылка
say Y -tmp-p8 y2 "сессия, которую не надо писать через ссылку"
run Y now >/dev/null
check "файл на месте ссылки: синхронизация проходит" run Z2 now
check "файл на месте ссылки: через неё ничего не записано" test ! -e "$T/outside/target.txt"

echo "== English"
en() { SESSYNC_LANG=en ss "$@"; }
check "английский: итог синхронизации" sh -c "$(declare -f ss en); T='$T' here='$here'; en A now | grep -q 'Done in'"
check "английский: status" sh -c "$(declare -f ss en); T='$T' here='$here'; en A status | grep -q 'Automatic sync: on'"
check "английский: поиск" sh -c "$(declare -f ss en); T='$T' here='$here'; en A find нетакогослова | grep -q 'Nothing found'"
check "английский: справка" sh -c "$(declare -f ss en); T='$T' here='$here'; en A help | grep -q 'sync now'"
check "английский: журнал движка" sh -c "$(declare -f ss en); T='$T' here='$here'; say() { :; }; printf '{\"type\":\"user\"}\n' >> '$T/A/projects/-tmp-p1/s1.jsonl'; en A now >/dev/null; grep -q 'appended' '$(stf A sync.log)'"

echo "== Повтор, если синхронизацию запросили, пока она шла"
mkdir "$(stf A lock)"
check "занято: выходит сразу и без ошибки" has A 'пройдёт ещё раз' now
check "занято: просьба повторить записана" test -f "$(stf A again)"
rmdir "$(stf A lock)"
check "следующая синхронизация проходит" run A now
check "и снимает просьбу" test ! -e "$(stf A again)"
check "блокировка снята" test ! -e "$(stf A lock)"

echo "== Мелочи"
src() { CLAUDE_CONFIG_DIR="$T/A" XDG_CONFIG_HOME="$T/A/config" XDG_STATE_HOME="$T/A/state" bash -c "source '$here/bin/sessync' version >/dev/null; $1"; }
hostid_ok() { env -u SESSYNC_HOST bash -c "source '$here/bin/sessync' version >/dev/null; host_id" | grep -Eq -- '-[0-9A-F]{8}$'; }
check "файл истории Mac: имя + начало аппаратного UUID (имена Mac совпадают)" hostid_ok
hostid_path() { env -u SESSYNC_HOST PATH=/usr/bin:/bin /bin/bash -c "source '$here/bin/sessync' version >/dev/null; host_id" | grep -Eq -- '-[0-9A-F]{8}$'; }
check "то же имя и без /usr/sbin в PATH (так запускаются хуки)" hostid_path
mkdir -p "$T/dotfiles" "$T/H/projects"; setconf H bucket
printf '{"cleanupPeriodDays": 30}\n' > "$T/dotfiles/settings.json"; chmod 644 "$T/dotfiles/settings.json"
ln -s "$T/dotfiles/settings.json" "$T/H/settings.json"
run H now >/dev/null
check "срок хранения: settings.json-ссылка осталась ссылкой" test -L "$T/H/settings.json"
check "срок хранения: записан в файл по ссылке, права прежние" sh -c "jq -e '.cleanupPeriodDays == 36500' '$T/dotfiles/settings.json' && test \"\$(stat -f %Lp '$T/dotfiles/settings.json')\" = 644"
pb="sessync-test-$$"
pbread() { osascript -l JavaScript -e "ObjC.import('AppKit'); var pb = \$.NSPasteboard.pasteboardWithName('$pb'); JSON.stringify([ObjC.deepUnwrap(pb.types), ObjC.unwrap(pb.stringForType('public.utf8-plain-text'))])"; }
printf 'секрет "в кавычках"' | SESSYNC_TEST_PASTEBOARD="$pb" src clip
out=$(pbread)
marked() { grep -q ConcealedType <<<"$out" && grep -q TransientType <<<"$out"; }
check "export: значение в буфере как есть" grep -qF 'секрет \"в кавычках\"' <<<"$out"
check "export: пометки «скрытое» и «временное» для истории буфера" marked
printf '' | SESSYNC_TEST_PASTEBOARD="$pb" src clip
out=$(pbread)
check "export: пустой ввод очищает буфер" sh -c "! grep -q секрет" <<<"$out"
osascript -l JavaScript -e "ObjC.import('AppKit'); \$.NSPasteboard.pasteboardWithName('$pb').releaseGlobally" >/dev/null 2>&1

echo "== sessync setup: хранилище, ключи и пароль — в терминале, как у человека"
PYT=${SESSYNC_PYTHON:-python3}
sm() {  # sm M command… — sessync on "Mac" M: secrets in its folder instead of the Keychain
  local m=$1; shift
  env -u SESSYNC_TEST_S3_ACCESS_KEY -u SESSYNC_TEST_S3_SECRET_KEY -u SESSYNC_TEST_PASSPHRASE \
    SESSYNC_TEST_KEYCHAIN="$T/$m/keychain" SESSYNC_BIN="$here/bin/sessync" \
    CLAUDE_CONFIG_DIR="$T/$m" XDG_CONFIG_HOME="$T/$m/config" XDG_STATE_HOME="$T/$m/state" \
    XDG_CACHE_HOME="$T/$m/cache" SESSYNC_HOST="host$m" "$here/bin/sessync" "$@"
}
tsm() {  # tsm M "command" action… — the same in a terminal (tests/tty.py); what it printed — in $T/M.out
  local m=$1 a=$2; shift 2
  "$PYT" -I "$here/tests/tty.py" "$@" -- bash -c "$(declare -f sm); T='$T' here='$here'; sm $m $a" > "$T/$m.out" 2>&1
}
smhas() { local m=$1 pat=$2; shift 2; sm "$m" "$@" 2>&1 | grep -q -- "$pat"; }
conf() { grep -qx -- "$2" "$T/$1/config/sessync/config"; }       # conf M LINE — a line of M's config
kc() { cat "$T/$1/keychain/$2" 2>/dev/null; }                     # kc M name — M's saved secret
shown() { grep -qF -- "$2" "$T/$1.out"; }                         # shown M text — it was on screen

check "без терминала: отказ" not sm M1 setup </dev/null
check "без терминала: просит свой терминал" smhas M1 'в своём терминале' setup </dev/null
mkdir -p "$T/s3/b-r2" "$T/M1/projects"   # бакет пользователь создаёт сам
check "первый Mac: Cloudflare R2, новый пароль, hook'и" tsm M1 setup \
  --expect 'Выбор [1-5]:' --send 1 --expect 'ID аккаунта Cloudflare' --send acc123 \
  --expect 'Имя бакета:' --send b-r2 \
  --expect 'S3 Access Key:' --secret AKR2TEST --expect 'S3 Secret Key:' --secret SKR2TEST-secret \
  --expect 'Enter — создать новый' --secret '' --expect '[Y/n]' --send '' --expect 'Первая синхронизация'
check "R2: адрес по ID аккаунта" conf M1 'ENDPOINT=https://acc123.r2.cloudflarestorage.com'
check "R2: провайдер, регион, бакет, язык" sh -c "$(declare -f conf); T='$T'; conf M1 PROVIDER=Cloudflare && conf M1 REGION=auto && conf M1 BUCKET=b-r2 && conf M1 STORAGE=s3 && conf M1 LANGUAGE=ru"
check "R2: подсказка, где взять ключи" shown M1 'Manage API tokens'
check "ключи сохранены" test "$(kc M1 s3-access-key)" = AKR2TEST -a "$(kc M1 s3-secret-key)" = SKR2TEST-secret
check "создан пароль: 32 буквы и цифры" grep -Eqx '[A-Za-z0-9]{32}' "$T/M1/keychain/passphrase"
check "ключи и пароль не попали на экран" sh -c "! grep -qF -e AKR2TEST -e SKR2TEST -e '$(kc M1 passphrase)' '$T/M1.out'"
check "проверка доступа и пароля прошла" shown M1 'Готово: доступ к хранилищу есть'
check "hook'и добавлены" jq -e '[.hooks.SessionStart[].hooks[].command] | any(test("sessync hook start"))' "$T/M1/settings.json"
say M1 -tmp-st st1 "сессия после setup"
check "после setup синхронизация работает" sm M1 now

mkdir -p "$T/M2/projects"; printf '{"cleanupPeriodDays": 36500}\n' > "$T/M2/settings.json"
check "второй Mac с другим паролем: проверка не проходит" not tsm M2 setup \
  --expect 'Выбор [1-5]:' --send 1 --expect 'ID аккаунта' --send acc123 --expect 'Имя бакета:' --send b-r2 \
  --expect 'S3 Access Key:' --secret AKR2TEST --expect 'S3 Secret Key:' --secret SKR2TEST-secret \
  --expect 'Enter — создать новый' --secret wrong-password-12345 --expect 'Проверка не прошла'
check "второй Mac: причина — пароль не тот" shown M2 'пароль шифрования не тот'
check "второй Mac: повторный setup — без выбора хранилища, ключи остаются, пароль заменяется" tsm M2 setup \
  --expect 'S3 Access Key уже есть' --secret '' --expect 'S3 Secret Key уже есть' --secret '' \
  --expect 'Пароль шифрования уже есть' --secret "$(kc M1 passphrase)" --expect 'Готово:' \
  --expect '[Y/n]' --send n --expect 'Первая синхронизация'
check "второй Mac: меню хранилища не показывалось" not shown M2 'Выбор [1-5]'
check "второй Mac: ключи прежние" test "$(kc M2 s3-access-key)" = AKR2TEST
check "второй Mac: от hook'ов отказались — их нет" not jq -e '.hooks' "$T/M2/settings.json"
check "второй Mac: сессии первого пришли" sh -c "$(declare -f sm); T='$T' here='$here'; sm M2 now && test -f '$T/M2/projects/-tmp-st/st1.jsonl'"
check "setup --storage, неверный выбор: хранилище не изменено" not tsm M1 'setup --storage' \
  --expect 'Выбор [1-5]:' --send 9 --expect 'не изменено'
check "и настройки прежние" conf M1 BUCKET=b-r2

mkdir -p "$T/s3/b-b2" "$T/M3/projects"
check "короткий пароль: отказ" not tsm M3 setup \
  --expect 'Выбор [1-5]:' --send 2 --expect 'Регион B2' --send us-west-004 --expect 'Имя бакета:' --send b-b2 \
  --expect 'S3 Access Key:' --secret K --expect 'S3 Secret Key:' --secret S \
  --expect 'Enter — создать новый' --secret short --expect 'Слишком короткий'
check "короткий пароль не сохранён" test ! -e "$T/M3/keychain/passphrase"
check "B2: адрес по региону" conf M3 'ENDPOINT=https://s3.us-west-004.backblazeb2.com'
check "B2: регион" conf M3 REGION=us-west-004
check "B2: подсказка про Application Keys" shown M3 'Application Keys'

mkdir -p "$T/s3/b-aws" "$T/M4/projects"
check "Amazon S3" tsm M4 setup \
  --expect 'Выбор [1-5]:' --send 3 --expect 'Регион AWS' --send eu-central-1 --expect 'Имя бакета:' --send b-aws \
  --expect 'S3 Access Key:' --secret AKAWS --expect 'S3 Secret Key:' --secret SKAWS \
  --expect 'Enter — создать новый' --secret aws-password-123456 --expect '[Y/n]' --send n --expect 'Первая синхронизация'
check "AWS: без адреса, с регионом" sh -c "$(declare -f conf); T='$T'; conf M4 ENDPOINT= && conf M4 REGION=eu-central-1 && conf M4 PROVIDER=AWS"
check "AWS: подсказка про IAM" shown M4 IAM
check "AWS: status называет регион" smhas M4 'b-aws (AWS eu-central-1)' status

mkdir -p "$T/s3/b-other" "$T/M5/config/sessync"
printf 'TRASH_DAYS=7\nINTERVAL=300\n' > "$T/M5/config/sessync/config"
check "другое S3-хранилище, регион пустой" tsm M5 setup \
  --expect 'Выбор [1-5]:' --send 4 --expect 'Адрес (endpoint' --send https://s3.example.net \
  --expect 'Регион (Enter' --send '' --expect 'Имя бакета:' --send b-other \
  --expect 'S3 Access Key:' --secret AKO --expect 'S3 Secret Key:' --secret SKO \
  --expect 'Enter — создать новый' --secret '' --expect '[Y/n]' --send n --expect 'Первая синхронизация'
check "другое S3: адрес и пустой регион" sh -c "$(declare -f conf); T='$T'; conf M5 ENDPOINT=https://s3.example.net && conf M5 REGION= && conf M5 PROVIDER=Other"
check "другое S3: подсказка про ключ только к бакету" shown M5 'не ключ всего аккаунта'
check "прочие строки настроек остались" sh -c "$(declare -f conf); T='$T'; conf M5 TRASH_DAYS=7 && conf M5 INTERVAL=300"

mkdir -p "$T/M6/projects"
check "rclone: без двоеточия — отказ" not tsm M6 setup \
  --expect 'Выбор [1-5]:' --send 5 --expect 'Хранилище и папка' --send gdrive --expect 'не изменено'
check "rclone: хранилище с папкой" tsm M6 setup \
  --expect 'Выбор [1-5]:' --send 5 --expect 'Хранилище и папка' --send ":local:$T/rc-setup/sessync" \
  --expect 'Enter — создать новый' --secret '' --expect '[Y/n]' --send n --expect 'Первая синхронизация'
check "rclone: настройки" sh -c "$(declare -f conf); T='$T'; conf M6 STORAGE=rclone && conf M6 'REMOTE=:local:$T/rc-setup/sessync' && conf M6 BUCKET="
check "rclone: ключи S3 не спрашивались" not shown M6 'S3 Access Key'
check "rclone: папка создана, контрольный файл записан" test -d "$T/rc-setup/sessync/v1-check"
say M6 -tmp-st r1 "через rclone после setup"
check "rclone: синхронизация работает" sm M6 now

echo "== sessync export: ключи и пароль по одному через буфер обмена (тестовый, не общий)"
pbx="sessync-test-export-$$"
cat > "$T/pbget" <<'EOF'
#!/bin/sh
exec osascript -l JavaScript -e 'function run(a) { ObjC.import("AppKit"); return ObjC.unwrap($.NSPasteboard.pasteboardWithName(a[0]).stringForType("public.utf8-plain-text")) || "" }' "$1"
EOF
chmod +x "$T/pbget"
check "без терминала: отказ" not sm M1 export </dev/null
mkdir -p "$T/M7"
check "не настроено: отказ с подсказкой" not tsm M7 export --expect 'сначала sessync setup'
export SESSYNC_TEST_PASTEBOARD=$pbx
check "R2: четыре шага" tsm M1 export \
  --expect 'Шаг 1 из 4' --expect 'нажмите Enter.' --send '' \
  --expect 'Шаг 2 из 4' --expect 'нажмите Enter.' --run "'$T/pbget' $pbx > '$T/clip2'" --send '' \
  --expect 'Шаг 3 из 4' --expect 'нажмите Enter.' --run "'$T/pbget' $pbx > '$T/clip3'" --send '' \
  --expect 'Шаг 4 из 4' --expect 'очистится.' --run "'$T/pbget' $pbx > '$T/clip4'" --send '' \
  --expect 'буфер обмена очищен'
check "шаг 1: адрес хранилища для «Паролей»" shown M1 'Веб-сайт» напишите адрес хранилища: acc123.r2.cloudflarestorage.com'
check "шаг 2: в буфере — Access Key" test "$(cat "$T/clip2")" = AKR2TEST
check "шаг 3: в буфере — пароль шифрования" test "$(cat "$T/clip3")" = "$(kc M1 passphrase)"
check "шаг 4: Secret Key и настройки для заметок" sh -c "grep -qx 'S3 Secret Key: SKR2TEST-secret' '$T/clip4' && grep -qx 'BUCKET=b-r2' '$T/clip4' && grep -qx 'ENDPOINT=https://acc123.r2.cloudflarestorage.com' '$T/clip4'"
check "в конце буфер пуст" test -z "$("$T/pbget" "$pbx")"
check "на экран ничего секретного" sh -c "! grep -qF -e AKR2TEST -e SKR2TEST -e '$(kc M1 passphrase)' '$T/M1.out'"
check "rclone: без ключей S3" tsm M6 export \
  --expect 'Шаг 1 из 4' --send '' --expect 'Впишите sessync' --send '' \
  --expect 'Шаг 3 из 4' --expect 'нажмите Enter.' --run "'$T/pbget' $pbx > '$T/clip3'" --send '' \
  --expect 'Шаг 4 из 4' --expect 'очистится.' --run "'$T/pbget' $pbx > '$T/clip4'" --send '' \
  --expect 'буфер обмена очищен'
check "rclone: адрес для «Паролей» — rclone.org" shown M6 'адрес хранилища: rclone.org'
check "rclone: шаг 3 — пароль" test "$(cat "$T/clip3")" = "$(kc M6 passphrase)"
check "rclone: шаг 4 — настройки, без Secret Key" sh -c "grep -qx 'REMOTE=:local:$T/rc-setup/sessync' '$T/clip4' && ! grep -q 'Secret Key' '$T/clip4'"
check "AWS: адрес для «Паролей» — s3.amazonaws.com" tsm M4 export \
  --expect 'адрес хранилища: s3.amazonaws.com' --send '' --expect 'Шаг 2 из 4' --send '' \
  --expect 'Шаг 3 из 4' --send '' --expect 'Шаг 4 из 4' --send '' --expect 'буфер обмена очищен'
unset SESSYNC_TEST_PASTEBOARD
osascript -l JavaScript -e "ObjC.import('AppKit'); \$.NSPasteboard.pasteboardWithName('$pbx').releaseGlobally" >/dev/null 2>&1

echo "== Поиск"
check "find находит по переписке" sh -c "$(declare -f ss); T='$T' here='$here'; ss B find уникальнаяфраза | grep -q 'claude -r s5'"
check "find: несуществующее не находит" has B 'Ничего не найдено' find нетакогослова
# В транскрипт может попасть вывод команд или текст страницы с ESC-последовательностями
say A -tmp-p1 s6 'опасныйтекст \u001b]0;pwned\u0007 \u001b[31mкрасный‮ конец'
out=$(ss A find опасныйтекст)
check "find находит сессию с управляющими символами" grep -q 'claude -r s6' <<<"$out"
check "find не выводит ESC, BEL и bidi" sh -c "! printf '%s' \"\$1\" | LC_ALL=C grep -q \$'[\\x1b\\x07]\\|\\xe2\\x80\\xae'" _ "$out"

echo "== Поиск: маска похожего на ключи"
# Значения собраны из частей: в исходнике нет цельных строк, похожих на настоящие ключи. sk-, ghp_ и github_pat_
# — только строчные буквы: правило длинных base64 их не поймает, и тест проверяет именно их правила
sk="sk-""ant-api03-ab12cd34ef56gh78ij90"; gh="ghp_""ab12cd34ef56gh78ij90kl12mn34op56qr78"
pat="gith""ub_pat_""11abcdefg0abcdefghij_klmnopqrstuvwxyz0123456789"; ak="AKIA""IOSFODNN7EXAMPLE"
hex="0123456789abcdef0123456789abcdef0123"; b64="dGhpc0lzQUxvbmdCYXNlNjRTZWNyZXQ0dGVzdFhZ"
say A -tmp-m1 m1 "маскаа1 ключ $sk конец"
say A -tmp-m1 m2 "маскаб2 токен $gh конец"
say A -tmp-m1 m3 "маскав3 доступ $pat конец"
say A -tmp-m1 m4 "маскаг4 облако $ak конец"
say A -tmp-m1 m5 "маскад5 хеш $hex конец"
say A -tmp-m1 m6 "маскае6 данные $b64 конец"
say A -tmp-m1 m7 "маскаж7 password=hunter2value конец"
say A -tmp-m1 m8 "масказ8 token=abcd1234 и secret=zzz9 конец"
plain="Hello-World-Of-Long-Words-Here-Again /Users/artemk/sessync/lib/find.py пароль: не трогать"
say A -tmp-m1 m9 "маскаи9 $plain"
for pair in "а1:$sk" "б2:$gh" "в3:$pat" "г4:$ak" "д5:$hex" "е6:$b64" "ж7:hunter2value" "з8:abcd1234"; do
  w="маска${pair%%:*}"; v="${pair#*:}"
  out=$(ss A find "$w")
  check "маска: $w — сессия найдена" grep -q 'claude -r m' <<<"$out"
  check "маска: $w — значение скрыто" sh -c '! printf "%s" "$1" | grep -qF -- "$2"' _ "$out" "$v"
  check "маска: $w — стоит ***" grep -qF '***' <<<"$out"
  check "маска: $w --no-mask показывает значение" sh -c "$(declare -f ss); T='$T' here='$here'; ss A find --no-mask '$w' | grep -qF -- '$v'"
done
out=$(ss A find масказ8); check "маска: secret= тоже скрыт" sh -c '! printf "%s" "$1" | grep -qF zzz9' _ "$out"
out=$(ss A find маскаи9)
check "маска: обычный текст не портится" grep -qF "$plain" <<<"$out"
check "маска: у обычного текста нет ***" sh -c '! printf "%s" "$1" | grep -qF "***"' _ "$out"
out=$(ss A find --raw маскаа1)
check "маска: и с --raw ключ скрыт" sh -c '! printf "%s" "$1" | grep -qF -- "$2"' _ "$out" "$sk"

echo "== Права файлов: только владелец, как у транскриптов Claude Code"
perm() { stat -f %Lp "$1"; }
check "индекс поиска — 700" test "$(perm "$T/A/cache/sessync/find")" = 700
check "файлы индекса — 600" test "$(perm "$(find "$T/A/cache/sessync/find/text" -type f | head -1)")" = 600
check "состояние sessync — 700" test "$(perm "$T/B/state/sessync")" = 700
check "чужая история промптов — 600" test "$(perm "$(find "$T/B/state/sessync/history" -type f | head -1)")" = 600
check "скачанные сессии — 600" test "$(perm "$T/B/projects/-tmp-p1/s4.jsonl")" = 600

echo "== install.sh и uninstall.sh"
H="$T/home"; mkdir -p "$H/.claude" "$H/.local/bin"
cat > "$H/.claude/settings.json" <<'JSON'
{
  "model": "opus",
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "echo чужой hook"}]},
                     {"matcher": "startup|resume", "hooks": [{"type": "command", "command": "[ -x ~/.local/bin/cc-sync ] && ~/.local/bin/cc-sync hook start || true"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}]
  }
}
JSON
ln -s "$here/bin/cc-sync" "$H/.local/bin/cc-sync"; ln -s "$here/bin/cc-find" "$H/.local/bin/cc-find"   # версия до 2.0
inst() { HOME="$H" CLAUDE_CONFIG_DIR="$H/.claude" SESSYNC_SKIP_BREW=1 "$here/$1"; }
check "install.sh" inst install.sh
s="$H/.claude/settings.json"
check "hook старта добавлен" jq -e '[.hooks.SessionStart[].hooks[].command] | any(test("sessync hook start"))' "$s"
check "hook конца добавлен" jq -e '[.hooks.SessionEnd[].hooks[].command] | any(test("sessync hook end"))' "$s"
check "старый hook cc-sync убран" jq -e '[.hooks[][].hooks[].command] | all(test("cc-sync") | not)' "$s"
check "чужие hook'и на месте" jq -e '([.hooks.SessionStart[].hooks[].command] | index("echo чужой hook")) and .hooks.Stop' "$s"
check "срок хранения поднят" jq -e '.cleanupPeriodDays == 36500' "$s"
check "команда в ~/.local/bin" test -L "$H/.local/bin/sessync"
check "старые команды cc-sync и cc-find убраны" test ! -L "$H/.local/bin/cc-sync" -a ! -L "$H/.local/bin/cc-find"
check "повторный install.sh ничего не меняет" sh -c "HOME='$H' CLAUDE_CONFIG_DIR='$H/.claude' SESSYNC_SKIP_BREW=1 '$here/install.sh' | grep -q 'уже на месте'"
check "uninstall.sh" inst uninstall.sh
check "hook'и sessync убраны" jq -e '[.hooks[]?[]?.hooks[]?.command] | all(test("sessync") | not)' "$s"
check "чужие hook'и остались" jq -e '([.hooks.SessionStart[].hooks[].command] | index("echo чужой hook")) and .hooks.Stop' "$s"
check "команда убрана" test ! -e "$H/.local/bin/sessync"
echo '{"hooks": {"SessionStart": [{"matcher": "без hooks"}]}}' > "$T/dotfiles/s2.json"
rm "$s"; ln -s "$T/dotfiles/s2.json" "$s"
check "install.sh: запись hook'а без поля hooks не ломает jq" inst install.sh
check "install.sh: settings.json-ссылка осталась ссылкой" test -L "$s"
check "install.sh: hook записан в файл по ссылке" jq -e '[.hooks.SessionStart[].hooks[]?.command] | any(test("sessync hook start"))' "$T/dotfiles/s2.json"
rm "$s"; printf '{ "model": "opus", сломано\n' > "$s"
check "install.sh: settings.json с ошибкой — отказ" not inst install.sh
check "install.sh: settings.json с ошибкой не тронут" grep -q 'сломано' "$s"

echo
if [ "$fails" -eq 0 ]; then echo "Все проверки прошли"; else echo "Не прошло: $fails"; exit 1; fi
