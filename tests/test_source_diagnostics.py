"""Stable draft-sources diagnostics: table, rendering, secret redaction.

The thirteen protocol classes render through one declared table
(:data:`csk.sources.diagnostics.REMEDIATION_BY_CODE`) and one renderer
(:func:`csk.sources.diagnostics.format_diagnostic`). Completeness is derived
from the error modules: every ``CODE_*`` constant in ``errors`` and
``repository_policy`` must be either a table key or an explicitly
listed non-protocol extra, so a fourteenth class fails
:func:`test_remediation_table_completeness_derived_from_error_modules`
instead of being silently unrendered.

Every test below asserts the RENDERED user-visible text, never only the
structured exception: finding F7 (TASK-260916-fsw7re) was structured
data right and displayed text wrong, so display is the obligation.
"""

from __future__ import annotations

import random
import re
import types
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import quote

import pytest

from csk import closure as closure_module
from csk import manifest as manifest_module
from csk import source_identity as source_identity_module
from csk.sources import diagnostics as source_diagnostics
from csk.sources import errors as source_errors
from csk.sources import repository_policy
from csk.sources import transport as source_transport
from csk.sources.repository_policy import EndpointProvenance

EXACT_LABEL = "draft skillfile-sources-v1 (opt-in)"


def _code_constants(module: types.ModuleType) -> set[str]:
    """Collect every ``CODE_*`` string constant defined in a module."""

    return {
        value
        for name, value in vars(module).items()
        if name.startswith("CODE_") and isinstance(value, str)
    }


def test_removed_label_constant_is_absent() -> None:
    assert not hasattr(source_errors, "DRAFT_SKILLFILE_SOURCES_LABEL")


def test_removed_label_is_absent_from_production_sources() -> None:
    """The removed status label no longer appears in production sources."""

    repo = Path(__file__).resolve().parent.parent / "src" / "csk"
    owned = [
        repo / "cli.py",
        repo / "installer.py",
        repo / "status.py",
        repo / "manifest.py",
        repo / "sources" / "diagnostics.py",
        repo / "sources" / "errors.py",
    ]
    quoted = re.compile(r"""['\"]draft skillfile-sources-v1 \(opt-in\)['\"]""")
    hits: list[str] = []
    for path in owned:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if quoted.search(line):
                hits.append(f"{path.parent.name}/{path.name}:{lineno}")
    assert hits == []


def test_schema2_manifest_needs_no_legacy_opt_in_hint() -> None:
    parsed = manifest_module.parse_manifest(
        {"schema_version": 2, "sources": {}, "skills": []}, Path("Skillfile.json")
    )
    assert parsed.schema_version == 2


def test_remediation_table_completeness_derived_from_error_modules() -> None:
    """Every error-module code is rendered or explicitly extra (13 + 3)."""

    collected = _code_constants(source_errors) | _code_constants(repository_policy)
    assert len(collected) == 16, sorted(collected)
    assert set(source_diagnostics.REMEDIATION_BY_CODE) == set(
        source_diagnostics.STABLE_DIAGNOSTIC_CODES
    )
    assert len(source_diagnostics.STABLE_DIAGNOSTIC_CODES) == 13
    assert collected - set(source_diagnostics.REMEDIATION_BY_CODE) == set(
        source_diagnostics.NON_PROTOCOL_SOURCE_CODES
    )
    assert set(source_diagnostics.NON_PROTOCOL_SOURCE_CODES) == {
        source_errors.CODE_PATH_CONFLICT,
        source_errors.CODE_INVENTORY_INVALID,
        source_errors.CODE_PATH_EQUIVALENCE_INVALID,
    }


def test_stable_codes_follow_protocol_order() -> None:
    assert source_diagnostics.STABLE_DIAGNOSTIC_CODES == (
        "source_alias_unknown",
        "source_selection_invalid",
        "source_member_missing",
        "source_member_invalid",
        "source_name_conflict",
        "source_output_overlap",
        "source_snapshot_changed",
        "source_snapshot_unavailable",
        "source_lock_stale",
        "repository_policy_invalid",
        "repository_endpoint_unavailable",
        "repository_mirror_undeclared",
        "repository_alias_unknown",
    )


