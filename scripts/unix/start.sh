#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
root=$(CDPATH= cd -P "$script_dir/../.." && pwd)
python="$root/.venv/bin/python"
pid_file="$root/server.pid"

if [ ! -x "$python" ]; then
    printf 'ERROR: Python not found at %s\n' "$python" >&2
    printf 'Set up the Python 3.11 virtual environment at %s/.venv\n' "$root" >&2
    exit 1
fi

background=False
if [ -f "$root/.env" ]; then
    background=$(awk '
        /^[[:space:]]*RUN_IN_BACKGROUND[[:space:]]*=/ {
            value = $0
            sub(/^[^=]*=[[:space:]]*/, "", value)
            sub(/[[:space:]]*#.*/, "", value)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
            single_quote = sprintf("%c", 39)
            if (length(value) >= 2 &&
                ((substr(value, 1, 1) == "\"" && substr(value, length(value), 1) == "\"") ||
                 (substr(value, 1, 1) == single_quote && substr(value, length(value), 1) == single_quote))) {
                value = substr(value, 2, length(value) - 2)
            }
        }
        END { print value }
    ' "$root/.env")
fi

if [ "$background" = "True" ]; then
    printf 'Starting server in the background...\n'
    cd "$root"
    nohup "$python" -u -m door_number_detector.web.app \
        >"$root/server.log" 2>"$root/server_error.log" </dev/null &
    server_pid=$!
    sleep 1
    if ! kill -0 "$server_pid" 2>/dev/null; then
        wait "$server_pid" 2>/dev/null || true
        printf 'Server failed to start; check %s\n' "$root/server_error.log" >&2
        tail -n 20 "$root/server_error.log" >&2 || true
        exit 1
    fi
    printf '%s\n' "$server_pid" > "$pid_file"
    printf 'Server running (PID %s). Access at http://127.0.0.1:8080\n' "$server_pid"
else
    printf 'Starting server in the foreground...\n'
    cd "$root"
    exec "$python" -u -m door_number_detector.web.app
fi
