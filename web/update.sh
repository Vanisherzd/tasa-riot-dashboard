#!/bin/sh
set -u

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)
LOG_FILE="$SCRIPT_DIR/update.log"
LOCK_FILE="$SCRIPT_DIR/.update.lock"
LOCK_DIR="$SCRIPT_DIR/.update.lockdir"
OUTPUT_FILE=$(mktemp "${TMPDIR:-/tmp}/riot-dashboard.XXXXXX")

trim_log() {
    tail -n 500 "$LOG_FILE" >"$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
}

run_step() {
    label=$1
    shift
    printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$label"
    "$@"
    status=$?
    if [ "$status" -ne 0 ]; then
        printf 'ERROR: %s failed with exit code %s\n' "$label" "$status" >&2
        return "$status"
    fi
}

run_update() {
    cd "$REPO_ROOT" || return 1
    printf '[%s] dashboard update started\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"
    run_step "fetch RIOT passes" python3 scripts/fetch_riot_passes.py || return $?
    run_step "summarize RIOT passes" python3 scripts/summarize_riot_passes.py || return $?
    if [ -n "${FOSSA_EXPORT_URL:-}" ]; then
        run_step "fetch FOSSA messages" python3 scripts/fetch_fossa.py || return $?
    fi
    run_step "build dashboard" python3 web/build_site.py || return $?
    printf '\n[%s] dashboard update completed\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"
}

perform_update() {
    status=0
    run_update >"$OUTPUT_FILE" 2>&1 || status=$?
    tee -a "$LOG_FILE" <"$OUTPUT_FILE" || status=1
    trim_log || status=1
    return "$status"
}

run_locked() {
    if command -v flock >/dev/null 2>&1; then
        (
            flock -n 9 || {
                printf 'ERROR: another dashboard update is already running\n' >&2
                exit 75
            }
            perform_update
        ) 9>"$LOCK_FILE"
        return $?
    fi

    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        printf 'ERROR: another dashboard update is already running\n' >&2
        return 75
    fi
    (
        trap 'rmdir "$LOCK_DIR"' EXIT HUP INT TERM
        perform_update
        update_status=$?
        exit "$update_status"
    )
}

trap 'rm -f "$OUTPUT_FILE"' EXIT HUP INT TERM
status=0
run_locked || status=$?
exit "$status"
