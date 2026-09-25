# gates/lib/tools-venv.sh — the TRUSTED tools venv (sourced). Built by the trusted driver, never from the
# candidate: from origin/main's requirements-dev.lock of the repository the TRUSTED gates dir belongs to,
# into ${XDG_CACHE_HOME:-$HOME/.cache}/chronos-gates/tools-<sha256(lock)[:16]> (0700), with
#   python -m venv <dir> && <dir>/bin/python -m pip install --no-deps --only-binary=:all: --require-hashes -r lock
# using the trusted repository venv's base interpreter. A build needs the network; it runs NO candidate code.
# Reuse needs the marker <dir>/.gates-lock-sha256 == the lock digest. GATES_TOOLS_VENV (runner-set) points at a
# prebuilt tools venv instead (the harness's stub); the lane leaves it unset.
tools_venv() {  # prints the tools venv path; non-zero (with a reason on stderr) if it cannot be built
  if [ -n "${GATES_TOOLS_VENV:-}" ]; then printf '%s\n' "$GATES_TOOLS_VENV"; return 0; fi
  local gdir repo lock digest dir base cache
  gdir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" && repo="$(git -C "$gdir" rev-parse --show-toplevel 2>/dev/null)" \
    || { echo "the trusted gates dir is not in a git checkout" >&2; return 1; }
  cache="${XDG_CACHE_HOME:-$HOME/.cache}/chronos-gates"; mkdir -p "$cache" && chmod 0700 "$cache" || return 1
  lock="$(mktemp "$cache/lock.XXXXXX")" || return 1
  git -C "$repo" show origin/main:requirements-dev.lock > "$lock" 2>/dev/null || { rm -f "$lock"; echo "cannot read origin/main:requirements-dev.lock" >&2; return 1; }
  digest="$(sha256sum < "$lock" | cut -c1-64)"; dir="$cache/tools-${digest:0:16}"
  if [ "$(cat "$dir/.gates-lock-sha256" 2>/dev/null)" != "$digest" ]; then
    base="$(sed -n 's/^home = //p' "$repo/.venv/pyvenv.cfg" 2>/dev/null)/python3"
    [ -x "$base" ] || base="$(command -v python3)"
    rm -rf "$dir" && "$base" -m venv "$dir" >/dev/null 2>&1 \
      && "$dir/bin/python" -m pip install -q --disable-pip-version-check --no-deps --only-binary=:all: --require-hashes -r "$lock" >/dev/null 2>&1 \
      && printf '%s\n' "$digest" > "$dir/.gates-lock-sha256" && chmod 0700 "$dir" \
      || { rm -rf "$dir" "$lock"; echo "could not build the trusted tools venv from origin/main's requirements-dev.lock" >&2; return 1; }
  fi
  rm -f "$lock"; printf '%s\n' "$dir"
}
