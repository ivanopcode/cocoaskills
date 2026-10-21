from __future__ import annotations

import hashlib
import logging
import ssl

import pytest

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
    assert str(git_ca_bundle) in records[0].message
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
