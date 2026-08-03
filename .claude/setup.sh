#!/usr/bin/env bash

set -Eeuo pipefail

readonly DEFAULT_PYTHON_VERSION="3.12"
readonly DEFAULT_GIT_USER_NAME="Claude Code"
readonly DEFAULT_GIT_USER_EMAIL="claude-code@localhost"

log() {
    printf '[setup] %s\n' "$*"
}

fail() {
    printf '[setup] error: %s\n' "$*" >&2
    exit 1
}

run_as_root() {
    if [[ "$(id -u)" -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        fail "Installing system packages requires root or sudo: $*"
    fi
}

install_linux_package() {
    local package="$1"

    if command -v apt-get >/dev/null 2>&1; then
        run_as_root apt-get update
        run_as_root apt-get install -y "$package"
    elif command -v dnf >/dev/null 2>&1; then
        run_as_root dnf install -y "$package"
    elif command -v yum >/dev/null 2>&1; then
        run_as_root yum install -y "$package"
    elif command -v zypper >/dev/null 2>&1; then
        run_as_root zypper --non-interactive install "$package"
    elif command -v apk >/dev/null 2>&1; then
        run_as_root apk add "$package"
    elif command -v pacman >/dev/null 2>&1; then
        run_as_root pacman --sync --needed --noconfirm "$package"
    else
        fail "No supported Linux package manager found; install '$package' and rerun this script"
    fi
}

install_system_package() {
    local package="$1"

    case "$(uname -s)" in
        Darwin)
            if command -v brew >/dev/null 2>&1; then
                brew install "$package"
            elif [[ "$package" == "git" ]] && command -v xcode-select >/dev/null 2>&1; then
                xcode-select --install >/dev/null 2>&1 || true
                fail "Finish installing the Xcode Command Line Tools, then rerun this script"
            else
                fail "Homebrew is required to install '$package' automatically on macOS"
            fi
            ;;
        Linux)
            install_linux_package "$package"
            ;;
        *)
            fail "Automatic package installation supports macOS and Linux only"
            ;;
    esac
}

ensure_git() {
    if git --version >/dev/null 2>&1; then
        return
    fi

    log "Git was not found; installing it"
    install_system_package git
    git --version >/dev/null 2>&1 || fail "Git installation did not produce a working git command"
}

configure_git_identity() {
    local current_value

    current_value="$(git -C "$REPO_ROOT" config --get user.name || true)"
    if [[ -z "$current_value" ]]; then
        git -C "$REPO_ROOT" config --local user.name \
            "${CLAUDE_GIT_USER_NAME:-${GIT_AUTHOR_NAME:-$DEFAULT_GIT_USER_NAME}}"
        log "Configured a repo-local Git user.name"
    fi

    current_value="$(git -C "$REPO_ROOT" config --get user.email || true)"
    if [[ -z "$current_value" ]]; then
        git -C "$REPO_ROOT" config --local user.email \
            "${CLAUDE_GIT_USER_EMAIL:-${GIT_AUTHOR_EMAIL:-$DEFAULT_GIT_USER_EMAIL}}"
        log "Configured a repo-local Git user.email"
    fi
}

ensure_downloader() {
    if command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1; then
        return
    fi

    log "A download client was not found; installing curl"
    install_system_package curl
}

download_file() {
    local url="$1"
    local destination="$2"

    if command -v curl >/dev/null 2>&1; then
        curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
            --output "$destination" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget --quiet --output-document="$destination" "$url"
    else
        fail "curl or wget is required to download uv"
    fi
}

ensure_uv() {
    local install_script
    local uv_install_dir

    if command -v uv >/dev/null 2>&1; then
        UV_BIN="$(command -v uv)"
        return
    fi

    ensure_downloader
    install_script="$(mktemp "${TMPDIR:-/tmp}/tp-mcp-uv-install.XXXXXX")"
    trap 'rm -f "${install_script:-}"' RETURN

    log "uv was not found; installing it"
    download_file "https://astral.sh/uv/install.sh" "$install_script"
    uv_install_dir="${UV_INSTALL_DIR:-$HOME/.local/bin}"
    UV_INSTALL_DIR="$uv_install_dir" sh "$install_script"
    rm -f "$install_script"
    trap - RETURN
    UV_BIN="$uv_install_dir/uv"
    [[ -x "$UV_BIN" ]] || fail "uv installation did not create $UV_BIN"
}

compatible_python() {
    local candidate

    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c \
            'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
            command -v "$candidate"
            return
        fi
    done

    return 1
}

select_python() {
    local detected_python

    if [[ -n "${CLAUDE_PYTHON_VERSION:-}" ]]; then
        PYTHON_SELECTOR="$CLAUDE_PYTHON_VERSION"
        log "Ensuring requested Python $PYTHON_SELECTOR is installed with uv"
        "$UV_BIN" python install "$PYTHON_SELECTOR"
        return
    fi

    if detected_python="$(compatible_python)"; then
        PYTHON_SELECTOR="$detected_python"
        log "Using $($PYTHON_SELECTOR --version 2>&1)"
        return
    fi

    PYTHON_SELECTOR="$DEFAULT_PYTHON_VERSION"
    log "Python 3.10+ was not found; installing Python $PYTHON_SELECTOR with uv"
    "$UV_BIN" python install "$PYTHON_SELECTOR"
}

main() {
    local script_dir

    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    REPO_ROOT="$(cd "$script_dir/.." && pwd)"

    ensure_git
    git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || \
        fail "$REPO_ROOT is not a cloned Git worktree"
    [[ -f "$REPO_ROOT/pyproject.toml" && -f "$REPO_ROOT/uv.lock" ]] || \
        fail "pyproject.toml or uv.lock is missing from $REPO_ROOT"

    configure_git_identity
    ensure_uv
    select_python

    log "Installing the locked development environment"
    "$UV_BIN" sync --all-extras --frozen --python "$PYTHON_SELECTOR" --project "$REPO_ROOT"
    "$UV_BIN" run --frozen --project "$REPO_ROOT" tp-mcp --help >/dev/null

    log "Setup complete. Run commands with: '$UV_BIN' run --project '$REPO_ROOT' <command>"
}

main "$@"