@pytest.mark.parametrize("code", source_diagnostics.STABLE_DIAGNOSTIC_CODES)
def test_format_diagnostic_shape_for_every_class(code: str) -> None:
    """A source diagnostic contains a reason and one remediation line."""

    remediation = source_diagnostics.REMEDIATION_BY_CODE[code]
    assert "\n" not in remediation
    rendered = source_diagnostics.format_diagnostic(
        code, "subject fixture-member failed for a stated reason"
    )
    lines = rendered.splitlines()
    assert len(lines) == 2, rendered
    assert lines[0].startswith(f"{code}: "), rendered
    assert "fixture-member" in lines[0], rendered
    assert "stated reason" in lines[0], rendered
    remediation_lines = [line for line in lines if line.startswith("remediation: ")]
    assert len(remediation_lines) == 1, rendered
    assert remediation_lines[0] == f"remediation: {remediation}", rendered
    assert EXACT_LABEL not in rendered


def test_format_diagnostic_unknown_code_is_structured() -> None:
    with pytest.raises(ValueError, match="unknown schema-2 source diagnostic code"):
        source_diagnostics.format_diagnostic("source_nope", "reason")


def test_format_exception_renders_source_and_policy_errors() -> None:
    rendered = source_diagnostics.format_exception(
        source_errors.SourceError(
            source_errors.CODE_NAME_CONFLICT, "Duplicate skill name: dup"
        )
    )
    assert rendered is not None
    assert rendered.splitlines()[0].startswith("source_name_conflict: ")
    assert len([line for line in rendered.splitlines() if line.startswith("remediation: ")]) == 1
    assert len(rendered.splitlines()) == 2
    assert EXACT_LABEL not in rendered

    rendered = source_diagnostics.format_exception(
        repository_policy.RepositoryPolicyError(
            repository_policy.CODE_POLICY_INVALID, "source policy is not valid"
        )
    )
    assert rendered is not None
    assert rendered.splitlines()[0].startswith("repository_policy_invalid: ")


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("plain v1 failure"),
        source_errors.SourceError(
            source_errors.CODE_PATH_CONFLICT, "inventory paths collide"
        ),
    ],
)
def test_format_exception_keeps_non_table_errors_legacy(exc: BaseException) -> None:
    assert source_diagnostics.format_exception(exc) is None


def _attempt(
    ordinal: int, classification: str, listed_url: str
) -> source_transport.AttemptDiagnostic:
    return source_transport.AttemptDiagnostic(
        ordinal=ordinal,
        endpoint=EndpointProvenance(
            listed_url=listed_url,
            url_host="example.org",
            url_port=None,
            resolved_host="example.org",
            resolved_port=443,
            alias=None,
            mirror_of=None,
        ),
        classification=classification,
        code=repository_policy.CODE_ENDPOINT_UNAVAILABLE,
    )


def test_transport_exhaustion_renders_classifications_and_table_remediation() -> None:
    """F7 pinned at the display: attempts, endpoint subject, one remediation."""

    exc = source_transport.TransportResolutionError(
        (
            _attempt(0, "timeout", "https://example.org/kit.git"),
            _attempt(1, "connection-refused", "https://example.org/kit.git"),
        )
    )
    rendered = source_diagnostics.format_transport_exception(exc)
    assert rendered is not None
    lines = rendered.splitlines()
    assert lines[0].startswith("repository_endpoint_unavailable: "), rendered
    assert "attempt 0=timeout" in lines[0], rendered
    assert "attempt 1=connection-refused" in lines[0], rendered
    assert "https://example.org/kit.git" in lines[0], rendered
    remediation_lines = [line for line in lines if line.startswith("remediation: ")]
    assert len(remediation_lines) == 1, rendered
    assert remediation_lines[0] == (
        "remediation: "
        + source_diagnostics.REMEDIATION_BY_CODE[
            repository_policy.CODE_ENDPOINT_UNAVAILABLE
        ]
    )
    assert len(lines) == 2, rendered
    assert EXACT_LABEL not in rendered


