#!/usr/bin/env bash
# Выгрузить конфигурацию (и расширения) из информационной базы 1С в файлы и
# обновить индекс Ариадны — Linux-версия scripts/update_from_infobase.ps1
# (те же шаги и параметры). Для запуска по расписанию (cron, systemd timer).
#
#   1. Конфигуратор в пакетном режиме: /DumpConfigToFiles. Первый раз —
#      полная выгрузка, дальше -update -force (только изменения по
#      ConfigDumpInfo.xml), поэтому каталог выгрузки (--dump-dir) хранится
#      между запусками. Без графического экрана Конфигуратор запускается
#      через xvfb-run (пакет xvfb).
#   2. По умолчанию — только разница по манифесту; --full-upload отправляет
#      tar.gz целиком. --target-root использует rsync. Template.* не передаются.
#   3. POST /api/projects/<проект>/sources/<источник>/refresh — Ариадна
#      переиндексирует источник, скрипт ждёт окончания.
#
# Подробно — docs/update.md. Коды возврата: 0 — обновлено; 1 — ошибка
# выгрузки; 2 — ошибка передачи или индексации; 3 — ошибка параметров.
#
# Пример:
#   ARIADNA_TOKEN=ar_... ONEC_PASSWORD=... ./update_from_infobase.sh \
#     --ariadna-url http://ariadna.lan:19878 --project myconf \
#     --server 'srv1c:1541\myconf' --user Робот --dump-dir /var/lib/ariadna-dump/myconf \
#     --extensions 'Расширение1,Расширение2'

set -uo pipefail

usage() {
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Параметры:
  --ariadna-url URL        адрес Web UI Ариадны (обязательно)
  --project ID             проект Ариадны (обязательно)
  --source ID              источник основной конфигурации (по умолчанию main)
  --file КАТАЛОГ           файловая база                  } одно из двух
  --server 'сервер\база'   серверная база                 }
  --user ИМЯ               пользователь базы; пароль — ONEC_PASSWORD
  --extensions 'А,Б'       расширения через запятую
  --extension-sources 'А=src1,Б=src2'   источники Ариадны для расширений
                           (по умолчанию — имя расширения)
  --dump-dir КАТАЛОГ       постоянный каталог выгрузки (обязательно)
  --full-upload            передать полную выгрузку архивом (по умолчанию — разница)
  --target-root КАТАЛОГ    передача rsync в папку sources проекта вместо архива
  --server-dump            многопоточная выгрузка на сервере 1С (-Server)
  --platform ПУТЬ          путь к 1cv8 (по умолчанию — самая новая в /opt/1cv8)
  --timeout-minutes N      сколько ждать индексацию (по умолчанию 120)
  --skip-dump              не выгружать — передать то, что уже в --dump-dir
Токен Ариадны — переменная ARIADNA_TOKEN (роль user достаточно).
EOF
}

ARIADNA_URL='' PROJECT='' SOURCE='main' IB_FILE='' IB_SERVER='' USER_1C=''
EXTENSIONS='' EXT_SOURCES='' DUMP_DIR='' TARGET_ROOT='' SERVER_DUMP=0
PLATFORM='' TIMEOUT_MIN=120 SKIP_DUMP=0 FULL_UPLOAD=0
while [ $# -gt 0 ]; do
    case "$1" in
        --ariadna-url) ARIADNA_URL="$2"; shift 2 ;;
        --project) PROJECT="$2"; shift 2 ;;
        --source) SOURCE="$2"; shift 2 ;;
        --file) IB_FILE="$2"; shift 2 ;;
        --server) IB_SERVER="$2"; shift 2 ;;
        --user) USER_1C="$2"; shift 2 ;;
        --extensions) EXTENSIONS="$2"; shift 2 ;;
        --extension-sources) EXT_SOURCES="$2"; shift 2 ;;
        --dump-dir) DUMP_DIR="$2"; shift 2 ;;
        --target-root) TARGET_ROOT="$2"; shift 2 ;;
        --server-dump) SERVER_DUMP=1; shift ;;
        --platform) PLATFORM="$2"; shift 2 ;;
        --timeout-minutes) TIMEOUT_MIN="$2"; shift 2 ;;
        --skip-dump) SKIP_DUMP=1; shift ;;
        --full-upload) FULL_UPLOAD=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Неизвестный параметр: $1 (--help — справка)" >&2; exit 3 ;;
    esac
