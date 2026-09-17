# Unresolved questions

## Skillfile lock Git directory agreement

The pinned `protocol/skillfile-sources.md` contains two sentences whose
application to legacy configured Git entries diverges:

1. “For both Git identity kinds these two directory fields MUST agree.”
2. “For legacy entries the directory is `.` relative to that entry's selected repository.”

The pinned corpus fixture
`conformance/draft-sources-v1/schema-cases/skillfile-lock-v1/valid-configured-git.json`
(curator-spec `8ba9c235ec5be00d52378479516c82386fd0c178`) takes the loose side:
its configured-Git package has `directory: "."` while the member records
`agents/skills/review`, and the case is schema-valid. The committed mirror
`tests/fixtures/skillfile-v2/hand-authored/Skillfile.lock.json` represents the
same interop shape as produced by another conforming manager.

The csk decision for this draft is strict-on-write and tolerant-on-read. New
locks emit `.` for configured-Git members and require network-Git member/package
directories to agree. The tolerance applies to the entire read surface:
`read_lock` and `parse_lock` both accept the hand-authored configured-Git
spelling, and a canonical read → write round trip preserves it byte-exactly.
Strict configured-Git agreement remains available as an explicit opt-in
(`parse_lock(..., strict=True)`) for write-path validation only. A stricter
peer can consume csk-produced locks; a csk reader can consume this peer's
legacy lock without rewriting it during a read.
