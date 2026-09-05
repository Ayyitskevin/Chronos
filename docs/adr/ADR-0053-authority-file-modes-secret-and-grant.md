# ADR-0053 — Authority files have two contracts: SECRET and GRANT

- Status: Accepted (owner review required before merge)
- Date: 2026-09-04
- Deciders: opus seat (author), owner (merge gate)
- Related: R-71, D-68, ADR-0052 (the descriptor-bound read), ADR-0017 (the mandate as
  the grant), ADR-0023 (the proposer registry)

## Context

ADR-0052 / R-67 gave the local API token a descriptor-bound read: one `open` with
`O_NOFOLLOW`, every check run against that descriptor by `fstat`, and the bytes and digest
taken from the same read. It deliberately wired only the token, and named the mandate and
proposer-registry loaders as the follow-up. Both still used `path.read_bytes()`: a symlink
was followed, the path was re-resolved between any check and the read, and the mode was
never looked at at all.

Adopting the helper unchanged would have required those two files to be exactly `0600`,
because that is the rule the token needs. That rule is wrong for them, and wrong in a way
that would have surfaced as an outage rather than a warning: `docs/model_worker.md` tells the
owner to create a mandate with `chronos.cli mandate template > data/autonomy_mandate.json`,
a shell redirect whose mode comes from the umask — `0644` under the common `022`, `0664`
under `002`, and never `0600`. A `0600`-only rule refuses every mandate our own documentation
produces.

The two files are also protecting against different things. The token's *contents* are the
credential: reading it is the compromise. A mandate's contents are an owner-authored
decision; reading it discloses nothing, and the registry stores only credential hashes. What
matters for a grant is that nobody else could have **written** it.

## Decision

`read_authority_file` takes an explicit `mode: AuthorityMode`, and the caller must say which
contract it means. There is no default — the two are not strength levels, and a default
would let a new call site inherit the wrong one silently.

- **`AuthorityMode.SECRET`** — the mode must be exactly `0600`. Unchanged from ADR-0052; the
  local API token is the only user.
- **`AuthorityMode.GRANT`** — the mode must have no group-write and no other-write bit.
  `0600` and `0644` are accepted; `0664`, `0666`, `0620`, `0602`, `0777` are refused.

Every other check is identical in both modes — regular file, `O_NOFOLLOW`, effective-owner
uid, single link, same-descriptor read and digest — because a symlinked or swapped grant is
exactly as dangerous as a swapped secret. The permission rule is the only axis that varies.

`load_persistent_mandate` and `load_proposer_registry` adopt `GRANT`. Both keep their
existing contracts exactly: an absent file still returns `None`, and an invalid document
still returns `None`. Only one thing is new — `UnsafeAuthorityFile` — and it is deliberately
*not* collapsed into `None`, because "this document does not parse" and "this file is not one
this process may trust" are different operator problems. `load_proposer_auth` turns the
second into `ProposerAuth(unsafe=True)`, and the backend's startup notes
`StartupFaultCode.AUTHORITY_FILE_UNSAFE` alongside the existing
`PROPOSER_REGISTRY_INVALID` rather than reporting one word for both.

Nothing is repaired. An unsafe grant produces a CRITICAL line naming the file, its actual
mode, the contract it failed and the fix, and then the system refuses: autonomy stays inert,
or every proposal refuses. Chmodding an owner-authored grant on the owner's behalf would be
the process editing the grant.

The decode moved into the read as well. Every authority file here is text by contract — a
JSON grant or a token — so `AuthorityFileContents` carries `text` alongside `data` and
`sha256`, and a file that is not UTF-8 refuses as `UnsafeAuthorityFile` like every other
unsafe shape. Previously the token path called `bytes.decode(..., errors="strict")` under a
`try` that caught only `UnsafeAuthorityFile`, so a non-UTF-8 token file raised
`UnicodeDecodeError` out of startup as a traceback.

## Consequences

The two grants that decide what this system may do are no longer readable through a symlink,
from a file another account owns, or from a file another account can rewrite. The backend
still boots in every one of those cases — refusing is not crashing — and says exactly what to
fix.

**Operators on a umask-`002` system must `chmod go-w` their mandate and registry**, and
`docs/model_worker.md` now says so at the point of creation. This is the visible cost of the
decision and it is deliberate: the alternative rules were to accept group-writable grants, or
to refuse `0644` and break the documented setup path.

File modes are now load-bearing in the test suite too, so `tests/conftest.py` pins the umask
to `022` for the session. Without it the suite passes in CI and fails on a developer's
machine for a reason unrelated to the change under test. That pin does not hide the rule:
every mode case has its own test that chmods explicitly.

## Rejected alternatives

**Require `0600` for grants too — one rule, fewer concepts.** Rejected: it refuses every
mandate `chronos.cli mandate template > file` produces, which is the command our own
documentation gives. Security theatre with an outage attached.

**Refuse only other-write (`0002`), accepting `0664`.** Rejected: on a machine with shared
groups, group-writable means another account can rewrite the grant. That is the exact threat
this ADR exists for, and "the group usually has one member" is an assumption about the
operator's `/etc/group`, not a property this code can check.

**Give `mode` a default of `SECRET`.** Rejected: the next call site to be added would
inherit the wrong contract by omission, and the failure would look like a permissions bug
rather than a design mistake. The caller states what it means.

**Chmod an unsafe grant into shape and continue.** Rejected: the process would be silently
editing the document that authorizes it, and the operator would never learn that something
had put the file in that state.

**Fold `UnsafeAuthorityFile` into the loaders' existing `None`.** Rejected: it is the one
piece of new information the change produces. Collapsing it would leave startup unable to
tell a typo from a file anyone can rewrite, which is precisely the distinction the typed
fault code exists to record.