done

if [ -z "$ARIADNA_URL" ] || [ -z "$PROJECT" ] || [ -z "$DUMP_DIR" ]; then
    echo 'Нужны --ariadna-url, --project и --dump-dir (--help — справка)' >&2; exit 3
fi
ARIADNA_URL="${ARIADNA_URL%/}"
LOG_DIR="$DUMP_DIR/logs"
mkdir -p "$LOG_DIR" || exit 3
RUN_LOG="$LOG_DIR/update_$(date +%Y%m%d_%H%M%S).log"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$RUN_LOG"; }
fail() { log "ОШИБКА: $2"; exit "$1"; }

for tool in curl tar; do
    command -v "$tool" >/dev/null || fail 3 "нужна утилита $tool"
done
[ -n "$TARGET_ROOT" ] && { command -v rsync >/dev/null || fail 3 'для --target-root нужна утилита rsync'; }

if [ "$SKIP_DUMP" = 0 ]; then
    if { [ -n "$IB_FILE" ] && [ -n "$IB_SERVER" ]; } || { [ -z "$IB_FILE" ] && [ -z "$IB_SERVER" ]; }; then
        fail 3 'укажите базу: --file (файловая) или --server (серверная), что-то одно'
    fi
    [ "$SERVER_DUMP" = 1 ] && [ -z "$IB_SERVER" ] && fail 3 '--server-dump — только для серверной базы'
    if [ -z "$PLATFORM" ]; then
        PLATFORM=$(ls -d /opt/1cv8/x86_64/8.*/1cv8 /opt/1cv8/aarch64/8.*/1cv8 2>/dev/null | sort -V | tail -1)
    fi
    [ -x "$PLATFORM" ] || fail 3 'не найден 1cv8 — укажите --platform'
fi

