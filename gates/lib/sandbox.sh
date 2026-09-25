# gates/lib/sandbox.sh — the ONE trusted launcher for candidate code. Sourced (never executed) by the
# gates that run code from the PR (10, 40, 60, 70); it comes from the trusted gate set like they do.
#   sandbox_snapshot <out>          clone the lane's HEAD into <out>/snap (the immutable exact-head tree)
#   sandbox_run <out> <cmd...>      run <cmd> in bwrap: no network, new pid namespace and session, cleared
#                                   environment (the fixed allowlist + PYTHONPATH=<snap>/src), tmpfs /home
#                                   and /tmp, read-only /usr /etc + the Python toolchain + the lane venv +
#                                   the snapshot; the ONLY writable paths are <out>/io and the snapshot
#                                   output dirs named in SANDBOX_WRITABLE (each backed by <out>/w/<name>).
#   sandbox_identity <out> <python> fail unless `chronos.__file__` resolves under <snap>/src, in the sandbox
# A missing bwrap or a sandbox that cannot start is an error for the caller to FAIL on — never a fallback.
# GATES_SANDBOX_RO (runner-set, colon-separated absolute paths) adds read-only binds for a toolchain
# outside /usr; the lane leaves it unset.
sandbox_snapshot() {
  local top sha; top="$(git rev-parse --show-toplevel)" && sha="$(git -C "$top" rev-parse HEAD)" || return 1
  git clone -q --no-checkout "$top" "$1/snap" 2>/dev/null && git -C "$1/snap" checkout -q --detach "$sha" 2>/dev/null || return 1
  mkdir -p "$1/io" "$1/w" && chmod 0700 "$1" "$1/io"
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
  if [ -d "$top/.venv" ]; then
    venv="$(readlink -f "$top/.venv")"; mkdir -p "$snap/.venv" 2>/dev/null
    a+=(--ro-bind "$venv" "$venv" --ro-bind "$venv" "$snap/.venv")  # its scripts' shebangs name the real path
    home="$(sed -n 's/^home = //p' "$venv/pyvenv.cfg" 2>/dev/null)"
    case "$home" in /usr/*|'') ;; *) a+=(--ro-bind "$(dirname "$(dirname "$home")")" "$(dirname "$(dirname "$home")")") ;; esac
  fi
  local IFS=:; for p in ${GATES_SANDBOX_RO:-}; do [ -n "$p" ] && a+=(--ro-bind "$p" "$p"); done; unset IFS
  for d in ${SANDBOX_WRITABLE:-}; do mkdir -p "$out/w/$d" "$snap/$d" 2>/dev/null; a+=(--bind "$out/w/$d" "$snap/$d"); done
  a+=(--bind "$out/io" "$out/io" --chdir "$snap"
    --setenv PATH /usr/local/bin:/usr/bin:/bin --setenv HOME /tmp --setenv LANG C.UTF-8 --setenv BROKER_MODE demo
    --setenv ALLOW_ORDER_TRANSMIT false --setenv ALLOW_LIVE_TRADING false --setenv PYTHONDONTWRITEBYTECODE 1
    --setenv PYTHONPATH "$snap/src" --setenv GATE_IO "$out/io")
  bwrap "${a[@]}" true 2>/dev/null || { echo "the bwrap sandbox could not start" >&2; return 125; }
  bwrap "${a[@]}" "$@"
}
sandbox_identity() {  # $1 out, $2 python (inside the sandbox): exit 0 iff chronos comes from the snapshot
  sandbox_run "$1" "$2" -c 'import os, sys, chronos
f, r = os.path.realpath(chronos.__file__), os.path.realpath(os.path.join(os.getcwd(), "src"))
sys.exit(0 if f.startswith(r + os.sep) else 3)' >/dev/null 2>&1
}
