from __future__ import annotations

import hashlib
import json
import logging
import os
import ssl
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import init_git_repo

from csk import git_ops, git_trust


def _listed(monkeypatch, *certificates):
    monkeypatch.setattr(
        git_trust,
        "DISTRUSTED_CERTIFICATES",
        {
            hashlib.sha256(ssl.PEM_cert_to_DER_cert(data.decode())).hexdigest(): name
            for name, data in certificates
        },
    )


@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
def test_filter_keeps_exact_blocks_and_warns_once(
    git_ca_bundle, incidental_git_ca, tmp_path, monkeypatch, caplog, line_ending
):
    # Listed duplicates are removed; unlisted PEM order/bytes survive repeated snapshots and a changed source path with one process warning.
    blocked = git_ca_bundle.read_bytes()
    clean = incidental_git_ca.read_bytes().replace(b"\n", line_ending)
    _listed(monkeypatch, ("Generated blocked CA", blocked))
    monkeypatch.setattr(git_trust, "_WARNED", False)
    content = b"# public CA bundle\n" + blocked + clean + blocked + clean
    git_ca_bundle.write_bytes(content)
    caplog.set_level(logging.WARNING, logger="csk.git_trust")
    for path in (git_ca_bundle, git_ca_bundle, tmp_path / "another.pem"):
        path.write_bytes(content)
        monkeypatch.setenv("CSK_GIT_CA_BUNDLE", str(path))
        with git_trust.private_git_trust() as trust:
            assert trust.ca_file != path
            assert trust.ca_file.read_bytes() == clean + clean
            if os.name != "nt":
                # Windows reports writable DOS mode bits; creation mode is checked separately.
                assert trust.ca_file.stat().st_mode & 0o777 == 0o600
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cafile=str(trust.ca_file))
            assert context.get_ca_certs(binary_form=True) == [
                ssl.PEM_cert_to_DER_cert(clean.decode())
            ]
    records = [r for r in caplog.records if r.name == "csk.git_trust"]
    assert len(records) == 1
    assert git_trust.DISTRUSTED in records[0].message
    assert "Generated blocked CA" in records[0].message
    assert repr(str(git_ca_bundle)) in records[0].message
    assert "BEGIN CERTIFICATE" not in caplog.text


def test_warning_names_all_dropped_roots(
    git_ca_bundle, incidental_git_ca, monkeypatch, caplog
):
    # A successful filter names every distinct dropped CA from the source, retaining a third root as a positive control.
    from conftest import _generated_git_ca

    first = git_ca_bundle.read_bytes()
    second = incidental_git_ca.read_bytes()
    clean = _generated_git_ca()
    _listed(monkeypatch, ("Blocked A", first), ("Blocked B", second))
    monkeypatch.setattr(git_trust, "_WARNED", False)
    git_ca_bundle.write_bytes(first + second + clean)
    with (
        caplog.at_level(logging.WARNING, logger="csk.git_trust"),
        git_trust.private_git_trust() as trust,
    ):
        assert trust.ca_file.read_bytes() == clean
    assert len(caplog.records) == 1
    assert "Blocked A, Blocked B" in caplog.text


def test_every_listed_root_leaves_no_trust_or_child(
    git_ca_bundle, incidental_git_ca, monkeypatch
):
    # Removing all listed roots refuses as invalid before any Git child; the operator file is preserved.
    first, second = git_ca_bundle.read_bytes(), incidental_git_ca.read_bytes()
    _listed(monkeypatch, ("Blocked A", first), ("Blocked B", second))
    git_ca_bundle.write_bytes(first + second + first)
    children = []
    monkeypatch.setattr(git_ops, "_run", lambda *a, **kw: children.append(a))
    with pytest.raises(git_ops.GitError, match=git_trust.INVALID) as error:
        git_ops.run_git(["--version"])
    assert "no certificates remain after filtering" in str(error.value)
    assert not children
    assert git_ca_bundle.read_bytes() == first + second + first


@pytest.mark.parametrize("position", ["prefix", "middle", "suffix"])
@pytest.mark.parametrize(
    "garbage", [b"unrecognised data\n", b"\x00\xff", b"-----BEGIN PRIVATE KEY-----\n"]
)
def test_garbage_is_never_silently_filtered(git_ca_bundle, position, garbage):
    # A valid certificate cannot hide text, binary garbage or a foreign PEM block at any bundle boundary.
    clean = git_ca_bundle.read_bytes()
    positive = git_trust._check_bundle(git_ca_bundle)
    assert positive == clean
    content = {
        "prefix": garbage + clean,
        "middle": clean + garbage + clean,
        "suffix": clean + garbage,
    }[position]
    git_ca_bundle.write_bytes(content)
    with pytest.raises(git_trust.GitTrustError, match=git_trust.INVALID):
        git_trust.https_git_args()


