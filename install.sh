#!/bin/sh
# Pair Desk installer for macOS and Linux (and Git Bash on Windows).
#
#   curl -fsSL https://raw.githubusercontent.com/rennerdo30/pair-desk/main/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- update            # or: uninstall, --dry-run, claude codex opencode antigravity muse
#
# It needs Python 3.11+ and curl or wget. It downloads Pair Desk (the main branch, or
# $PAIR_DESK_REF; or the .tar.gz named by $PAIR_DESK_ARCHIVE) into a temporary folder and runs `desk.py install` from there, which shows
# exactly what it will do for supported agent tools and asks before each one. Non-Claude
# integrations get a copy in ~/.local/share/agent-pair-programming (or $PAIR_DESK_APP). The download
# is deleted afterwards. It never answers the questions for you.

main() {
    repo="rennerdo30/pair-desk"
    ref="${PAIR_DESK_REF:-main}"

    py=""
    ok() { "$@" -I -S -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1 </dev/null; }
    if [ -n "${PAIR_DESK_PYTHON:-}" ] && ok "$PAIR_DESK_PYTHON"; then
        py=$PAIR_DESK_PYTHON
    else
        for c in python3 python3.14 python3.13 python3.12 python3.11 python; do
            if command -v "$c" >/dev/null 2>&1 && ok "$c"; then py=$c; break; fi
        done
    fi
    if [ -z "$py" ]; then
        echo "Pair Desk needs Python 3.11 or newer (python3 or python on the PATH)." >&2
        echo "Install it from https://www.python.org/downloads/, Homebrew or your package manager, then run this again." >&2
        exit 1
    fi
    echo "Using $("$py" -c 'import sys; print(sys.executable, sys.version.split()[0])')"

    tmp=$(mktemp -d 2>/dev/null || mktemp -d -t pair-desk)
    trap 'rm -rf "$tmp"' EXIT INT TERM
    url="${PAIR_DESK_ARCHIVE:-https://github.com/$repo/archive/refs/heads/$ref.tar.gz}"
    if [ -f "$url" ]; then
        # a local .tar.gz (testing a release before it is pushed)
        cp "$url" "$tmp/src.tar.gz"
    elif command -v curl >/dev/null 2>&1; then
        echo "Downloading $url"
        curl -fsSL "$url" -o "$tmp/src.tar.gz"
    elif command -v wget >/dev/null 2>&1; then
        echo "Downloading $url"
        wget -q "$url" -O "$tmp/src.tar.gz"
    else
        echo "Neither curl nor wget is installed." >&2
        exit 1
    fi
    tar -xzf "$tmp/src.tar.gz" -C "$tmp"
    desk=$(ls -d "$tmp"/*/desk.py 2>/dev/null | head -n 1)
    if [ -z "$desk" ]; then
        echo "The download did not contain desk.py." >&2
        exit 1
    fi

    case "${1:-}" in
        install|update|uninstall) action=$1; shift ;;
        *) action=install ;;
    esac
    # stdin is this script when piped into sh: answer the questions from the terminal instead
    if [ -r /dev/tty ] && (exec </dev/tty) 2>/dev/null; then
        "$py" "$desk" "$action" "$@" </dev/tty
    else
        "$py" "$desk" "$action" "$@" </dev/null
    fi
}

# everything is inside main, so a partial download never runs half a script
main "$@"
