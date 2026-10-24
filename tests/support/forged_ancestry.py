"""Forged ancestry for the D-44 pins: four ways a writer to a clone can lie about a parent edge.

Gate 30 reads ancestry with ``GIT_GRAFT_FILE=/dev/null git -c core.commitGraph=false
--no-replace-objects`` (GATE30-1r1), so the answer comes from the commit objects themselves. The
other authority-bearing ancestry reads (``scripts/post_merge_proof.sh``,
``scripts/verify_release_security.py``) are pinned against the same four arms here:

- ``graph``: a forged ``.git/objects/info/commit-graph`` in the clone (gate 30's A1 arm);
- ``alternate``: a clean clone, with only ``GIT_ALTERNATE_OBJECT_DIRECTORIES`` pointing at a
  directory that holds the forged graph (gate 30's A2 arm);
- ``grafts``: a planted ``.git/info/grafts`` line;
- ``replace``: a ``refs/replace`` graft (``git replace --graft``).

The commit-graph forger is gate 30's (``tests/gates/test_gates_10_30.sh``, GATE30-1r1): it rewrites
one commit's parent edge in a valid v1 SHA-1 graph and re-hashes the file. A forged graph only
reaches commits git loads THROUGH the graph: a commit named on the command line is parsed from its
own object, so the forged commit must be an intermediate one.

The same writer can also lie about a commit's CONTENT, which ``post_merge_proof.sh`` reads to prove
byte equality and the claimed files (A1-F1). ``plant_forged_tree`` makes a commit read as another
commit's content: the graph arms rewrite its root-tree oid in the graph (a revision walk such as
``git diff H MR`` loads a named commit through the graph, so the swap reaches it), and ``replace``
replaces the whole commit. ``grafts`` cannot: a grafts line carries parents, never a tree.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import struct
import subprocess
from collections.abc import Callable
from pathlib import Path

ARMS = ("graph", "alternate", "grafts", "replace")
GRAPH_ARMS = ("graph", "alternate")

#: Every pin checks that its forgery changes the raw command's answer on this git first. If it no
#: longer does, the pin proves nothing and fails with this message rather than passing.
DEAD = "forged ancestry no longer changes the raw git answer on this git; re-examine this pin"


def raw_git(
    repository: Path, extra_env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    """The unhardened command (no flags, the arm's env): what each DEAD control asks first."""

    return subprocess.run(
        ("git", "-C", str(repository), *args),
        env={**os.environ, **extra_env},
        capture_output=True,
        text=True,
        check=False,
    )


def forge_parent_edge(graph: Path, target: str, forged_parent: str) -> None:
    """Rewrite ``target``'s first parent to ``forged_parent`` in the commit-graph file ``graph``."""

    data, oids, record = _read_graph(graph)
    t, par = record(oids.index(target)), record(oids.index(forged_parent))
    data[t + 20 : t + 24] = struct.pack(">I", oids.index(forged_parent))
    data[t + 24 : t + 28] = struct.pack(">I", 0x70000000)
    level = struct.unpack(">I", data[par + 28 : par + 32])[0] >> 2
    data[t + 28 : t + 32] = struct.pack(">I", (level + 5) << 2)
    date = struct.unpack(">I", data[par + 32 : par + 36])[0]
    data[t + 32 : t + 36] = struct.pack(">I", date + 1000)
    _write_graph(graph, data)


def forge_root_tree(graph: Path, target: str, forged_tree: str) -> None:
    """Rewrite ``target``'s root-tree oid to ``forged_tree`` in the commit-graph file ``graph``."""

    data, oids, record = _read_graph(graph)
    t = record(oids.index(target))
    data[t : t + 20] = bytes.fromhex(forged_tree)
    _write_graph(graph, data)


def _read_graph(graph: Path) -> tuple[bytearray, list[str], Callable[[int], int]]:
    data = bytearray(graph.read_bytes())
    assert data[:4] == b"CGPH" and data[4] == 1 and data[5] == 1, "unsupported commit-graph format"
    chunks: dict[bytes, int] = {}
    for i in range(data[6] + 1):
        (offset,) = struct.unpack(">Q", data[12 + 12 * i : 20 + 12 * i])
        chunks[bytes(data[8 + 12 * i : 12 + 12 * i])] = offset
    count = struct.unpack(">I", data[chunks[b"OIDF"] + 1020 : chunks[b"OIDF"] + 1024])[0]
    oids = [
        bytes(data[chunks[b"OIDL"] + 20 * i : chunks[b"OIDL"] + 20 * i + 20]).hex()
        for i in range(count)
    ]

    def record(index: int) -> int:
        return chunks[b"CDAT"] + 36 * index

    return data, oids, record


def _write_graph(graph: Path, data: bytearray) -> None:
    data[-20:] = hashlib.sha1(bytes(data[:-20])).digest()
    graph.chmod(stat.S_IRUSR | stat.S_IWUSR)
    graph.write_bytes(data)


def plant_forged_parent(
    repository: Path, arm: str, *, target: str, forged_parent: str
) -> dict[str, str]:
    """Make ``target``'s parent read as ``forged_parent`` by ``arm``; return the env the arm needs.

    Both commits must be reachable from a ref (the graph arms write ``--reachable``).
    """

    def git(*args: str) -> None:
        subprocess.run(("git", "-C", str(repository), *args), check=True, capture_output=True)

    git_dir = repository / ".git"
    if arm in GRAPH_ARMS:
        return _plant_graph(
            repository, arm, lambda graph: forge_parent_edge(graph, target, forged_parent)
        )
    if arm == "grafts":
        (git_dir / "info").mkdir(exist_ok=True)
        (git_dir / "info" / "grafts").write_text(f"{target} {forged_parent}\n", encoding="utf-8")
        return {}
    if arm == "replace":
        git("replace", "--graft", target, forged_parent)
        return {}
    raise ValueError(f"unknown arm {arm!r}")


def plant_forged_tree(repository: Path, arm: str, *, target: str, forged: str) -> dict[str, str]:
    """Make ``target`` read as ``forged``'s content by ``arm``; return the env the arm needs.

    Both commits must be reachable from a ref. The graph arms swap ``target``'s root tree for
    ``forged``'s; ``replace`` replaces ``target`` with ``forged`` outright.
    """

    def git(*args: str) -> str:
        result = subprocess.run(
            ("git", "-C", str(repository), *args), check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    if arm in GRAPH_ARMS:
        tree = git("rev-parse", f"{forged}^{{tree}}")
        return _plant_graph(repository, arm, lambda graph: forge_root_tree(graph, target, tree))
    if arm == "replace":
        git("replace", target, forged)
        return {}
    raise ValueError(f"arm {arm!r} cannot forge a commit's content")


def _plant_graph(repository: Path, arm: str, forge: Callable[[Path], None]) -> dict[str, str]:
    subprocess.run(
        ("git", "-C", str(repository), "commit-graph", "write", "--reachable"),
        check=True,
        capture_output=True,
    )
    graph = repository / ".git" / "objects" / "info" / "commit-graph"
    forge(graph)
    if arm == "alternate":
        alternate = repository.parent / f"{repository.name}-alternate"
        (alternate / "info").mkdir(parents=True)
        shutil.move(graph, alternate / "info" / "commit-graph")
        assert not graph.exists()
        return {"GIT_ALTERNATE_OBJECT_DIRECTORIES": str(alternate)}
    return {}
