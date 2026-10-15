#!/bin/bash
# Keep a JumpServer session usable across drops.
#
# Royal TSX runs this as a "Custom Terminal" connection. The terminal session
# then belongs to *this* process, and this process does not exit until asked
# to -- so losing the SSH connection no longer ends the tab, closes it, or
# takes the scrollback with it. Everything printed stays on screen; press R to
# dial back in.
#
# Usage: jms_terminal.sh <login> <host> <port>
#
#   <login>  koko's direct-login name, e.g. alice@root@10.0.0.10
#   <host>   the koko host, e.g. jumpserver.example.com
#   <port>   the koko SSH port, e.g. 2222
#
# No pty plumbing here on purpose: while a session is up, ssh owns the
# terminal and handles raw mode, window size and Ctrl-C itself. This script
# only runs in the gaps between sessions, where the terminal is cooked again.

set -u

LOGIN="${1:?usage: jms_terminal.sh <login> <host> <port>}"
HOST="${2:?usage: jms_terminal.sh <login> <host> <port>}"
PORT="${3:?usage: jms_terminal.sh <login> <host> <port>}"

# Detect a peer that vanished rather than waiting forever on a dead socket.
ALIVE_INTERVAL="${JMS_SERVER_ALIVE_INTERVAL:-30}"

# Ctrl-C while waiting for a key must not take the wrapper down: it is the only
# thing holding the terminal session together. During a session the terminal is
# in raw mode, so Ctrl-C reaches the remote shell as a byte and ssh handles it.
trap '' INT

reconnect_keys='rR'
quit_keys='qQ'

prompt() {
    printf '\n\033[33m━━━ 连接已断开 ━━━\033[0m\n'
    printf '  \033[1mR\033[0m 重连    \033[1mQ\033[0m 关闭\n'
}

# Read the answer from the terminal itself, falling back to stdin if there is
# no controlling terminal to be had.
if ! exec 3</dev/tty 2>/dev/null; then
    exec 3<&0
fi

while true; do
    ssh -tt \
        -o ServerAliveInterval="$ALIVE_INTERVAL" \
        -o ServerAliveCountMax=3 \
        -p "$PORT" -l "$LOGIN" "$HOST"
    status=$?

    prompt

    key=''
    while :; do
        # -s keeps the key from echoing.
        if ! IFS= read -r -s -n1 key <&3; then
            key='q'
            break
        fi
        case "$key" in
            [$reconnect_keys]) key='r'; break ;;
            [$quit_keys]) key='q'; break ;;
        esac
    done

    printf '\r\033[K'
    if [ "$key" != 'r' ]; then
        break
    fi
    printf '正在重连 %s ...\n' "$LOGIN"
done

# 0 for a clean exit, otherwise pass ssh's status on so Royal TSX can show it.
exit "$status"