# Что выгружаем: «имя расширения|каталог|источник»; основная конфигурация — без имени.
ITEMS=("|$DUMP_DIR/config|$SOURCE")
IFS=',' read -r -a EXT_LIST <<< "$EXTENSIONS"
for ext in "${EXT_LIST[@]}"; do
    ext="$(echo "$ext" | xargs)"; [ -z "$ext" ] && continue
    sid="$ext"
    IFS=',' read -r -a PAIRS <<< "$EXT_SOURCES"
    for pair in "${PAIRS[@]}"; do
        [ -z "$(echo "$pair" | xargs)" ] && continue
        case "$pair" in *=*) ;; *) fail 3 "--extension-sources: ожидается «Имя=источник», получено «$pair»" ;; esac
        name="$(echo "${pair%%=*}" | xargs)"; value="$(echo "${pair#*=}" | xargs)"
        [ "$name" = "$ext" ] && sid="$value"
    done
    ITEMS+=("$ext|$DUMP_DIR/ext/$ext|$sid")
done

# ---------------------------------------------------------------- выгрузка
dump() {
    local name="$1" dir="$2" what='конфигурацию' mode='полная'
    [ -n "$name" ] && what="расширение «$name»"
    mkdir -p "$dir"
    local out="$LOG_DIR/designer.log" result="$LOG_DIR/designer_result.txt"
    rm -f "$result"
    local args=(DESIGNER)
    if [ -n "$IB_FILE" ]; then args+=(/F "$IB_FILE"); else args+=(/S "$IB_SERVER"); fi
    [ -n "$USER_1C" ] && args+=(/N "$USER_1C")
    [ -n "${ONEC_PASSWORD:-}" ] && args+=(/P "$ONEC_PASSWORD")
    args+=(/DisableStartupDialogs /DisableStartupMessages /DumpConfigToFiles "$dir")
    [ -n "$name" ] && args+=(-Extension "$name")
    # -update требует ConfigDumpInfo.xml прошлой выгрузки: без него — полная.
    if [ -f "$dir/ConfigDumpInfo.xml" ]; then args+=(-update -force); mode='только изменения'; fi
    [ "$SERVER_DUMP" = 1 ] && args+=(-Server)
    args+=(/Out "$out" /DumpResult "$result")
    log "Выгрузка: $what ($mode)"
    local runner=()
    # Конфигуратору нужен экран даже в пакетном режиме: на сервере без X — xvfb-run.
    if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
        command -v xvfb-run >/dev/null || fail 3 'нет графического экрана: установите xvfb (xvfb-run)'
        runner=(xvfb-run -a)
    fi
    "${runner[@]}" "$PLATFORM" "${args[@]}"
    local code=$? res=''
    [ -f "$result" ] && res="$(tr -d '[:space:]\357\273\277' < "$result")"
    if [ "$code" != 0 ] || [ "$res" != 0 ]; then
        [ -f "$out" ] && tail -n 30 "$out" | while IFS= read -r line; do log "  1С: $line"; done
        fail 1 "выгрузка не удалась: $what, код $code, результат «$res». Журнал Конфигуратора: $out"
    fi
}

if [ "$SKIP_DUMP" = 0 ]; then
    log "Платформа: $PLATFORM"
    for item in "${ITEMS[@]}"; do IFS='|' read -r name dir sid <<< "$item"; dump "$name" "$dir"; done
fi

# ---------------------------------------------------------------- передача и индексация
urlencode() {
    # Имена источников и проектов в адресе (у расширений бывают русские).
    python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1" 2>/dev/null         || printf '%s' "$1"
}

refresh() {
    local sid="$1" archive="$2"
    local url="$ARIADNA_URL/api/projects/$(urlencode "$PROJECT")/sources/$(urlencode "$sid")/refresh"
    local resp="$LOG_DIR/ariadna_response.json"
    local curl_args=(-sS -o "$resp" -w '%{http_code}' -X POST -F wait=1 -F refresh_manifest=1 -F "timeout=$((TIMEOUT_MIN * 60))"
                     --max-time $((TIMEOUT_MIN * 60 + 600)))
    [ -n "${ARIADNA_TOKEN:-}" ] && curl_args+=(-H "Authorization: Bearer $ARIADNA_TOKEN")
    [ -n "$archive" ] && curl_args+=(-F "xml_archive=@$archive;type=application/gzip")
    local status
    status="$(curl "${curl_args[@]}" "$url")" || fail 2 "нет связи с Ариадной ($url)"
    [ "$status" = 200 ] || fail 2 "Ариадна ответила $status на обновление источника «$sid»: $(cat "$resp" 2>/dev/null)"
    local summary
    summary="$(python3 - "$resp" 2>/dev/null <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding='utf-8'))
i = d.get('index') or {}
print('{}, {} с; модулей {}, процедур {}'.format(
    d.get('status'), d.get('duration_sec'), i.get('total_modules'), i.get('total_procedures')))
PY
)" || summary="$(cat "$resp")"
    log "Источник «$sid»: $summary"
}

