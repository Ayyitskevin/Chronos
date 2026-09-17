# Off-host alert sidecar — DESIGN ONLY (receive-only protocol sketch; no placement decision)

Status: design, 2026-09-14 (M3 slice). Nothing here runs. Two decisions are Kevin's and are not made here: **where the sidecar lives**
(it must be a separately administered host to mean anything) and **backup-encryption key custody** (see the restore-drill runbook once it
lands). This document defines the envelope the local M3 layers will be carried in off-host. Of the producers it names, exactly one exists at
this head — the audit log's head anchor (`src/chronos/auditlog/log.py`); the watchdog, dead-man checker and restore-drill harness are
PLANNED (packets W-1 and R-1, in flight on this base) and `docs/limitations.md` still records them as not implemented. The record table
below marks each producer `present` or `planned`; a contract test (`tests/unit/test_ops_design_sidecar_contract.py`) holds those labels
to the source tree and holds the anchor schema stated here to the bytes the log actually writes. The purpose is that the risk the roadmap
names ("the first thing in Chronos that is not local-first") is bounded by protocol before anything is placed.

## The one property that matters: receive-only
The sidecar never calls the Chronos host, never holds a credential for it, never issues a command, and never becomes an input to any
authority decision. It receives typed records, stores them append-only, and can be read by an operator.

Failure model, stated exactly:
- A **compromised sidecar** cannot control the host and cannot forge host-signed records (the signature covers the canonical bytes, below).
  It CAN suppress or delete the off-host copy, present a false operator timeline, and suppress or fabricate its own sidecar-originated
  alerts. The isolation boundary is "no host control, no host-record forgery" — not "nothing else". Detecting a lying sidecar is the
  operator's cross-check against the host's local files, and an argument for a second receiver later; it is not solved here.
- A **down sidecar** is noticed by the host's sender as transport failure (non-202, timeout): the sender records a local gap and retries
  with backoff. That is telemetry only — it never changes trading or authority behaviour. The sidecar cannot record its own outage
  contemporaneously; a gap in its timeline is inferred afterwards from `producer_seq` discontinuities and receipt times, never asserted.

## What is carried (record kinds; `present` = the producer exists at this head, `planned` = R-1)
| kind | local producer | status | why it is worth carrying |
|---|---|---|---|
| `audit.head` | the audit log's head anchor `<stem>.head.json` — its bytes are exactly `{"count": 7, "last_hash": "<64 hex>"}` (a JSON object with those two keys and nothing else) (`src/chronos/auditlog/log.py`, `_anchor_bytes`); it carries NO timestamp | present | the off-host audit-head receipt named in D1 PR-4: a later chain rewrite on the host cannot rewrite the copy |
| `watchdog.observation` | `chronos.operations.watchdog` — one JSON line per tick | present | an off-host copy shows a gap when the host stops observing itself |
| `watchdog.verdict` | `chronos.operations.watchdog` — `TRIPPED`/`HEALTHY` transitions | present | the trip an operator must see even if the host is gone |
| `deadman.verdict` | `chronos.operations.deadman` | present | the layer that catches a dead watchdog |
| `backup.manifest` | `chronos.operations.restore_drill` (`BackupManifest`) | planned (R-1) | proves a backup existed with a given digest at a given time |
| `drill.report` | `chronos.operations.restore_drill` (`DrillReport` with measured `rpo_s` / `rto_s`) | planned (R-1) | the measured numbers, kept where a host loss cannot erase them |
Two layers of bytes, kept distinct: the **producer payload** is whatever the local file holds (for `audit.head`, exactly the two-key anchor
object); the **sidecar envelope** adds `schema_version: 1`, `host_id`, `head_sha`, `kind`, `producer_seq`, `sent_at` (host UTC, a claim),
`payload`, and then `record_id` and `signature` (defined below). No payload carries an order, a position, an account id, a credential or a mandate.

## Transport sketch (push from the host; the sidecar only listens)
- HTTPS `POST /v1/records` from the host to the sidecar, body = one record or a JSONL batch (≤ 1 MiB), header `Authorization: Bearer <host-write-token>`.
  The token is **write-only** and per host: it can append records and do nothing else; it is not a Chronos credential and grants nothing on the host.
- The sidecar answers `202 {received: n, first_seq, last_seq}` after fsync; any other answer is a *transport gap* the host records locally and retries
  with backoff — never a reason to change host behaviour.
- Canonical bytes: `canonical = json.dumps(envelope_without_record_id_and_signature, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`
  — the same rule the audit anchor already uses (sorted keys, one representation). `record_id = sha256(canonical)`, encoded as 64 lowercase
  hex characters; `signature = Ed25519.sign(host_private_key, canonical)`, encoded as unpadded base64url of the 64 raw signature bytes; both are
  then added as string fields. The RECEIVER parses the envelope, drops the two fields, re-canonicalizes, RECOMPUTES `record_id` (a digest anyone
  can compute) and VERIFIES `signature` against the host's PUBLIC key (a signature only the host can produce; the receiver never recomputes it);
  a record failing either check is refused. Identity is therefore independent of the sender's JSON spelling and of retries.
- Idempotency: the sidecar deduplicates by `record_id`; a retried record is stored once; a record with the same `(host_id, kind, producer_seq)`
  but a different `record_id` is stored AND flagged as a producer conflict (never silently replaced).
- Integrity: the per-host Ed25519 key's PUBLIC half is held by the sidecar; the private half lives in the host's untracked per-machine `.env` (R2)
  — the sidecar cannot forge host records; a host key rotation is a new `host_id` epoch, never a rewrite of old records.
- Ordering and time: `producer_seq` is monotonic per `(host_id, kind)` and is the ordering authority; every host timestamp (`sent_at`, any
  `assessed_at` inside a payload) is a CLAIM. The sidecar stamps `received_at` from its own clock, and its own clock is the ONLY time authority
  for sidecar-side dead-man decisions (below); the sidecar stores gaps as gaps.
- Read side: `GET /v1/records?host=…&kind=…&since=…` with an operator READ token (separate from the write token), and one HTML timeline for humans.
  No endpoint on the sidecar can reach the host; there is no `/v1/commands`.

## Trip semantics on the sidecar side (later work; named now so the local layers fit it)
The sidecar keeps, per host, the `received_at` (its own clock) of the last `watchdog.observation` whose payload says `state=HEALTHY`. If that
age — measured on the sidecar's clock, never on host-claimed timestamps — exceeds an operator-set outer deadline,
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
