#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
root=$(CDPATH= cd -P "$script_dir/../.." && pwd)
pid_file="$root/server.pid"

printf 'Stopping Door Number Detector...\n'

if [ ! -f "$pid_file" ]; then
    printf 'No background server PID file found.\n'
    exit 0
fi

server_pid=$(cat "$pid_file")
case "$server_pid" in
    ''|*[!0-9]*)
        printf 'Invalid PID in %s; refusing to stop a process.\n' "$pid_file" >&2
        exit 1
        ;;
esac

if ! kill -0 "$server_pid" 2>/dev/null; then
    printf 'PID %s is not running; removing stale PID file.\n' "$server_pid"
    rm -f "$pid_file"
    exit 0
fi

if ! ps -p "$server_pid" -o command= | grep -F 'door_number_detector.web.app' >/dev/null; then
    printf 'PID %s is not the Door Number Detector; refusing to stop it.\n' "$server_pid" >&2
    exit 1
fi

kill "$server_pid"
attempt=0
while kill -0 "$server_pid" 2>/dev/null && [ "$attempt" -lt 10 ]; do
    sleep 1
    attempt=$((attempt + 1))
done

if kill -0 "$server_pid" 2>/dev/null; then
    printf 'Shutdown requested; PID %s is still stopping. PID file retained.\n' "$server_pid"
else
    rm -f "$pid_file"
    printf 'Stopped PID %s.\n' "$server_pid"
fi
