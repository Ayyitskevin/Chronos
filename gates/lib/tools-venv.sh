# gates/lib/tools-venv.sh — the TRUSTED tools venv (sourced). Built by the trusted driver, never from the
# candidate: from origin/main's requirements-dev.lock of the repository the TRUSTED gates dir belongs to,
# into ${XDG_CACHE_HOME:-$HOME/.cache}/chronos-gates/tools-<sha256(lock)[:16]> (0700), with
#   python -m venv <dir> && <dir>/bin/python -m pip install --no-deps --only-binary=:all: --require-hashes -r lock
# using the trusted repository venv's base interpreter. A build needs the network; it runs NO candidate code.
# Reuse needs the marker <dir>/.gates-lock-sha256 == the lock digest. GATES_TOOLS_VENV (runner-set) points at a
# prebuilt tools venv instead (the harness's stub); the lane leaves it unset.
# The TRUSTED base: GATES_TRUSTED_REF (default origin/main) of GATES_TRUSTED_REPO (default the repository the
# trusted gates dir belongs to). Runner knobs; the lane leaves them unset, the harness points them at a fixture.
trusted_repo() {
  if [ -n "${GATES_TRUSTED_REPO:-}" ]; then printf '%s\n' "$GATES_TRUSTED_REPO"; return 0; fi
  git -C "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" rev-parse --show-toplevel 2>/dev/null
}
trusted_show() {  # $1 = path in the trusted base → stdout; non-zero if absent
  local repo; repo="$(trusted_repo)" || return 1
  git -C "$repo" show "${GATES_TRUSTED_REF:-origin/main}:$1" 2>/dev/null
}
tools_venv() {  # prints the tools venv path; non-zero (with a reason on stderr) if it cannot be built
  if [ -n "${GATES_TOOLS_VENV:-}" ]; then printf '%s\n' "$GATES_TOOLS_VENV"; return 0; fi
  local repo lock digest dir base cache
  repo="$(trusted_repo)" || { echo "the trusted gates dir is not in a git checkout" >&2; return 1; }
  cache="${XDG_CACHE_HOME:-$HOME/.cache}/chronos-gates"; mkdir -p "$cache" && chmod 0700 "$cache" || return 1
  lock="$(mktemp "$cache/lock.XXXXXX")" || return 1
  trusted_show requirements-dev.lock > "$lock" || { rm -f "$lock"; echo "cannot read the trusted base's requirements-dev.lock" >&2; return 1; }
  digest="$(sha256sum < "$lock" | cut -c1-64)"; dir="$cache/tools-${digest:0:16}"
  if [ "$(cat "$dir/.gates-lock-sha256" 2>/dev/null)" != "$digest" ]; then
    # G70-FIX r2: a digest-scoped flock serialises cold builds; the build happens in a private sibling and is verified before one
    # atomic rename publishes it, so no builder ever deletes or consumes a directory another process is building or executing from.
    # (The venv is built at its private path and renamed: it is used through `python -m` only, never through generated scripts.)
    local tmp; exec 9>"$cache/.build-${digest:0:16}.lock" && flock 9 || { rm -f "$lock"; echo "cannot lock the tools venv build" >&2; return 1; }
    if [ "$(cat "$dir/.gates-lock-sha256" 2>/dev/null)" != "$digest" ]; then  # re-check: another builder may have published meanwhile
      base="$(sed -n 's/^home = //p' "$repo/.venv/pyvenv.cfg" 2>/dev/null)/python3"
      [ -x "$base" ] || base="$(command -v python3)"
      tmp="$(mktemp -d "$cache/.build-${digest:0:16}.XXXXXX")" || { rm -f "$lock"; return 1; }
      "$base" -I -B -m venv "$tmp" >/dev/null 2>&1 \
        && "$tmp/bin/python" -I -B -m pip install -q --disable-pip-version-check --no-deps --only-binary=:all: --require-hashes -r "$lock" >/dev/null 2>&1 \
        && [ -x "$tmp/bin/python" ] && printf '%s\n' "$digest" > "$tmp/.gates-lock-sha256" && chmod 0700 "$tmp" \
        || { rm -rf "$tmp" "$lock"; echo "could not build the trusted tools venv from origin/main's requirements-dev.lock" >&2; return 1; }
      [ ! -e "$dir" ] || mv "$dir" "$cache/.stale-${digest:0:16}.$$.$RANDOM" || { rm -rf "$tmp" "$lock"; return 1; }  # an invalid dir is renamed aside, never deleted
      mv -T "$tmp" "$dir" || { rm -rf "$tmp" "$lock"; echo "could not publish the trusted tools venv" >&2; return 1; }
    fi
    exec 9>&-
  fi
  rm -f "$lock"; printf '%s\n' "$dir"
}