delta_refresh() {
    local sid="$1" dir="$2" url="$ARIADNA_URL/api/projects/$(urlencode "$PROJECT")/sources/$(urlencode "$sid")"
    local manifest="$LOG_DIR/server_manifest.json" delta="$DUMP_DIR/delta_$sid.zip"
    local resp="$LOG_DIR/ariadna_response.json" status attempt get_args
    for attempt in 1 2; do
        get_args=(-sS -o "$manifest" -w '%{http_code}')
        [ -n "${ARIADNA_TOKEN:-}" ] && get_args+=(-H "Authorization: Bearer $ARIADNA_TOKEN")
        status="$(curl "${get_args[@]}" "$url/manifest")" \
            || fail 2 "нет связи с Ариадной при запросе манифеста источника «$sid»"
        if [ "$status" = 404 ] || [ "$status" = 405 ]; then return 1; fi
        [ "$status" = 200 ] || fail 2 "Ариадна ответила $status на запрос манифеста «$sid»: $(cat "$manifest" 2>/dev/null)"
        if ! command -v python3 >/dev/null; then
            log 'Python 3 не найден: для совместимости передаётся полный архив'
            return 1
        fi
        python3 "$SCRIPT_DIR/build_source_delta.py" "$dir" "$manifest" "$delta" > "$LOG_DIR/delta_summary.json" \
            || fail 2 'не удалось построить архив разницы (нужен Python 3)'
        local base
        base="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1],encoding="utf-8"))["version"])' "$manifest")"
        local curl_args=(-sS -o "$resp" -w '%{http_code}' -X POST -F "base_version=$base"
                         -F wait=1 -F "timeout=$((TIMEOUT_MIN * 60))"
                         --max-time $((TIMEOUT_MIN * 60 + 600))
                         -F "delta_archive=@$delta;type=application/zip")
        [ -n "${ARIADNA_TOKEN:-}" ] && curl_args+=(-H "Authorization: Bearer $ARIADNA_TOKEN")
        status="$(curl "${curl_args[@]}" "$url/delta")" || fail 2 'нет связи с Ариадной при передаче разницы'
        if [ "$status" = 404 ] || [ "$status" = 405 ]; then return 1; fi
        if [ "$status" = 409 ] && [ "$attempt" = 1 ]; then
            log "Манифест источника «$sid» изменился — запрашиваем его повторно"
            continue
        fi
        [ "$status" = 200 ] || fail 2 "Ариадна ответила $status на передачу разницы «$sid»: $(cat "$resp" 2>/dev/null)"
        local summary index_summary
        summary="$(cat "$LOG_DIR/delta_summary.json")"
        index_summary="$(python3 - "$resp" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding='utf-8')); i = d.get('index') or {}
print('{}, модулей {}, процедур {}'.format(d.get('status'), i.get('total_modules'), i.get('total_procedures')))
PY
)"
        log "Источник «$sid»: передана разница $summary; $index_summary"
        rm -f "$delta"
        return 0
    done
    fail 2 "манифест источника «$sid» менялся дважды; повторите запуск"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

n=0
for item in "${ITEMS[@]}"; do
    IFS='|' read -r name dir sid <<< "$item"
    n=$((n + 1))
    [ -f "$dir/Configuration.xml" ] || fail 2 "в $dir нет выгрузки (Configuration.xml)"
    if [ -n "$TARGET_ROOT" ]; then
        target="$TARGET_ROOT/$sid/xml"
        log "Копирование в $target"
        mkdir -p "$target" || fail 2 "не создать $target"
        # --delete — зеркало: удалённые в базе файлы удаляются и в Ариадне.
        rsync -a --delete --exclude='Template.*' --exclude='logs/' "$dir/" "$target/" \
            || fail 2 "rsync завершился с ошибкой"
        refresh "$sid" ''
    elif [ "$FULL_UPLOAD" = 0 ] && delta_refresh "$sid" "$dir"; then
        :
    else
        archive="$DUMP_DIR/upload_$n.tar.gz"
        log "Архив для передачи: $archive"
        tar -czf "$archive" --exclude='Template.*' -C "$dir" . || fail 2 'не удалось собрать архив'
        log "Передача $(( $(stat -c %s "$archive") / 1048576 )) МБ в Ариадну"
        refresh "$sid" "$archive"
        rm -f "$archive"
    fi
done
log 'Готово: индекс Ариадны обновлён'
exit 0
