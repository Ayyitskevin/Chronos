# gates/lib/sandbox.sh — the ONE trusted launcher for candidate code. Sourced (never executed) by the
# gates that run code from the PR (10, 40, 60, 70); it comes from the trusted gate set like they do.
#   sandbox_snapshot <out>          clone the lane's HEAD into <out>/snap (the immutable exact-head tree)
#   sandbox_run <out> <cmd...>      run <cmd> in bwrap: no network, new pid namespace and session, cleared
#                                   environment (the fixed allowlist + PYTHONPATH=<snap>/src), tmpfs /home
#                                   and /tmp, read-only /usr /etc + the Python toolchain + the lane venv +
#                                   the snapshot; the ONLY writable paths are <out>/io and the snapshot
#                                   output dirs named in SANDBOX_WRITABLE (each backed by <out>/w/<name>).
#   sandbox_identity <out> <python> fail unless `chronos.__file__` resolves under <snap>/src, in the sandbox
#                                   (and under `python -I` too when the lane venv's editable .pth is overlaid)
# Parity with the host, none of it network or home: the snapshot carries the lane's origin/main ref (a local
# fetch, as data); every lane-venv *.pth that names a src dir is overlaid read-only (in the sandbox only)
# with one naming <snap>/src, so `python -I` imports the snapshot; a GATES_SANDBOX_RO entry that is a FILE
# is bound at its real path and its directory appended to the sandbox PATH (e.g. age, age-keygen, node).
# SANDBOX_OVERLAY (gate-internal, space-separated SRC=RELPATH) binds trusted files read-only over snapshot
# paths, in the sandbox's view only (the snapshot's bytes are unchanged).
# A missing bwrap or a sandbox that cannot start is an error for the caller to FAIL on — never a fallback.
# GATES_SANDBOX_RO (runner-set, colon-separated absolute paths) adds read-only binds for a toolchain
# outside /usr; the lane leaves it unset. The GATE itself may set, per call: SANDBOX_RO (space-separated
# trusted paths bound read-only, e.g. the tools venv, the wheel cache; a bound venv's toolchain is bound
# too), SANDBOX_PYPATH (one trusted dir appended to PYTHONPATH) and SANDBOX_ENV (NAME=VALUE pairs, only
# PIP_NO_INDEX, PIP_FIND_LINKS and PYTEST_DISABLE_PLUGIN_AUTOLOAD are accepted — anything else is an error, never passed).
sandbox_snapshot() {
  local top sha; top="$(git rev-parse --show-toplevel)" && sha="$(git -C "$top" rev-parse HEAD)" || return 1
  git clone -q --no-checkout "$top" "$1/snap" 2>/dev/null && git -C "$1/snap" checkout -q --detach "$sha" 2>/dev/null || return 1
  mkdir -p "$1/io" "$1/w" && chmod 0700 "$1" "$1/io" || return 1
  if git -C "$top" rev-parse -q --verify refs/remotes/origin/main >/dev/null; then  # the base ref, as data
    git -C "$1/snap" fetch -q "$top" "+refs/remotes/origin/main:refs/remotes/origin/main" 2>/dev/null || return 1
  fi
}
sandbox_run() {
  local out="$1" snap="$1/snap" top venv home d p; shift
  command -v bwrap >/dev/null 2>&1 || { echo "bwrap is not installed" >&2; return 125; }
  top="$(git rev-parse --show-toplevel)" || return 125
  local -a a=(--unshare-net --unshare-pid --die-with-parent --new-session --clearenv
    --ro-bind /usr /usr --ro-bind /etc /etc --proc /proc --dev /dev --tmpfs /tmp --tmpfs /home)
  for d in bin lib lib64 sbin; do
    if [ -L "/$d" ]; then a+=(--symlink "$(readlink "/$d")" "/$d"); elif [ -d "/$d" ]; then a+=(--ro-bind "/$d" "/$d"); fi
  done
  a+=(--ro-bind "$snap" "$snap")
  _sbx_toolchain() {  # bind a venv's base-interpreter tree when it lives outside /usr
    local h; h="$(sed -n 's/^home = //p' "$1/pyvenv.cfg" 2>/dev/null)"
    case "$h" in /usr/*|'') ;; *) a+=(--ro-bind "$(dirname "$(dirname "$h")")" "$(dirname "$(dirname "$h")")") ;; esac
  }
  if [ -d "$top/.venv" ]; then
    venv="$(readlink -f "$top/.venv")"; mkdir -p "$snap/.venv" 2>/dev/null
    a+=(--ro-bind "$venv" "$venv" --ro-bind "$venv" "$snap/.venv")  # its scripts' shebangs name the real path
    _sbx_toolchain "$venv"
    mkdir -p "$out/w/.pth-overlay"
    for p in "$venv"/lib/python*/site-packages/*.pth; do  # an editable install's path line → the snapshot's src
      [ -f "$p" ] && grep -qxE '/[^[:space:]]*/src' "$p" || continue
      printf '%s\n' "$snap/src" > "$out/w/.pth-overlay/${p##*/}" || continue
      a+=(--ro-bind "$out/w/.pth-overlay/${p##*/}" "$p" --ro-bind "$out/w/.pth-overlay/${p##*/}" "$snap/.venv/${p#"$venv"/}")
    done
  fi
  for p in ${SANDBOX_RO:-}; do a+=(--ro-bind "$p" "$p"); [ -f "$p/pyvenv.cfg" ] && _sbx_toolchain "$p"; done
  local pypath="$snap/src" kv; [ -n "${SANDBOX_PYPATH:-}" ] && pypath="$pypath:$SANDBOX_PYPATH"
  local -a env=()
  for kv in ${SANDBOX_ENV:-}; do
    case "${kv%%=*}" in PIP_NO_INDEX|PIP_FIND_LINKS|PYTEST_DISABLE_PLUGIN_AUTOLOAD) env+=(--setenv "${kv%%=*}" "${kv#*=}") ;;
      *) echo "SANDBOX_ENV may not set ${kv%%=*}" >&2; return 125 ;; esac
  done
  local sbx_path=/usr/local/bin:/usr/bin:/bin
  local IFS=:; for p in ${GATES_SANDBOX_RO:-}; do
    [ -n "$p" ] || continue; a+=(--ro-bind "$p" "$p")
    [ -f "$p" ] && [ -x "$p" ] && sbx_path="$sbx_path:${p%/*}"  # a declared binary: only that file, on PATH
  done; unset IFS
  for kv in ${SANDBOX_OVERLAY:-}; do
    [ -e "$snap/${kv#*=}" ] || : > "$snap/${kv#*=}" 2>/dev/null
    a+=(--ro-bind "${kv%%=*}" "$snap/${kv#*=}")
  done
  for d in ${SANDBOX_WRITABLE:-}; do mkdir -p "$out/w/$d" "$snap/$d" 2>/dev/null; a+=(--bind "$out/w/$d" "$snap/$d"); done
  a+=(--bind "$out/io" "$out/io" --chdir "$snap"
    --setenv PATH "$sbx_path" --setenv HOME /tmp --setenv LANG C.UTF-8 --setenv BROKER_MODE demo
    --setenv ALLOW_ORDER_TRANSMIT false --setenv ALLOW_LIVE_TRADING false --setenv PYTHONDONTWRITEBYTECODE 1
    --setenv PYTHONPATH "$pypath" --setenv GATE_IO "$out/io" "${env[@]}")
  bwrap "${a[@]}" true 2>/dev/null || { echo "the bwrap sandbox could not start" >&2; return 125; }
  bwrap "${a[@]}" "$@"
}
sandbox_identity() {  # $1 out, $2 python (inside the sandbox): exit 0 iff chronos comes from the snapshot
  local code='import os, sys, chronos
f, r = os.path.realpath(chronos.__file__), os.path.realpath(os.path.join(os.getcwd(), "src"))
sys.exit(0 if f.startswith(r + os.sep) else 3)'
  sandbox_run "$1" "$2" -c "$code" >/dev/null 2>&1 || return 1
  if [ -n "$(ls -A "$1/w/.pth-overlay" 2>/dev/null)" ]; then sandbox_run "$1" "$2" -I -c "$code" >/dev/null 2>&1 || return 1; fi
}
