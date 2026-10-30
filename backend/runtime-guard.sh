#!/usr/bin/env bash
# Shared lifecycle ownership for active backend runners. This file is sourced;
# it is not a user-facing command.

# Extra exitlag-netd arguments shared by every runner. Kept here so a new runner
# cannot quietly omit the identity map: the helper also refuses to start if
# EXITLAG_IDENTITY_MAP is set but the flag never reaches it.
runtime_identity_args() {
    if [[ -n "${EXITLAG_IDENTITY_MAP:-}" ]]; then
        printf '%s\n--identity-map\n%s\n' "" "$EXITLAG_IDENTITY_MAP" | tail -n +2
    fi
}

runtime_guard_acquire() {
    local project_root="$1"
    exitlag_helper_path="$(readlink -f "$project_root/backend/exitlag-netd")"
    exitlag_state_dir="/run/exitlag-linux"
    exitlag_pidfile="$exitlag_state_dir/helper.pid"
    install -d -o root -g root -m 0755 "$exitlag_state_dir"

    exec {exitlag_lock_fd}>"$exitlag_state_dir/backend.lock"
    if ! flock -n "$exitlag_lock_fd"; then
        echo "another ExitLag backend runner owns the runtime lock" >&2
        return 4
    fi

    if [[ -f "$exitlag_pidfile" ]]; then
        local old_pid="" old_helper=""
        read -r old_pid old_helper <"$exitlag_pidfile" || true
        if [[ ! "$old_pid" =~ ^[1-9][0-9]*$ || "$old_helper" != "$exitlag_helper_path" ]]; then
            echo "refusing malformed or foreign stale state: $exitlag_pidfile" >&2
            return 4
        fi

        if [[ -d "/proc/$old_pid" ]]; then
            local live_exe
            live_exe="$(readlink "/proc/$old_pid/exe" 2>/dev/null || true)"
            live_exe="${live_exe% (deleted)}"
            if [[ "$live_exe" == "$exitlag_helper_path" ]]; then
                echo "recovering owned orphan helper pid=$old_pid"
                kill "$old_pid" 2>/dev/null || true
                for _ in $(seq 1 40); do
                    [[ ! -d "/proc/$old_pid" ]] && break
                    sleep 0.05
                done
                [[ -d "/proc/$old_pid" ]] && kill -9 "$old_pid" 2>/dev/null || true
            else
                echo "stale helper PID was reused; leaving unrelated pid=$old_pid untouched"
            fi
        fi

        if nft list table inet exitlag >/dev/null 2>&1; then
            nft delete table inet exitlag
            echo "recovered stale project-owned table inet exitlag"
        fi
        rm -f -- "$exitlag_pidfile"
    elif nft list table inet exitlag >/dev/null 2>&1; then
        echo "table inet exitlag exists without verifiable project ownership; refusing automatic deletion" >&2
        return 4
    fi
}

runtime_guard_record_helper() {
    local helper_pid="$1"
    printf '%s %s\n' "$helper_pid" "$exitlag_helper_path" >"$exitlag_pidfile"
}

# Bash does not mark dynamically allocated redirection descriptors close-on-exec.
# Close the ownership lock in long-lived children so a runner killed with
# SIGKILL cannot leave its helper or timer holding the lock forever.
runtime_guard_spawn_helper() {
    local lock_fd="$exitlag_lock_fd"
    [[ "$lock_fd" =~ ^[0-9]+$ ]] || { echo "invalid runtime lock descriptor" >&2; return 4; }
    (
        eval "exec ${lock_fd}>&-"
        exec "$@"
    ) &
    exitlag_spawned_pid=$!
}

runtime_guard_clear_helper() {
    local expected_pid="$1"
    if [[ -f "$exitlag_pidfile" ]]; then
        local recorded_pid="" recorded_helper=""
        read -r recorded_pid recorded_helper <"$exitlag_pidfile" || true
        if [[ "$recorded_pid" == "$expected_pid" && "$recorded_helper" == "$exitlag_helper_path" ]]; then
            rm -f -- "$exitlag_pidfile"
        fi
    fi
}

# Wait for a helper indefinitely, or until a timer expires. If the helper dies
# first this returns failure immediately so the caller's EXIT trap removes the
# nftables surface instead of leaving it in fail-open mode until a long sleep.
runtime_guard_wait() {
    local helper_pid="$1" duration="${2:-}"
    if [[ -z "$duration" ]]; then
        wait "$helper_pid"
        return $?
    fi

    local lock_fd="$exitlag_lock_fd"
    [[ "$lock_fd" =~ ^[0-9]+$ ]] || { echo "invalid runtime lock descriptor" >&2; return 4; }
    (
        eval "exec ${lock_fd}>&-"
        exec sleep "$duration"
    ) &
    exitlag_timer_pid=$!
    set +e
    wait -n "$helper_pid" "$exitlag_timer_pid"
    set -e
    if kill -0 "$helper_pid" 2>/dev/null; then
        return 0
    fi
    set +e
    wait "$helper_pid" 2>/dev/null
    local helper_status=$?
    set -e
    (( helper_status == 0 )) && helper_status=1
    echo "exitlag-netd exited before the ${duration}s timer (status $helper_status)" >&2
    return "$helper_status"
}
