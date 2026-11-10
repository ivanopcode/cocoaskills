"""Transport regression controls retained from independent origin-reconcile review."""
import pytest
from conftest import init_git_repo, run
from test_sources_transport import _loopback_tls_cert, _tls_server_factory
import test_source_closure_refresh as refresh
from csk import git_ops


@pytest.mark.parametrize('schema', [2, 3])
def test_review_both_schemas_resolve_current_policy_endpoint(
    schema, tmp_path, skills_root, csk_home, monkeypatch, capsys,
):
    # Exercise the existing CLI current-endpoint refresh scenario in both
    # schemas. Endpoint planning is real; acquisition uses the existing local
    # transport fixture, so this does not prove a live remote authentication.
    original = refresh.write_skillfile
    observed_schemas = []

    def write(project, data):
        if data.get('schema_version') == 2:
            data = {**data, 'schema_version': schema}
            observed_schemas.append(schema)
        return original(project, data)

    monkeypatch.setattr(refresh, 'write_skillfile', write)
    refresh.test_v2_refresh_current_endpoint_existing_checkout_uses_alias_and_replaces_lock(
        tmp_path, skills_root, csk_home, monkeypatch, capsys,
    )
    assert observed_schemas and all(value == schema for value in observed_schemas)


@pytest.mark.parametrize('trusted', [True, False])
def test_review_reconcile_real_tls_pins_effective_endpoint(
    _tls_server_factory, _loopback_tls_cert, git_ca_bundle, tmp_path, monkeypatch, trusted,
):
    # With a declared .git spelling rewritten to real loopback TLS, the selected
    # CA reaches HTTP and an unrelated CA cannot; neither failure persists origin.
    server = _tls_server_factory(status=404, body=b'not found')
    monkeypatch.setenv('CSK_GIT_CA_BUNDLE', _loopback_tls_cert[1] if trusted else str(git_ca_bundle))
    monkeypatch.setenv('GIT_SSL_NO_VERIFY', '1')
    repo = init_git_repo(tmp_path / 'clone')
    old = 'https://example.test/repo'
    declared = old + '.git'
    run(['git', 'remote', 'add', 'origin', old], repo)
    run(['git', 'config', f'url.{server.url}.insteadOf', declared], repo)
    run(['git', 'config', f'http.{server.url}.sslVerify', 'false'], repo)
    run(['git', 'config', f'http.{server.url}.sslCAInfo', '/unchecked.pem'], repo)
    with pytest.raises(git_ops.GitError):
        git_ops.fetch_repo(repo, declared)
    assert server.auth_seen == ([None] if trusted else [])
    assert run(['git', 'config', '--get', 'remote.origin.url'], repo).stdout.strip() == old
