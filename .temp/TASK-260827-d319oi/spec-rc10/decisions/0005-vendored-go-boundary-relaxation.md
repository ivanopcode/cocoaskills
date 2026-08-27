# Decision 0005: vendored Go boundary relaxation

## Context

`go-v1` as shipped in `manager:rc2` enforces a closed vendor-only pipeline with a fixed `go list`/`go build` graph. Real vendored skills (e.g. `skill-project-management` with `tools/board-cli`/`tools/board-tui`) hit four false positives that are not package-controlled code execution:

1. `GOROOT/src/vendor/golang.org/x/*` reports 15 `Standard==true && Goroot==true` packages with `ImportPath=vendor/golang.org/x/...` and `Root==""` under Go 1.25. The profile requires `Root==GOROOT` for trusted packages.
2. `coder/websocket` vendors pure Go assembly (`mask_amd64.s`/`mask_arm64.s` with `SFiles`) — rejected as non-standard `SFiles` but contains no `CgoFiles`/`CFiles`/host objects and is already hashed via `curator-build-source-v1`.
3. `golang.org/x/sys` vendors `zsyscall_darwin_arm64.go` containing `//go:cgo_import_dynamic` for syscall trampolines — rejected as exact-bytes `cgo_import_dynamic` but is an audited `x/sys` pattern, not arbitrary dynamic import.
4. Many vendored deps contain `//go:generate` comments (`clipperhouse/displaywidth`, `x/text`, `chroma`, etc.) — rejected as generator but `curator` never runs `go generate`; `go list -mod=vendor` + `go build -mod=vendor` do not execute generators and vendor is already materialized. The directive is inert.

`curator:rc2` hotfixed `internal/godriver/graph.go` to `Root=="" && vendor/` allow, `if false && SFiles`, `x/sys` allowlist, `if false && go:generate`. This is broader than the intended trust boundary and diverges from `cocoaskills:main` (`src/csk/builds/go_v1.py:852/1011`) which still enforces the strict profile.

## Decision

Keep `go-v1` closed and vendor-only, but document four **vendored exceptions** as the intended boundary. Update `profiles/manager.md:2.3` to:

- Trusted `Standard+Goroot` packages remain below `GOROOT`, but `GOROOT/src/vendor` packages with `ImportPath` prefix `vendor/` and `Root==""` are accepted as trusted when `Standard==true && Goroot==true` and the directory is below `GOROOT`.
- `SysoFiles` remains empty for all results. For non-standard results, `CgoFiles`/`CFiles`/`CXXFiles`/`MFiles`/`HFiles`/`FFiles`/`SwigFiles`/`SwigCXXFiles` remain empty. `SFiles` is allowed **only** for vendored non-standard packages with no cgo/C/SWIG inputs and no host objects, where the `SFiles` are regular files below the build root and the package is hashed via `curator-build-source-v1` (i.e. pure Go assembly, e.g. `coder/websocket` masks).
- `//go:cgo_import_dynamic` remains rejected for non-standard `GoFiles` except for the audited allowlist `golang.org/x/sys` (and `golang.org/x/sys/*`). No other package may contain the directive.
- `//go:generate` in `GoFiles` is not a build input; managers MUST NOT run generators. The presence of the comment in vendored `GoFiles` does not fail preflight because vendor is already materialized and `go build -mod=vendor` does not execute generators.

The artifact is still never executed during install, the snapshot remains frozen, and the build input still binds `curator-build-source-v1` + `curator-go-toolchain-v1` + fixed policy, so the hotfix does not introduce RCE — even a broad disable would not be RCE, but the narrow allowlists preserve the closed profile for future drivers.

## Consequences

- `curator-spec:rc4` will carry the relaxed `manager.md:2.3` and this decision. `curator:rc3` and `cocoaskills` `go-v1` driver must implement the same four vendored exceptions (allowlist for `SFiles` and `cgo_import_dynamic`, inert `go:generate`, `GOROOT/src/vendor` `Root`).
- Skills vendoring `x/sys`, `coder/websocket`, and generators (as `skill-project-management` does) will pass `csk skill check` / `curator skill check` and install without a per-skill workaround.
- Future drivers remain closed by `decisions/0004` rule: broadening `go-v1` does not enable generic execution.

## Alternatives

- Keep strict `manager.md` and require skills to avoid `x/sys`/`coder/websocket`/generators: rejected — impractical and forces forks of widely audited deps.
- Broad `if false` for `SFiles`/`go:generate` in both managers: rejected — expands trust boundary beyond the vendored, audited cases.

## Security impact

The narrow exceptions do not add code execution during manager activity. `curator-build-source-v1` still hashes every vendored file, the process graph remains fixed (`go list`/`go build` only), `CGO_ENABLED=0`, `GOPROXY=off`, and the artifact is never executed for validation. Host-object and external-link bans remain.
