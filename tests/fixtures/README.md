# Release provider fixtures

`real-release-run-list.json` and `real-distribution-smoke-run-list.json` each
contain one row captured with `gh run list` on 2026-09-29. The smoke row has
`headBranch: main` and the same `headSha` as the release row; its `createdAt`
is after the release row's `startedAt`. The fixtures retain only provider run
metadata returned by `--json databaseId,event,headBranch,headSha,createdAt,
startedAt,url`; they contain no credentials or response headers.
