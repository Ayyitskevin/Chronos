# `docs/ops/` — service unit templates

**These are templates. Nothing in this repository installs, enables, or starts any of
them, and CI never touches them.** They exist so an operator copies a reviewed shape
instead of writing a unit from memory.

| File | What it runs | Runbook |
|---|---|---|
| `chronos-backend.service` | the loopback backend on the **demo** broker, for the autonomy SHADOW campaign | [`../SHADOW_CAMPAIGN.md`](../SHADOW_CAMPAIGN.md) §3 |
| `chronos-worker.service` | the model worker on **local** inference, forwarding off | [`../SHADOW_CAMPAIGN.md`](../SHADOW_CAMPAIGN.md) §3 |

## Rules these templates keep, and a test that enforces them

`tests/unit/test_ops_service_templates.py` fails if any of the following stops being
true, so they are enforced rather than asserted:

1. **Every file parses as a systemd INI** with `[Unit]`, `[Service]` and `[Install]`.
2. **No absolute path outside a placeholder.** Every path is `%h/<PLACEHOLDER>/...`.
   A template carrying a real path is a template that was filled in and committed.
3. **No `*_FORWARD=true`.** Forwarding is an owner act (`docs/AGENT_PROTOCOL.md` §9),
   never a shipped default.
4. **No live-capable variable.** `ALLOW_ORDER_TRANSMIT`, `ALLOW_LIVE_TRADING`, an
   `IB_ENVIRONMENT` of `LIVE`, or a `BROKER_MODE` other than `demo` must not appear.
   Absence is the control here; a template is exactly the kind of file where a
   convenience default would survive unread.
5. **The worker unit carries `UnsetEnvironment=CHRONOS_WORKER_FORWARD`.** systemd.exec(5):
   settings from `EnvironmentFile=` *override* `Environment=`, so stating the inert default
   with `Environment=…=false` would not be a guarantee — the private environment file, which
   is not in this repository and which no test can see, would win. `UnsetEnvironment=` is
   applied last and removes the variable outright, so the worker falls back to its own
   default of `False` and turning forwarding on becomes an edit to the reviewed unit.

## Using them

```bash
mkdir -p ~/.config/systemd/user
cp docs/ops/chronos-backend.service docs/ops/chronos-worker.service ~/.config/systemd/user/
# Replace every <PLACEHOLDER>, then create the two 0600 environment files.
systemctl --user daemon-reload
loginctl enable-linger "$USER"     # so the units survive logout
```

Do not `systemctl --user enable` either unit until `docs/SHADOW_CAMPAIGN.md` §5's daily
check runs clean by hand at least once.
