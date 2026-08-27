# Independent review evidence

Stable protocol releases require two public review reports under
`reviews/<version>/`:

- `security.json` for the registry and manager security model;
- `interoperability.json` for independent implementation and shared-suite
  interoperability.

Each stable report conforms to `review-report-v2.schema.json`. The reviewer
attests that they are neither a maintainer of this specification nor an author
or committer of the reviewed normative changes. Affiliation and any relevant
conflict of interest are disclosed in the report. `contact` is a stable public
reviewer identity and MUST differ between the two reports. The `source_url`
points to the public review, issue, or pull request that establishes reviewer
authorship and discussion history.

Reviewers inspect a stable-version candidate commit before reports are added.
After that commit, only files below `reviews/` may change before the release
tag. Release CI verifies ancestry and this diff restriction for both reports.
Open critical or high findings, a conditional conclusion, and a failed report
all block the stable release.

The examples directory is validation input, not release evidence.

Schema v2 adds the mandatory `project_maintainer` and
`authored_reviewed_changes` attestations. The original
`review-report.schema.json` remains available as schema v1 for already-created
draft evidence, but v1 reports do not satisfy the stable release gate.
