#!/bin/sh
set -eu

uid=$1
gid=$2
if [ "$uid" -eq 0 ]; then
    uid=$(id -u postgres)
    gid=$(id -g postgres)
fi
if ! getent passwd "$uid" >/dev/null; then
    printf 'golbench:x:%s:%s:Benchmark:/tmp:/bin/sh\n' "$uid" "$gid" >> /etc/passwd
fi

# Match the host caller's UID so owner-readable GOLs can stay read-only.
exec setpriv --reuid="$uid" --regid="$gid" --clear-groups sh -ec '
    export HOME=/tmp
    if [ ! -r /input.gol ]; then
        echo "The mounted GOL must be readable by the unprivileged PostgreSQL user." >&2
        exit 1
    fi
    work=$(mktemp -d)
    trap '"'"'
        if [ -f "$work/data/postmaster.pid" ]; then
            pg_ctl -D "$work/data" -m immediate -w stop >&2
        fi
        rm -rf "$work"
    '"'"' EXIT
    initdb -D "$work/data" -A trust -U golbench >/dev/null
    pg_ctl -D "$work/data" -l "$work/postgres.log" \
        -o "-k $work -c listen_addresses=" -w start >&2
    stdbuf -oL psql -XqAt -F "|" -h "$work" -U golbench -d postgres -v ON_ERROR_STOP=1 || {
        cat "$work/postgres.log" >&2
        exit 1
    }
'