def test_transport_reason_renders_endpoint_structurally() -> None:
    """A token-shaped scp username renders as parsed host/path, never raw.

    The listed URL is re-parsed with the production endpoint parser
    and only scheme, host, port and path reach the display; userinfo
    is dropped by construction rather than redacted by pattern.
    """

    exc = source_transport.TransportResolutionError(
        (_attempt(0, "timeout", "ghp_SECRETx9@github.com:o/r.git"),)
    )
    rendered = source_diagnostics.format_transport_exception(exc)
    assert rendered is not None
    assert "for ssh://github.com/o/r.git" in rendered.splitlines()[0], rendered
    assert "ghp_SECRETx9" not in rendered, rendered


def test_transport_reason_omits_unparseable_endpoint() -> None:
    """An unparseable listed URL yields no subject rather than a raw echo."""

    exc = source_transport.TransportResolutionError(
        (_attempt(0, "timeout", "notaurl"),)
    )
    reason = source_diagnostics.transport_failure_reason(exc)
    assert reason == "repository endpoint unavailable: attempt 0=timeout", reason


def test_endpoint_remediation_pinned_to_transport_structured_detail() -> None:
    """The table remediation equals the transport's structured remediation."""

    exc = source_transport.TransportResolutionError(
        (_attempt(0, "timeout", "https://example.org/kit.git"),)
    )
    expected = source_diagnostics.REMEDIATION_BY_CODE[
        repository_policy.CODE_ENDPOINT_UNAVAILABLE
    ]
    assert f"remediation: {expected}" in str(exc)


def test_transport_wrapping_policy_error_renders_chained_reason() -> None:
    cause = repository_policy.RepositoryPolicyError(
        repository_policy.CODE_POLICY_INVALID, "source policy requires an object"
    )
    exc = source_transport.TransportError(
        repository_policy.CODE_POLICY_INVALID,
        "source policy could not produce an attempt plan",
    )
    exc.__cause__ = cause
    rendered = source_diagnostics.format_transport_exception(exc)
    assert rendered is not None
    assert rendered.splitlines()[0].startswith("repository_policy_invalid: "), rendered
    assert "source policy requires an object" in rendered, rendered


def test_transport_failure_class_only_renders_named_class() -> None:
    exc = source_transport.TransportFailure("timeout", "attempt failed")
    reason = source_diagnostics.transport_failure_reason(exc)
    assert "timeout" in reason


def test_transport_bare_refusal_has_honest_fallback_reason() -> None:
    exc = source_transport.TransportError(
        repository_policy.CODE_ENDPOINT_UNAVAILABLE, "x"
    )
    assert (
        source_diagnostics.transport_failure_reason(exc)
        == "transport refusal recorded without attempt detail"
    )


def test_format_transport_exception_rejects_released_lane_codes() -> None:
    exc = source_transport.TransportError(
        "build_repository_transport_failure", "attempt raised"
    )
    assert source_diagnostics.format_transport_exception(exc) is None


def test_format_transport_exception_rejects_spoofed_code_origin() -> None:
    """A matching ``.code`` outside the transport module does not render."""

    class Spoofed(Exception):
        def __init__(self) -> None:
            super().__init__("spoofed")
            self.code = repository_policy.CODE_ENDPOINT_UNAVAILABLE

    assert source_diagnostics.format_transport_exception(Spoofed()) is None


