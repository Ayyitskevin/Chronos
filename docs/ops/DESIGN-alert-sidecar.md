# Off-host alert sidecar — DESIGN ONLY (receive-only protocol sketch; no placement decision)

Status: design, 2026-09-14 (M3 slice). Nothing here runs. Two decisions are Kevin's and are not made here: **where the sidecar lives**
(it must be a separately administered host to mean anything) and **backup-encryption key custody** (see RESTORE-DRILL.md). This document
exists so that the local layers built tonight — the watchdog's evidence files and the dead-man checker (WATCHDOG.md), the backup
manifests and drill reports (RESTORE-DRILL.md), and the audit-chain head anchor (`<stem>.head.json`) — have a defined shape to be
carried off-host later, and so the risk the roadmap names ("the first thing in Chronos that is not local-first") is bounded by protocol,
not by hope.

## The one property that matters: receive-only
The sidecar never calls the Chronos host, never holds a credential for it, never issues a command, and never becomes an input to any
authority decision. It receives typed records, stores them append-only, and can be read by an operator. If the sidecar is compromised,
the attacker gets a copy of evidence and nothing else. If the sidecar is down, Chronos does not notice and does not change behaviour;
the loss is observability, recorded as a gap in the sidecar's own timeline, never as a change on the host.

## What is carried (record kinds, all already local files tonight)
| kind | local producer | why it is worth carrying |
|---|---|---|
| `watchdog.observation` | `chronos.operations.watchdog` — one JSON line per tick (`data/ops/watchdog.jsonl`) | an off-host copy shows a gap when the host stops observing itself |
| `watchdog.verdict` | same — `TRIPPED`/`HEALTHY` transitions | the trip an operator must see even if the host is gone |
| `deadman.verdict` | `chronos.operations.deadman` (run by a timer, later by the sidecar side itself) | the layer that catches a dead watchdog |
| `audit.head` | the audit log's head anchor `<stem>.head.json` (hash, sequence, written_at) | the off-host audit-head receipt named in D1 PR-4: a later chain rewrite on the host cannot rewrite the copy |
| `backup.manifest` | `chronos.operations.restore_drill` (`BackupManifest`) | proves a backup existed with a given digest at a given time |
| `drill.report` | same (`DrillReport` with measured `rpo_s` / `rto_s`) | the measured numbers, kept where a host loss cannot erase them |
Everything is JSON, schema-versioned (`schema_version: 1`), and carries `host_id`, `head_sha` (the running code), `written_at` (UTC),
and `monotonic_ns` where the producer has one. No record carries an order, a position, an account id, a credential or a mandate.

## Transport sketch (push from the host; the sidecar only listens)
- HTTPS `POST /v1/records` from the host to the sidecar, body = one record or a JSONL batch (≤ 1 MiB), header `Authorization: Bearer <host-write-token>`.
  The token is **write-only** and per host: it can append records and do nothing else; it is not a Chronos credential and grants nothing on the host.
- The sidecar answers `202 {received: n, first_seq, last_seq}` after fsync; any other answer is a *transport gap* the host records locally and retries
  with backoff — never a reason to change host behaviour.
- Idempotency: each record carries `record_id` = sha256(host_id ‖ kind ‖ producer_seq ‖ body); the sidecar deduplicates by it, so retries are safe.
- Integrity: each record is signed by the host with a per-host Ed25519 key whose PUBLIC half the sidecar holds; the private half lives in the host's
  untracked per-machine `.env` (R2) — the sidecar cannot forge host records, and a host key rotation is a new `host_id` epoch, never a rewrite.
- Ordering: `producer_seq` is monotonic per (host_id, kind); the sidecar stores gaps as gaps.
- Read side: `GET /v1/records?host=…&kind=…&since=…` with an operator READ token (separate from the write token), and one HTML timeline for humans.
  No endpoint on the sidecar can reach the host; there is no `/v1/commands`.

## Trip semantics on the sidecar side (later work; named now so the local layers fit it)
The sidecar keeps, per host, the time of the last `watchdog.observation` with `state=HEALTHY`. If that age exceeds an operator-set outer deadline,
the sidecar raises its own `sidecar.deadman` record and an always-on alert (mail/Telegram — *from the sidecar host*, an outbound send that is RED
per fleet rules and therefore an owner decision when the sidecar is placed). This is the dead-man that survives the host: the local checker
(`chronos.operations.deadman`) catches a dead watchdog on a live host; the sidecar catches a dead host.

## What this design refuses
- No placement (mickey, a VPS, a phone) — Kevin's; each has a different trust and administration story and that IS the decision.
- No host-side inbound listener, no remote restart, no "sidecar tells the host to halt": a compromised or mistaken sidecar must not be able to move
  the trading process. Halting stays a local, operator-initiated action (docs/ops/OB2-B runbook).
- No encryption-at-rest decision for the carried backups: the sidecar carries *manifests* (digests), not backup bytes, until key custody is decided.
- No new authority input: nothing the sidecar knows feeds admission, mandates, or the order plane (the same rule the health projection follows).

## Open owner asks (verbatim for Kevin Briefing)
1. Sidecar placement: which separately administered host, who administers it, what its outbound alert channel is.
2. Backup encryption: algorithm is a detail; **who holds the key** and where it is stored is the decision (see RESTORE-DRILL.md).
3. Retention and access for the off-host evidence (how long, who can read).
