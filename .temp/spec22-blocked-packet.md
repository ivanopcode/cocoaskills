# TASK-260826-29y82b spec22-pin-delta: deferred, evidence packet

The delta was started on a wrong premise and is reverted; the working
tree matches HEAD for docs/external-build-repositories.md.

Constraint: the doc header must mirror the ACCEPTED protocol revision
owned by src/csk/build_repository.py (PROTOCOL_VERSION = "1.0.0-rc.5",
CONFORMANCE_MANIFEST_SHA256 = b6f56aac...). The CI RELEASED_SUITE_PIN
(0ed5c691, curator-spec v1.0.0-rc.9) pins the released TEST SUITE the
manager is qualified against, not the accepted revision. At rc.5 the
pinned-agent form is not yet a normative third canonical
authentication-tail form, so citing curator-spec#22 as source is wrong
until the code accepts a revision that contains it.

Resume trigger (exact): a commit changing PROTOCOL_VERSION in
src/csk/build_repository.py away from 1.0.0-rc.5. Then rerun this task:
update the header version/hex/manifest SHA (derive the SHA from the
newly accepted revision, literal shasum output required) and re-source
the pinned-agent wording to the spec.

Useful evidence already banked: at rc.9 (0ed5c691) the
conformance/v1/manifest.json SHA-256 is
803918bf8672f76cf990985e51db213b826674cd5bb54fbf47731b8404b44403,
derived independently twice (writer and orchestrator).