@pytest.mark.parametrize(
    ("raw", "kept", "dropped"),
    [
        (
            "git declaration 'https://ops:s3cret@git.example.com/o/r.git' refused",
            ["https://***@git.example.com/o/r.git"],
            ["ops:s3cret", "s3cret"],
        ),
        (
            "git declaration 'https://tok-abc123@github.com/o/r.git' refused",
            ["https://***@github.com/o/r.git"],
            ["tok-abc123"],
        ),
        (
            "endpoint 'ssh://deploy@corp.example/kit.git' names unknown alias 'ci'",
            ["ssh://***@corp.example/kit.git", "'ci'"],
            ["deploy@corp.example"],
        ),
        (
            "git declaration 'https://operator:pass%20word@git.example.com/t/k.git' refused",
            ["https://***@git.example.com/t/k.git"],
            ["pass%20word", "operator:pass"],
        ),
        (
            "git declaration 'https://git.example.com/t/k.git?private_token=QUERYSECRET123' refused",
            ["https://git.example.com/t/k.git?private_token=***"],
            ["QUERYSECRET123"],
        ),
        (
            "git declaration 'https://git.example.com/t/k.git?SIG=QUERYSECRET123&x=1' refused",
            ["?SIG=***&x=1"],
            ["QUERYSECRET123"],
        ),
        (
            "git declaration 'https://git.example.com/t/k.git?signature=QUERYSECRET123' refused",
            ["?signature=***"],
            ["QUERYSECRET123"],
        ),
        (
            "git declaration 'https://git.example.com/t/k.git?x-amz-signature=QUERYSECRET123' refused",
            ["?x-amz-signature=***"],
            ["QUERYSECRET123"],
        ),
        # Quote-aware URL-token and existing substring-key controls.
        (
            'git declaration "https://op:pa\'ss@host.example/t/k.git" refused',
            ["https://***@host.example/t/k.git"],
            ["op:pa'ss", "pa'ss"],
        ),
        (
            "git declaration 'https://host.example/t/k.git?x_token=SUBSECRET9' refused",
            ["https://host.example/t/k.git?x_token=***"],
            ["SUBSECRET9"],
        ),
        (
            "Clone user:scp-secret@git.example.com:team/repo.git now",
            ["***@git.example.com:team/repo.git"],
            ["user:scp-secret@"],
        ),
        (
            "git declaration 'https://op:pa\\\\ss@host.example/t/k.git' refused",
            ["https://***@host.example/t/k.git"],
            ["pa\\\\ss", "op:pa"],
        ),
        (
            "repository endpoint unavailable: attempt 0=timeout for https://org:FormatSettings@example.org/k.git",
            ["for https://***@example.org/k.git"],
            ["org:FormatSettings"],
        ),
        (
            "cannot read source policy /tmp/u/home/source-policy.json: denied",
            ["cannot read source policy", "<path>"],
            ["/tmp/u/home/source-policy.json"],
        ),
        (
            "Source root /tmp/u/src is inside managed output '.agents'",
            ["Source root", "<path>", "'.agents'"],
            ["/tmp/u/src"],
        ),
        (
            "ancestor '/var/data/x' disappeared during inspection",
            ["ancestor", "<path>"],
            ["/var/data/x"],
        ),
        (
            "bad root C:\\Build\\repo here",
            ["bad root", "<path>"],
            ["C:\\Build\\repo"],
        ),
        (
            "share \\\\FILESRV\\drop\\a end",
            ["share", "<path>"],
            ["\\\\FILESRV\\drop\\a"],
        ),
    ],
)
def test_sanitize_redacts_secrets(raw: str, kept: list[str], dropped: list[str]) -> None:
    redacted = source_errors.sanitize_detail(raw)
    for keep in kept:
        assert keep in redacted, redacted
    for drop in dropped:
            assert drop not in redacted, redacted


@pytest.mark.parametrize(
    ("git_url", "reason"),
    [
        (
            "https://operator:abc/def+ghi=@git.example.com/t/k.git",
            "invalid explicit port in source",
        ),
        (
            "https://operator:pass word@git.example.com/t/k.git",
            "network source must not contain a password",
        ),
        (
            "https://ghp_abc/Z9x@github.com/o/r.git",
            "network source has an invalid host",
        ),
    ],
    ids=("slash-delimited-field", "whitespace-field", "path-at-field"),
)
def test_malformed_git_field_refusal_keeps_reason_without_echoing_field(
    git_url: str, reason: str
) -> None:
    with pytest.raises(source_errors.SourceError) as raised:
        closure_module._canonical_requirement_identity(
            git_url, name="dependency", chain="root"
        )

    rendered = source_diagnostics.format_exception(raised.value)

    assert rendered is not None
    assert "malformed Git source" in rendered
    assert reason in rendered
    assert git_url not in rendered
    assert "operator:abc" not in rendered
    assert "pass word" not in rendered
    assert "ghp_abc/Z9x" not in rendered


