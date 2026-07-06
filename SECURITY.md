# Security Policy

Translations: [Русский](SECURITY.ru.md). English is the source of truth.

## Supported versions

Security fixes land on the latest release line.

| Version | Supported |
|---|---|
| 0.9.x | yes |
| < 0.9 | no; upgrade to the latest release |

## Reporting a vulnerability

Report vulnerabilities privately through GitHub:
[Security advisories](https://github.com/ivanopcode/cocoaskills/security/advisories/new).
Please keep vulnerability details out of public issues and pull requests until
a fix is released.

Include what you can: the affected version, a reproduction, the impact you
see, and a suggested fix if you have one. The project is maintained on a
best-effort basis; expect an initial response within a week.

## Scope

Reports of particular interest:

- Writing outside designated directories during install (path traversal
  through manifests, archives, or names).
- Command execution through manifest content, git URLs, or skill archives.
- Bypasses of the source allowlist, the audit gates, or the trust workflow.
- Prompt-context contamination: repository content reaching the agent window
  past the whitelist.
- Secret exposure in logs, reports, or generated files.

## Hardening overview

The threat model treats skill repositories as third-party input. The
boundaries are described in
[ARCHITECTURE.md, Security boundaries](ARCHITECTURE.md#security-boundaries),
and the audit subsystem is specified in
[docs/audit-design.md](docs/audit-design.md) and
[docs/v0.8-design.md](docs/v0.8-design.md).