def test_annotations_are_discarded_but_not_accepted_as_a_trailing_certificate(
    git_ca_bundle,
):
    # The macOS annotated format and comments work; an unfinished annotation after the last PEM is invalid.
    clean = git_ca_bundle.read_bytes()
    annotation = b"# root\n\n=== /CN=Generated root\nCertificate:\n    Data:\n        Version: 3 (0x2)\nSHA1 Fingerprint=AA:BB\nSHA256 Fingerprint=CC:DD\n"
    git_ca_bundle.write_bytes(annotation + clean + b"# end\n")
    assert git_trust._check_bundle(git_ca_bundle) == clean
    git_ca_bundle.write_bytes(clean + annotation)
    with pytest.raises(git_trust.GitTrustError, match=git_trust.INVALID):
        git_trust.https_git_args()


def test_filtered_cache_invalidates_on_bytes_and_policy(
    git_ca_bundle, incidental_git_ca, monkeypatch
):
    # Cached filtering never returns listed bytes; content and policy changes recompute the retained set.
    blocked, clean = git_ca_bundle.read_bytes(), incidental_git_ca.read_bytes()
    _listed(monkeypatch, ("Blocked", blocked))
    git_ca_bundle.write_bytes(blocked + clean)
    assert git_trust._check_bundle(git_ca_bundle) == clean
    assert git_trust._check_bundle(git_ca_bundle) == clean
    git_ca_bundle.write_bytes(clean + blocked + clean)
    assert git_trust._check_bundle(git_ca_bundle) == clean + clean
    _listed(monkeypatch, ("Other blocked", clean))
    assert git_trust._check_bundle(git_ca_bundle) == blocked


@pytest.mark.parametrize(
    "annotation",
    [
        b"=== root\n",
        b"=== root\nCertificate:\n    Data:\n",
        b"=== root\nCertificate:\nSHA1 Fingerprint=AA\n",
        b"=== root\nCertificate:\n-----BEGIN BROKEN-----\nSHA1 Fingerprint=AA\nSHA256 Fingerprint=BB\n",
    ],
)
def test_incomplete_annotations_do_not_hide_malformed_blocks(git_ca_bundle, annotation):
    # An annotation must be complete, and cannot hide an unparsed PEM marker before an otherwise valid certificate.
    clean = git_ca_bundle.read_bytes()
    git_ca_bundle.write_bytes(annotation + clean)
    with pytest.raises(git_trust.GitTrustError, match=git_trust.INVALID):
        git_trust.https_git_args()


def _annotation(text):
    return (
        b"=== /CN=Generated root\nCertificate:\n    Data:\n"
        + text
        + b"SHA1 Fingerprint=AA:BB\nSHA256 Fingerprint=CC:DD\n"
    )


@pytest.mark.parametrize("prefix", [b"", b" ", b"\t", b"\r\n\t", b"extension: ", b"# "])
@pytest.mark.parametrize("label", [b"CERTIFICATE", b"TRUSTED CERTIFICATE", b"X509 CRL"])
@pytest.mark.parametrize("marker", [b"-----BEGIN", b"-----end", b"-----BeGiN"])
def test_annotation_delimiters_refuse_before_git(
    git_ca_bundle, monkeypatch, prefix, label, marker
):
    # Any unparsed BEGIN/END delimiter in an annotation refuses, regardless of indentation, label, case or comment prefix; plain text still works.
    clean = git_ca_bundle.read_bytes()
    git_ca_bundle.write_bytes(
        _annotation(b"extension: printable value\n# comment\n") + clean
    )
    assert git_trust._check_bundle(git_ca_bundle) == clean
    malformed = _annotation(prefix + marker + b" " + label + b"-----\n") + clean
    git_ca_bundle.write_bytes(malformed)
    with pytest.raises(git_trust.GitTrustError) as error:
        git_trust._check_bundle(git_ca_bundle)
    assert error.value.code == git_trust.INVALID
    children = []
    monkeypatch.setattr(git_ops, "_run", lambda *a, **kw: children.append(a))
    with pytest.raises(git_ops.GitError, match=git_trust.INVALID):
        git_ops.run_git(["--version"])
    assert not children
    assert git_ca_bundle.read_bytes() == malformed


