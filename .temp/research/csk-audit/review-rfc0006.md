# Review: RFC 0006 (v0.8 audit LLM backends) — gaps

Date: 2026-06-17. Strong RFC; absorbed prior reviews. Below are genuine gaps
("что упустили"), prioritized. Most need a decision in the RFC before implementing.

## Well-handled (so the verdict is balanced)
- Hermetic codex: empty temp cwd, skill files NOT materialized as files, prompt on
  stdin (not argv), --ephemeral, --ignore-rules, --skip-git-repo-check. Closes the
  "skill's own AGENTS.md hijacks the auditor" vector.
- Malformed/invalid backend output = backend failure, not empty-findings-allow.
  (§5, §8, §13). This was the critical silent-bypass risk and it is nailed.
- Cloud gated by BOTH source classification AND redaction (§6, §13) — correct
  defense in depth, classification first line, redaction second.
- Real secret-pattern redaction for cloud file content (PEM, auth headers,
  high-entropy, env values), not URL-only (§11).
- Timeout plumbing (§12), prompt versioning (§10/§12), cache key semantics (§14).

## G1 (High) — unverifiable LLM findings will gate; contradicts RFC 0005 §10
Confirmed in code: `policy.decide` gates on ALL findings >= threshold and never
filters `verifiable` (it appears only in pipeline.py:238 serialization). v0.7 was
safe because every static finding is `verifiable=True`. RFC 0006 introduces LLM
findings that can be `verifiable=false` / hallucinated, but does not reconcile this
with RFC 0005 §10 ("unverifiable findings are logged but do not gate"). Result: a
hallucinated high-severity LLM finding BLOCKS installs under strict. This breaks the
core property the whole design rests on — "the LLM is advisory; its hallucination
cannot block." Fix: filter `verifiable=false` out of the deterministic gate (keep
them in the report/JSON as advisory), or explicitly accept LLM-gates-and-can-false-
positive and document the pin path as the relief. Decide in the RFC.

## G2 (High) — extra_args can override the hard security flags
§6 allows codex `extra_args` (free string list, only "disallowed when cloud=true").
§9 makes `--sandbox read-only` / `--ask-for-approval never` hard constraints. But
extra_args is appended to the codex argv, so a local
`extra_args: ["--sandbox","danger-full-access"]` (or `--search`, `--cd <skill-dir>`,
`--full-auto`) overrides the locked flags by last-wins and breaks the sandbox /
no-network / no-skill-in-cwd invariants. Fix: validate extra_args against a denylist
of security-relevant flags (`--sandbox`, `--ask-for-approval`, `--search`, `--cd`,
`--output-*`, `--full-auto`, `--oss` if it changes egress, ...) and reject if
present — regardless of cloud.

## G3 (Medium-High) — cloud=false for codex is an unguarded assertion → silent egress
§9: "cloud=false means the operator asserts the configured profile/provider does not
send content to a cloud provider." So `{kind:codex, model:"gpt-5", cloud:false}` (no
oss/local_provider) is treated as local: raw file content, no source-classification
gate — but it egresses to OpenAI. The footgun is one missing flag away from the
`codex-cloud` example. Fix: when `cloud=false` for codex, require `oss=true` or
`local_provider` set (you cannot claim local without declaring how it is local),
else config error or loud warning.

## G4 (Medium) — network-egress invariant is implicit; local-provider interaction unspecified
(a) The RFC relies on `--sandbox read-only` to imply no network egress, but never
states "no network egress from the audit sandbox" as an explicit hard constraint or
test. State and test it — it is the property protecting against injection-driven
exfiltration. (b) Unspecified: how does codex reach a local Ollama (localhost:11434)
under `read-only`? If read-only blocks localhost network, the local-provider model
call cannot happen and the whole local-first path is broken; if the model API call
is outside the sandbox (only tool-exec sandboxed), say so. This is a practical
blocker for the headline local-first story.

## G5 (Medium) — oversize/truncation = silent partial audit = bypass
§20 leaves `max_request_bytes` open but does not define what happens to content that
exceeds the prompt budget. Silently auditing a truncated subset is a bypass (a
payload in the dropped tail is never seen). Define now: on oversize, fail closed
under strict / emit a finding; never silently audit a partial set.

## G6 (Low-Med) — LLM backend canary named but not designed or tested
§4 says "adds backend canary for command/codex", but §17 release-blocking tests use
a FAKE codex executable, so the canary's real job ("does the actual backend flag a
known-malicious fixture, else fail closed") is never exercised. The canary is
inherently weaker for an LLM backend (expensive, flaky to run on every audit).
Define its mechanism, or gate codex behind the feature flag from open-question 1
until a real local-provider smoke exists.

## G7 (Low) — backend extraction is sequential
The v0.7 `audit_plans` loop is sequential; with codex (~60s/skill) a first install
of an N-skill project is N×60s (cache only helps steady-state). RFC 0005 §17 said
"in parallel"; consider bounded parallelism for backend calls, or note the UX.

## G8 (Low) — the redaction-applied synthetic finding can itself block
`audit.redaction.applied` is LOW (§11). Under `fail_on=low` + strict it would BLOCK
on its own — provenance metadata causing a block. Make it INFO or exclude
metadata-category findings from gating.

## G9 (Low) — local backends receive raw content; local logging residual
By design (§5) local backends get raw bytes, so codex/Ollama/the command may persist
raw secrets in their own logs. `--ephemeral` covers codex sessions, not Ollama or a
custom command's logs. Acceptable (own machine) but state the residual explicitly.

## Bottom line
The architecture is right and the hard injection/egress vectors are mostly closed.
Before implementing, resolve G1 (verifiable gating — it changes the gate's trust
model), G2 (extra_args bypass), G3 (cloud=false footgun). G4/G5 should be specified
now rather than discovered in code. G6-G9 are polish.
