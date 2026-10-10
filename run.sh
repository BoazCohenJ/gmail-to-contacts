#!/usr/bin/env bash
# Cron-friendly wrapper: one run at a time, cleans up one-off containers, keeps a rotating log.
#   */15 * * * * /path/to/gmail-to-contacts/run.sh
set -Eeuo pipefail

cd "$(dirname "$0")"
mkdir -p logs data

LOG_FILE="logs/gmail_to_contacts.log"
MAX_LOG_LINES=1000
MAX_LOG_ARCHIVES=5
LOCK_FILE="/tmp/gmail-to-contacts.lock"

log_message() {
    printf '%s %s\n' "[$(date '+%Y-%m-%d %H:%M:%S')]" "$*"
}

rotate_log() {
    local i
    for ((i = MAX_LOG_ARCHIVES; i > 1; i--)); do
        if [[ -f "${LOG_FILE}.$((i - 1))" ]]; then
            mv "${LOG_FILE}.$((i - 1))" "${LOG_FILE}.${i}"
        fi
    done
    if [[ -f "$LOG_FILE" ]]; then
        cp "$LOG_FILE" "${LOG_FILE}.1"
        : > "$LOG_FILE"
    fi
}

if [[ -f "$LOG_FILE" ]] && (( $(wc -l < "$LOG_FILE") >= MAX_LOG_LINES )); then
    rotate_log
fi

cleanup_stopped_runs() {
    docker container prune \
        --filter label=com.docker.compose.project=gmail-to-contacts \
        --filter label=com.docker.compose.oneoff=True \
        --force >/dev/null
}

exec > >(tee -a "$LOG_FILE") 2>&1

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log_message "Skipping run: another run is already active."
    exit 0
fi

trap cleanup_stopped_runs EXIT
cleanup_stopped_runs

log_message "Starting run."
status=0
docker compose run --rm gmail-to-contacts || status=$?
log_message "Finished with status ${status}."
exit "$status"