@pytest.mark.parametrize(
    "text",
    [
        "Source 'team-docs' must be an acquisition object",
        "Skill 'review' locked source cannot be revalidated",
        "Selector directory 'agents/skills/review' escapes the source root",
        "no machine endpoint policy entry for 'example.org/kit'",
        "resolved host 'mirror.example.net' differs from 'example.org'",
        "Duplicate installed skill name 'dup' in lock membership",
        "lock manifest_sha256 sha256:abc does not match current Skillfile",
        "and/or host/path N/A a/b 1/2",
        "Skill 'review' is up-to-date",
        "See https://example.com/docs and contact bob@corp for access",
        "endpoint 'https://example.org/kit.git?next=1&depth=2' exhausted",
        "fragment https://example.org/kit.git#token=visible",
        "wrapped (user:pass@host:path) text",
    ],
)
def test_sanitize_preserves_subjects_byte_exactly(text: str) -> None:
    assert source_errors.sanitize_detail(text) == text


def _generated_url_diagnostic(
    rng: random.Random,
    iteration: int,
    counters: dict[str, int],
) -> tuple[str, list[str], list[str], dict[str, set[str]]]:
    secrets: list[str] = []
    keeps: list[str] = []
    coverage: dict[str, set[str]] = {
        "schemes": set(),
        "subdelims": set(),
        "at_forms": set(),
        "hosts": set(),
        "url_counts": set(),
        "joiners": set(),
        "multi_url_quotes": set(),
        "whole_segment_quotes": set(),
        "multi_url_quotes_used": set(),
        "whole_segment_quotes_used": set(),
    }

    def sentinel(kind: str) -> str:
        number = counters[kind]
        counters[kind] += 1
        value = f"{kind.upper()}_{number}"
        (secrets if kind == "secret" else keeps).append(value)
        return value

    schemes = ("https", "ssh", "git+ssh", "http")
    quote_kinds = ("'", '"', "`")
    subdelims = "!$&'()*+,;="
    count = rng.randint(1, 4)
    coverage["url_counts"].add(str(count))
    urls: list[str] = []

    def credentialed_url() -> str:
        scheme = rng.choice(schemes)
        coverage["schemes"].add(scheme)
        username_secret = sentinel("secret")
        password_secret = sentinel("secret")
        query_secret = sentinel("secret")
        username_sep = rng.choice(("@", "%40"))
        coverage["at_forms"].add("raw" if username_sep == "@" else "percent-encoded")
        username_delim = rng.choice(subdelims)
        password_delim = rng.choice(subdelims)
        coverage["subdelims"].update((username_delim, password_delim))
        username = (
            f"user{username_delim}{username_secret}"
            f"{username_sep}{rng.choice(subdelims)}"
        )
        password = (
            f"pass{password_delim}{password_secret}"
            f"{rng.choice(subdelims)}"
        )
        userinfo = f"{username}:{password}@"

        if rng.getrandbits(1):
            host = f"[2001:db8::{rng.randrange(1, 65536):x}]"
            coverage["hosts"].add("ipv6")
        else:
            host = f"host{rng.randrange(1, 10000)}.example"
            coverage["hosts"].add("dns")
        port = rng.randrange(1, 65536)
        path_keep = sentinel("keep")
        query_keep = sentinel("keep")
        fragment_keep = sentinel("keep")
        query_key = rng.choice(
            (
                "token",
                "private_token",
                "access_token",
                "sig",
                "signature",
                "client_secret",
            )
        )
        url = (
            f"{scheme}://{userinfo}{host}:{port}/repo/{path_keep}.git"
            f"?{query_key}={query_secret}&next={query_keep}"
            f"#fragment-{fragment_keep}"
        )
        return url

    for index in range(count):
        urls.append(credentialed_url())
    if count > 1:
        # Keep the corpus at one to four actual URL occurrences while placing
        # one later scheme inside the first URL's fragment.
        urls[0] += f"::nested::{urls.pop(1)}"

    email_keep = sentinel("keep")
    prose_words = [sentinel("keep") for _ in range(rng.randint(1, 4))]
    email_local = rng.choice(("ops", "ci", "review"))
    email_domain = rng.choice(
        ("example.test", "example.invalid", "ops.example.test")
    )
    before = " ".join(
        (*prose_words, f"{email_local}{email_keep}@{email_domain}")
    )
    after = sentinel("keep")
    joiner = rng.choice(("", ";", " | ", " "))
    group = joiner.join(urls)
    mode = iteration % 5
    quote = ""
    if mode == 1:
        quote = quote_kinds[(iteration // 5) % len(quote_kinds)]
        group = f"{quote}{group}{quote}"
    elif mode == 2:
        quote = quote_kinds[(iteration // 5) % len(quote_kinds)]
        group = f"{quote}{before} {group} {after}{quote}"
        before = after = ""
    elif mode == 3:
        # Each adjacent pair has no delimiter, exercising the next-scheme
        # boundary independently of any surrounding whitespace.
        group = "".join(urls)

    if mode == 1:
        coverage["multi_url_quotes_used"].add(quote)
    elif mode == 2:
        coverage["whole_segment_quotes_used"].add(quote)
    if count > 1 and mode == 1:
        coverage["multi_url_quotes"].add(quote)
    if mode == 2:
        coverage["whole_segment_quotes"].add(quote)
    if count > 1 and (mode == 3 or joiner == ""):
        coverage["joiners"].add("adjacent")
    else:
        coverage["joiners"].add("separated")

    message = f"{before} {group} {after}"
    assert message.count("://") == count
    return message, secrets, keeps, coverage


def test_sanitize_generated_url_diagnostics_process_every_scheme_independently() -> None:
    seed = 0x5EED260927
    rng = random.Random(seed)
    counters = {"secret": 0, "keep": 0}
    observed: dict[str, set[str]] = {}

    for iteration in range(2500):
        message, secrets, keeps, coverage = _generated_url_diagnostic(
            rng, iteration, counters
        )
        for key, values in coverage.items():
            observed.setdefault(key, set()).update(values)
        redacted = source_errors.sanitize_detail(message)

        for secret in secrets:
            assert secret not in redacted, (
                f"seed={seed} iteration={iteration} leaked {secret}"
            )
        for keep in keeps:
            assert keep in redacted, (
                f"seed={seed} iteration={iteration} lost {keep}"
            )

    assert observed["schemes"] == {"https", "ssh", "git+ssh", "http"}
    assert observed["subdelims"] == set("!$&'()*+,;=")
    assert observed["at_forms"] == {"raw", "percent-encoded"}
    assert observed["hosts"] == {"dns", "ipv6"}
    assert observed["url_counts"] == {"1", "2", "3", "4"}
    assert observed["joiners"] == {"adjacent", "separated"}
    assert observed["multi_url_quotes"] == {"'", '"', "`"}
    assert observed["whole_segment_quotes"] == {"'", '"', "`"}
    assert observed["multi_url_quotes_used"] == {"'", '"', "`"}
    assert observed["whole_segment_quotes_used"] == {"'", '"', "`"}


# The raw secret corpus includes characters that must be encoded before
# entering URL userinfo. Malformed source fields are handled by their typed
# parser refusal and never echoed; URL redaction itself follows authority
# boundaries and does not guess that path text is userinfo.
_SECRET_ALPHABET = "abcdef0123456789/:+= ABCDEFGHJKLMNPQRSTUVWXYZ\n"


def _generated_secret(rng: random.Random, length: int) -> str:
    chars = [rng.choice(_SECRET_ALPHABET) for _ in range(length)]
    pinned = {at for at, char in enumerate(chars) if char in "/ +=:\n"}
    for required in ("/", " ", "+", "=", ":", "\n"):
        if required in chars:
            continue
        candidates = [at for at in range(length) if at not in pinned]
        at = rng.choice(candidates)
        chars[at] = required
        pinned.add(at)
    return "".join(chars)


def _rendered_form(secret: str) -> str:
    """The exact bytes of ``secret`` inside a ``{value!r}`` echo site."""

    return repr(secret)[1:-1]


def _generated_corpus(
    rng: random.Random, home: Path | PureWindowsPath
) -> dict[str, str]:
    password = _generated_secret(rng, 24)
    token = _generated_secret(rng, 32)
    key_material = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        + _generated_secret(rng, 48)
        + "\n-----END OPENSSH PRIVATE KEY-----"
    )
    userinfo_safe = "!$&()*+,;=:@"
    password_field = quote(password, safe=userinfo_safe)
    token_field = quote(token, safe=userinfo_safe)
    key_material_field = quote(key_material, safe=userinfo_safe)
    home_path = str(home / ".cocoaskills" / "source-policy.json")
    return {
        "password": password,
        "token": token,
        "password_field": password_field,
        "credentialed_url": f"https://operator:{password_field}@git.corp.example/team/kit.git",
        "token_field": token_field,
        "token_url": f"https://{token_field}@git.corp.example/team/kit.git",
        "query_token": quote(token, safe=""),
        "query_url": (
            "https://git.corp.example/team/kit.git"
            f"?private_token={quote(token, safe='')}&next=1"
        ),
        "key_password_url": (
            f"https://operator:{key_material_field}@git.corp.example/team/kit.git"
        ),
        "key_material_field": key_material_field,
        "key_material": key_material,
        "home_path": home_path,
    }


def test_transport_drops_broker_detail_from_display() -> None:
    """Opaque broker secrets never reach display text: the boundary drops them."""

    secret = "broker-secret-token-value"
    assert secret not in str(
        source_transport.TransportFailure("timeout", f"broker said {secret}")
    )
    assert secret not in str(
        source_transport.TransportError("x-code", f"credential broker: {secret}")
    )


@pytest.mark.parametrize(
    "home_root",
    [None, r"C:\Users\runner\workspace"],
    ids=["host-path", "windows-path"],
)
def test_sanitize_generated_corpus_absent_from_echo_sites(
    tmp_path: Path, home_root: str | None
) -> None:
    """Secrets placed at every hypothetical echo site vanish; subjects stay readable.

    This pins the sanitizer as a second line of defence: the echo
    shapes below are what production built before revision 3
    (``{value!r}``); production no longer echoes, and the full
    hostile alphabet is proven absent at the CLI level instead (see
    the corpus comment above). Opaque secrets reach an echo site
    inside URL userinfo (key material included: operators paste keys
    as passwords) or a query-string credential parameter; broker
    replies never echo by construction (see the test above). Every
    asserted-absent secret is first asserted PRESENT in the
    unsanitized detail: that presence assertion is the non-vacuity
    proof, i.e. the test fails for every case when sanitization is
    disabled.
    """

    rng = random.Random(0x1A2B3C)
    home = tmp_path if home_root is None else PureWindowsPath(home_root)
    corpus = _generated_corpus(rng, home)
    cases: list[tuple[str, str, str]] = [
        (
            "password",
            corpus["password_field"],
            f"git declaration {corpus['credentialed_url']!r} "
            "must not contain userinfo, a password, or a port",
        ),
        (
            "token",
            corpus["token_field"],
            f"git declaration {corpus['token_url']!r} "
            "must not contain userinfo, a password, or a port",
        ),
        (
            "query_token",
            corpus["query_token"],
            f"git declaration {corpus['query_url']!r} "
            "carries an invalid repository path",
        ),
        (
            "key_material",
            corpus["key_material_field"],
            f"git declaration {corpus['key_password_url']!r} "
            "must not contain userinfo, a password, or a port",
        ),
        (
            "home_path",
            corpus["home_path"],
            f"cannot read source policy {corpus['home_path']}: "
            "[Errno 13] denied",
        ),
    ]
    for name, secret, detail in cases:
        rendered = secret if name == "home_path" else _rendered_form(secret)
        assert rendered in detail, (name, "vacuous case: secret never occurs")
        redacted = source_errors.sanitize_detail(detail)
        assert rendered not in redacted, (name, detail, redacted)
        assert secret not in redacted, (name, detail, redacted)
    # Subjects stay readable: the involved host survives userinfo
    # redaction, a non-credential query parameter survives query
    # redaction, and uninvolved policy hostnames are untouched here
    # (cross-entry leakage is proven at the CLI level with decoys).
    redacted_query = source_errors.sanitize_detail(cases[2][2])
    assert "git.corp.example" in redacted_query, redacted_query
    assert "next=1" in redacted_query, redacted_query
    assert "?private_token=***" in redacted_query, redacted_query
    assert "mirror.example.net" in source_errors.sanitize_detail(
        "resolved host 'mirror.example.net' differs from 'example.org'"
    )
