# Operating assurance modes

Curator defaults to `portable`. Existing commands and configuration therefore
retain rc.6 behavior unless an operator explicitly requests `verified`.

Use portable mode when the CLI-only `manager-worker-v1` guarantees are
appropriate. Capability observations in this mode are honest diagnostics, not
proof of the six verified capabilities.

Use verified mode only after installing a trusted host provider separately from
all skills and configuring its expected provider id, version policy, signature
trust, and binary SHA-256. The same `host-execution-provider-v1` messages apply
on macOS, Linux, and Windows; consult the provider's own qualification record
for platform mechanisms and native evidence. This release does not ship or
endorse a provider binary.

Operationally:

1. Install or update the provider through the host's trusted administrative
   channel, never through `curator install` and never inside a skill directory.
2. Verify the configured provider identity before selecting verified mode.
3. Request verified mode explicitly. Treat any preflight failure as terminal
   for that operation; do not automate a portable retry.
4. Keep verified cache entries and checkpoints in separate namespaces from
   portable cache and journals.
5. After provider or policy updates, expect cache misses and fresh negotiation.
6. To return to portable behavior, start a separate explicitly portable
   operation. Do not relabel verified or portable records.

For migration, no action is required for existing portable installations.
Historical receipts, markers, cache entries, and claims preserve their original
meaning. They cannot satisfy verified requirements. Removing a provider makes
verified operations fail before execution and leaves portable mode available
only when separately selected.
