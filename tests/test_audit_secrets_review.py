import json
import os
from pathlib import Path
import pytest
from csk import cli, config
from test_audit_publish import _record

@pytest.fixture
def context(tmp_path, monkeypatch):
    home = tmp_path / 'manager'
    home.mkdir()
    cfg = config.GlobalConfig(path=home/'config.json', skills_root=tmp_path/'skills', preferred_locale=None,
        default_agents=['codex_cli'], adapter_mode='auto', worktree_alias_pattern='[A-Z]+-[0-9]+', projects={})
    config.save_config(cfg)
    monkeypatch.setenv('CSK_CONFIG', str(cfg.path))
    monkeypatch.delenv('CSK_SYSTEM_CONFIG', raising=False)
    monkeypatch.delenv('CSK_REGISTRY_TOKEN', raising=False)
    record = tmp_path/'record.json'
    record.write_text(json.dumps(_record()))
    return ['audit','--publish',str(record),'--registry','http://127.0.0.1:1']

@pytest.mark.parametrize('source', ['file','env'])
def test_header_failure_never_echoes_token(context, tmp_path, monkeypatch, capsys, source):
    # Synthetic values only; real HTTP serialization fails before a connection.
    token = 'review-synthetic-private-marker\ninvalid-header'
    argv = context[:]
    if source == 'file':
        path = tmp_path/'token'
        path.write_text(token)
        path.chmod(0o600)
        argv += ['--token-file',str(path)]
    else:
        monkeypatch.setenv('CSK_REGISTRY_TOKEN',token)
    assert cli.main(argv) == cli.EXIT_CONFIG
    out = capsys.readouterr()
    assert 'review-synthetic-private-marker' not in out.out + out.err

@pytest.mark.parametrize('prefix', [['audit'],['config','build-https','add','git.example/team']])
def test_token_abbreviation_never_echoes_value(prefix, capsys):
    assert cli.main(prefix+['--tok=review-synthetic-private-marker']) == cli.EXIT_CONFIG
    out = capsys.readouterr()
    assert 'review-synthetic-private-marker' not in out.out + out.err


def test_unknown_home_is_structured_refusal(context, capsys):
    assert cli.main(context+['--token-file','~csk_review_user_does_not_exist/token']) == cli.EXIT_CONFIG
    out = capsys.readouterr()
    assert 'csk_review_user_does_not_exist' not in out.out + out.err

@pytest.mark.skipif(os.name == 'nt', reason='POSIX device/permissions and privileged symlinks')
@pytest.mark.parametrize('kind',['device','hardlink','cycle'])
def test_extra_file_shapes_refused(context, tmp_path, capsys, kind):
    path = tmp_path/'token'
    if kind=='device':
        path = Path('/dev/null')
    elif kind=='hardlink':
        target = tmp_path/'shared'
        target.write_text('review-synthetic-private-marker')
        target.chmod(0o640)
        os.link(target,path)
    else:
        path.symlink_to(path)
    assert cli.main(context+['--token-file',str(path)]) == cli.EXIT_CONFIG
    out=capsys.readouterr()
    assert str(path) not in out.out+out.err
    assert 'review-synthetic-private-marker' not in out.out+out.err

@pytest.mark.parametrize('kind', ['command','codex'])
@pytest.mark.parametrize('phase', ['extract','canary'])
def test_backend_windows_mixed_case_override_through_launch(tmp_path, monkeypatch, kind, phase):
    from types import SimpleNamespace
    from csk.audit.backends import environment
    from csk.audit.backend_config import parse_backend_config
    from csk.audit.backends.command_backend import CommandBackend
    from csk.audit.backends.codex_backend import CodexBackend
    from test_audit_secrets import _request
    import subprocess
    parent = {'pAtH':'original','Home':str(tmp_path),'SystemRoot':'synthetic-system',
        'ComSpec':'synthetic-shell','lc_messages':'C','lc_private_key':'synthetic-secret',
        'Git_CONFIG_COUNT':'1','ssh_auth_sock':'synthetic-secret','service_token':'synthetic-secret'}
    monkeypatch.setattr(environment,'os',SimpleNamespace(name='nt',environ=parent))
    if kind=='command':
        cfg=parse_backend_config(kind,{'kind':kind,'command':['synthetic-adapter'], 'env': {
            'PaTh':'override','GIT_config_global':'synthetic-secret','Lc_Secret_Token':'synthetic-secret',
            'sSh_AuTh_SoCk':'synthetic-secret','private_key':'synthetic-secret'}},global_model=None,allow_cloud=False)
        backend=CommandBackend(cfg)
    else:
        cfg=parse_backend_config(kind,{'kind':kind,'oss':True,'local_provider':'ollama'},global_model=None,allow_cloud=False)
        backend=CodexBackend(cfg)
    launches=[]
    def run(argv, **kwargs):
        launches.append(kwargs['env'])
        data=json.dumps({'schema_version':1,'findings':[]})
        if '--output-last-message' in argv:
            Path(argv[argv.index('--output-last-message')+1]).write_text(data)
        return subprocess.CompletedProcess(argv,0,data.encode(),b'')
    monkeypatch.setattr(subprocess,'run',run)
    if phase=='extract':
        assert backend.extract(_request(),timeout=5)==()
    else:
        assert backend.run_canary() is False
    assert launches == [{'PATH':'override' if kind=='command' else 'original','HOME':str(tmp_path),
        'SYSTEMROOT':'synthetic-system','COMSPEC':'synthetic-shell','LC_MESSAGES':'C'}]


@pytest.mark.skipif(os.name == 'nt', reason='Privileged symlink creation; regular replacement is tested natively')
def test_nofollow_unavailable_must_not_follow_swap(context, tmp_path, monkeypatch):
    # Exercise the platform capability fallback used where O_NOFOLLOW is absent.
    path = tmp_path/'token'
    target = tmp_path/'target'
    path.write_text('original-synthetic-token')
    target.write_text('replacement-synthetic-token')
    path.chmod(0o600)
    target.chmod(0o600)
    original_open = os.open
    def swap_before_open(p, flags, *args, **kwargs):
        if Path(p)==path:
            path.unlink()
            path.symlink_to(target)
        return original_open(p, flags, *args, **kwargs)
    monkeypatch.delattr(os,'O_NOFOLLOW',raising=False)
    monkeypatch.setattr(os,'open',swap_before_open)
    calls=[]
    monkeypatch.setattr(cli.audit_registry,'http_publish_record',lambda url,token,data: calls.append(token) or {'seq':1})
    rc=cli.main(context+['--token-file',str(path)])
    assert calls == [], 'symlink replacement was read and sent to publication'
    assert rc == cli.EXIT_CONFIG
