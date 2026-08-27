# Decision 0001: normative boundaries

## Context

The original draft combined portable wire contracts with one implementation's
CLI, machine-home paths, and environment variables. This made two independently
conforming managers appear incompatible even when they exchanged identical
project state.

## Decision

Portable filenames, JSON objects, exact bytes, resolution algorithms, and
project installation artifacts are normative protocol. Machine-local state,
command spelling, global environment variables, cache layout, and UI are an
implementation profile or informative guide.

Schemas, prose, and released vectors are authoritative in that order only for
their respective structural, semantic, and exact-byte concerns. Conflicts are
specification defects. Implementations are never normative oracles.

## Consequences

Curator and csk may use different machine homes and command names while
remaining interoperable. A conformance claim requires the shared suite rather
than behavioral comparison with the reference implementation.