@pytest.mark.parametrize("position", ["prefix", "middle", "suffix"])
@pytest.mark.parametrize("marker", [b"-----BEGIN", b"-----end"])
def test_comments_cannot_hide_delimiters(git_ca_bundle, position, marker):
    # Comment handling cannot discard an unparsed delimiter at any bundle boundary; delimiter-free comments preserve exact PEM bytes.
    clean = git_ca_bundle.read_bytes()
    git_ca_bundle.write_bytes(
        b"# begin and end annotations\n" + clean + b"# plain end\n"
    )
    assert git_trust._check_bundle(git_ca_bundle) == clean
    comment = b"# embedded " + marker + b" CERTIFICATE-----\n"
    content = {
        "prefix": comment + clean,
        "middle": clean + comment + clean,
        "suffix": clean + comment,
    }[position]
    git_ca_bundle.write_bytes(content)
    with pytest.raises(git_trust.GitTrustError) as error:
        git_trust._check_bundle(git_ca_bundle)
    assert error.value.code == git_trust.INVALID


@pytest.mark.parametrize("prefix", [b" ", b"\t"])
def test_real_cli_annotation_delimiters_start_no_git(git_ca_bundle, tmp_path, prefix):
    # Real update accepts a plain annotation but refuses the reviewer's indented malformed block before any Git start, preserving local state.
    skills = tmp_path / "skills"
    repo = init_git_repo(skills / "demo")
    configuration = tmp_path / "config.json"
    configuration.write_text(
        json.dumps({"schema_version": 1, "skills_root": str(skills), "projects": {}})
    )
    trace = tmp_path / "trace.json"
    environment = dict(
        os.environ,
        CSK_CONFIG=str(configuration),
        CSK_GIT_CA_BUNDLE=str(git_ca_bundle),
        GIT_TRACE2_EVENT=str(trace),
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    for name in ("GIT_DIR", "GIT_WORK_TREE"):
        environment.pop(name, None)

    def update():
        return subprocess.run(
            [sys.executable, "-m", "csk", "update"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    clean = git_ca_bundle.read_bytes()
    git_ca_bundle.write_bytes(_annotation(b"        Version: 3 (0x2)\n") + clean)
    positive = update()
    assert positive.returncode == 0, positive.stdout + positive.stderr
    assert "fetched demo" in positive.stdout
    assert any(
        json.loads(line).get("event") == "start"
        for line in trace.read_text().splitlines()
    )
    trace.unlink()
    before = {
        p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()
    }
    config_before = configuration.read_bytes()
    hidden = (
        b"\n".join(
            prefix + line
            for line in (
                b"-----BEGIN CERTIFICATE-----",
                b"@@ invalid base64 @@",
                b"-----END CERTIFICATE-----",
            )
        )
        + b"\n"
    )
    malformed = _annotation(hidden) + clean
    git_ca_bundle.write_bytes(malformed)
    negative = update()
    assert negative.returncode == 1
    assert git_trust.INVALID in negative.stdout + negative.stderr
    assert not trace.exists(), negative.stdout + negative.stderr
    assert {
        p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()
    } == before
    assert configuration.read_bytes() == config_before
    assert git_ca_bundle.read_bytes() == malformed


@pytest.mark.parametrize("suffix", [b"\x00", b"garbage", b"\x30\x00"])
def test_malformed_listed_der_is_not_ignored_in_a_mixed_bundle(
    git_ca_bundle, incidental_git_ca, monkeypatch, suffix
):
    # Filtering a listed root does not excuse ambiguous DER, even when a valid unlisted root would remain.
    original = git_ca_bundle.read_bytes()
    clean = incidental_git_ca.read_bytes()
    _listed(monkeypatch, ("Blocked", original))
    git_ca_bundle.write_bytes(original + clean)
    assert git_trust._check_bundle(git_ca_bundle) == clean
    malformed = ssl.DER_cert_to_PEM_cert(
        ssl.PEM_cert_to_DER_cert(original.decode()) + suffix
    ).encode()
    git_ca_bundle.write_bytes(malformed + clean)
    with pytest.raises(git_trust.GitTrustError, match=git_trust.INVALID):
        git_trust.https_git_args()


def test_snapshot_creation_always_requests_private_exclusive_mode(
    git_ca_bundle, monkeypatch
):
    # All platforms request 0600 and exclusive creation; POSIX additionally exposes those permissions through stat.
    real_open = git_trust.os.open
    snapshots = []

    def record(path, flags, mode=0o777, **kwargs):
        if str(path).endswith("ca.pem") and flags & os.O_CREAT:
            snapshots.append((flags, mode))
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(git_trust.os, "open", record)
    with git_trust.private_git_trust() as trust:
        assert trust.ca_file.read_bytes() == git_ca_bundle.read_bytes()
    assert len(snapshots) == 1
    flags, mode = snapshots[0]
    assert mode == 0o600
    assert flags & os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        assert flags & os.O_NOFOLLOW
