#!/bin/sh
# Ежедневная резервная копия PostgreSQL с проверкой восстановления (сервис backup в docker-compose).
#
#   backup.sh loop          раз в BACKUP_CHECK_EVERY_S проверяет, есть ли копия за сегодня (UTC) после
#                           BACKUP_AFTER_UTC; если нет — делает. Компьютер был выключен — копия делается
#                           при следующем включении
#   backup.sh now           копия прямо сейчас (make backup)
#   backup.sh restore FILE  восстановить базу из копии (make restore FILE=...): база пересоздаётся
#
# Каждая копия проверяется: восстанавливается во временную базу, считаются строки ключевых таблиц,
# временная база удаляется. Итог — в $BACKUP_DIR/last_backup.json (его показывает админка).
#
# Переменные: PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE — подключение; BACKUP_DIR (/backups),
# BACKUP_AFTER_UTC (01:30 — после ночного обхода), BACKUP_KEEP (14 копий), BACKUP_CHECK_EVERY_S (600).
set -u

BACKUP_DIR=${BACKUP_DIR:-/backups}
BACKUP_AFTER_UTC=${BACKUP_AFTER_UTC:-01:30}
BACKUP_KEEP=${BACKUP_KEEP:-14}
BACKUP_CHECK_EVERY_S=${BACKUP_CHECK_EVERY_S:-600}
CHECK_DB="${PGDATABASE}_restore_check"
STATUS="$BACKUP_DIR/last_backup.json"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup: $*"; }

json_escape() { printf '%s' "$1" | tr '\n\r\t' '   ' | sed 's/\\/\\\\/g; s/"/\\"/g'; }

write_status() {  # ok file size_bytes listings crawl_runs error started_at
    tmp="$STATUS.tmp"
    printf '{"ok": %s, "file": "%s", "size_bytes": %s, "listings": %s, "crawl_runs": %s, "error": "%s", "started_at": "%s", "finished_at": "%s"}\n' \
        "$1" "$2" "${3:-0}" "${4:-null}" "${5:-null}" "$(json_escape "${6:-}")" "$7" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp" && mv "$tmp" "$STATUS"
}

count_rows() {  # база таблица
    psql -d "$1" -tAc "SELECT count(*) FROM $2" 2>/dev/null
}

backup_once() {
    started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    name="eadh_$(date -u +%Y-%m-%d).dump"
    file="$BACKUP_DIR/$name"
    partial="$file.partial"
    mkdir -p "$BACKUP_DIR"
    log "копия $name"

    if ! err=$(pg_dump -Fc -f "$partial" 2>&1); then
        rm -f "$partial"
        log "pg_dump: $err"
        write_status false "$name" 0 "" "" "pg_dump: $err" "$started"
        return 1
    fi

    # проверка: восстановить во временную базу и посчитать строки
    dropdb --if-exists "$CHECK_DB" >/dev/null 2>&1
    if ! err=$(createdb "$CHECK_DB" 2>&1 && pg_restore --no-owner --exit-on-error -d "$CHECK_DB" "$partial" 2>&1); then
        dropdb --if-exists "$CHECK_DB" >/dev/null 2>&1
        rm -f "$partial"
        log "проверка восстановления: $err"
        write_status false "$name" 0 "" "" "копия не восстанавливается: $err" "$started"
        return 1
    fi
    listings=$(count_rows "$CHECK_DB" listing)
    runs=$(count_rows "$CHECK_DB" crawl_run)
    live=$(count_rows "$PGDATABASE" listing)
    dropdb --if-exists "$CHECK_DB" >/dev/null 2>&1
    if [ -z "$listings" ] || { [ "${live:-0}" -gt 0 ] && [ "$listings" -eq 0 ]; }; then
        rm -f "$partial"
        log "проверка восстановления: в копии нет объявлений (в базе ${live:-?})"
        write_status false "$name" 0 "" "" "в восстановленной копии нет объявлений (в базе ${live:-?})" "$started"
        return 1
    fi

    mv "$partial" "$file"
    size=$(wc -c < "$file" | tr -d ' ')
    # хранить BACKUP_KEEP последних копий
    ls -1 "$BACKUP_DIR"/eadh_*.dump 2>/dev/null | sort -r | tail -n +$((BACKUP_KEEP + 1)) | xargs -r rm -f
    write_status true "$name" "$size" "$listings" "$runs" "" "$started"
    log "готово: $name, $size байт, объявлений $listings, запусков $runs; восстановление проверено"
}

due() {
    [ ! -e "$BACKUP_DIR/eadh_$(date -u +%Y-%m-%d).dump" ] || return 1
    now=$(date -u +%H%M)
    after=$(echo "$BACKUP_AFTER_UTC" | tr -d ':')
    [ "$now" -ge "$after" ]
}

restore() {
    file=$1
    [ -f "$file" ] || { log "нет файла $file"; exit 1; }
    pg_restore -l "$file" >/dev/null || { log "$file — не копия pg_dump"; exit 1; }
    log "восстановление $PGDATABASE из $file"
    psql -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"$PGDATABASE\" WITH (FORCE)" \
        -c "CREATE DATABASE \"$PGDATABASE\"" || exit 1
    pg_restore --no-owner --exit-on-error -d "$PGDATABASE" "$file" || exit 1
    log "готово: объявлений $(count_rows "$PGDATABASE" listing), запусков $(count_rows "$PGDATABASE" crawl_run)"
}

case "${1:-loop}" in
    now) backup_once ;;
    restore) restore "${2:?укажите файл копии}" ;;
    loop)
        log "резервные копии в $BACKUP_DIR: раз в сутки после $BACKUP_AFTER_UTC UTC, хранится $BACKUP_KEEP"
        while true; do
            if due; then backup_once; fi
            sleep "$BACKUP_CHECK_EVERY_S"
        done ;;
    *) echo "использование: backup.sh loop | now | restore FILE" >&2; exit 2 ;;
esac
