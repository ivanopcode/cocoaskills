import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import pytest
from test_mcp_project_install import project_cli, PATHS
from test_mcp_install_acceptance import _snapshot

pytestmark = pytest.mark.usefixtures('stable_env')

def run_cli(root,*args):
    return subprocess.run([sys.executable,'-m','csk',*args],cwd=root,env=os.environ.copy(),capture_output=True,text=True,stdin=subprocess.DEVNULL,timeout=30)

def stdio(command,args,**extra):
    return {'bad':{'server':{'transport':'stdio','command':command,'args':args,**extra}}}

# A real awk dispatch reaches the recording stub, while csk refuses both recipes.
# The actual dispatch check uses a recording npx stub, never the real package runner.
@pytest.mark.parametrize('args',[
    ['BEGIN {system("npx pkg")}'],
    ['-v','a=npx pkg','BEGIN {system(a)}'],
])
@pytest.mark.skipif(os.name == "nt", reason="recording shell stub requires POSIX")
def test_embedded_runner_is_refused(project_cli,tmp_path,args):
    if not shutil.which("awk"):
        pytest.skip("real awk dispatch witness requires awk")
    root,home,declare=project_cli
    declare(stdio('awk',args))
    stub=tmp_path/'stub-bin'
    stub.mkdir()
    marker=tmp_path/'runner-invoked.json'
    npx=stub/'npx'
    npx.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$REVIEW_NPX_MARKER"\n')
    npx.chmod(0o700)
    # Positive control for the execution interpretation, confined to this test fixture.
    executed=subprocess.run([shutil.which('awk'),*args],cwd=root,env=dict(os.environ,PATH=str(stub)+os.pathsep+os.environ['PATH'],REVIEW_NPX_MARKER=str(marker)),capture_output=True,text=True,timeout=5)
    assert executed.returncode==0 and marker.read_text().strip()=='pkg'
    before=_snapshot(root,home)
    results=[]
    for command in [('check','app'),('install','app','--dry-run')]:
        r=run_cli(root,*command)
        results.append((command,r.returncode,r.stdout,r.stderr))
    print(json.dumps({'args':args,'recorded_runner_args':marker.read_text(),'cli':results}))
    assert _snapshot(root,home)==before
    assert all(r[1]!=0 and 'mcp_command_invalid' in r[2]+r[3] for r in results)


# Reusing Git metadata must retain relative and absolute core.excludesFile semantics.
@pytest.mark.parametrize('kind',['relative','absolute','info','nested-negation','global-negation'])
def test_ignore_context_equivalence(project_cli,kind):
    root,home,_=project_cli
    before_ignore=(root/'.gitignore').read_bytes()
    if kind in ('relative','absolute'):
        excludes=root/'review-excludes'
        excludes.write_text('/.mcp.json\n/.codex/config.toml\n/.cursor/mcp.json\n')
        value='review-excludes' if kind=='relative' else str(excludes)
        subprocess.run(['git','-C',str(root),'config','core.excludesFile',value],check=True)
    elif kind=='info':
        (root/'.git/info').mkdir(exist_ok=True)
        (root/'.git/info/exclude').write_text('/.mcp.json\n/.codex/config.toml\n/.cursor/mcp.json\n')
    elif kind=='nested-negation':
        (root/'.codex').mkdir()
        (root/'.codex/.gitignore').write_text('!config.toml\n')
    elif kind=='global-negation':
        excludes=root/'review-excludes'
        excludes.write_text('!/.mcp.json\n!/.codex/config.toml\n!/.cursor/mcp.json\n')
        subprocess.run(['git','-C',str(root),'config','core.excludesFile',str(excludes)],check=True)
    live=[subprocess.run(['git','-C',str(root),'check-ignore','-q',path]).returncode for path in PATHS]
    before=_snapshot(root,home)
    r=run_cli(root,'install','app')
    effective=[subprocess.run(['git','-C',str(root),'check-ignore','-q',path]).returncode for path in PATHS]
    print(json.dumps({'kind':kind,'live_before':live,'exit':r.returncode,'stdout':r.stdout,'stderr':r.stderr,'live_after':effective}))
    if kind=='nested-negation':
        assert r.returncode!=0 and 'gitignore_required' in r.stderr
        assert _snapshot(root,home)==before
    else:
        assert r.returncode==0 and effective==[0,0,0],(r.stdout,r.stderr)
        if kind in ('relative','absolute','info'):
            assert (root/'.gitignore').read_bytes()==before_ignore

# An embedded runner refuses before terminal consent or native publication.
@pytest.mark.skipif(os.name == "nt", reason="real terminal witness requires POSIX PTY")
def test_embedded_real_publish(project_cli,tmp_path):
    import errno,pty,select,time
    root,home,declare=project_cli
    declare(stdio('awk',['BEGIN {system("npx pkg")}']))
    before=_snapshot(root,home)
    master,slave=pty.openpty()
    child=subprocess.Popen([sys.executable,'-m','csk','install','app'],cwd=root,env=os.environ.copy(),stdin=slave,stdout=slave,stderr=slave)
    os.close(slave)
    output=b''
    replied=False
    try:
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            ready,_,_=select.select([master],[],[],0.1)
            if ready:
                try:
                    data=os.read(master,65536)
                except OSError as exc:
                    if exc.errno==errno.EIO: break
                    raise
                if not data: break
                output+=data
                if b'[y/N]' in output and not replied:
                    os.write(master,b'y\n')
                    replied=True
            elif child.poll() is not None: break
        rc=child.wait(timeout=3)
    finally:
        if child.poll() is None:
            child.terminate(); child.wait(timeout=5)
        os.close(master)
    native=json.loads((root/'.mcp.json').read_text()) if (root/'.mcp.json').exists() else None
    print(json.dumps({'install_exit':rc,'approval':replied,'transcript':output.decode(errors='replace'),'native':native}))
    assert rc == 1 and b"mcp_command_invalid" in output
    assert not replied and native is None

    assert _snapshot(root,home)==before


# A subdirectory invocation and a config include must retain the root-relative
# excludes source. XDG/default and info/exclude controls reuse live sources;
# the XDG writer stays test-local so later installs retain their ignore policy.
@pytest.mark.parametrize("kind", ["included-relative", "tilde", "xdg", "subdirectory"])
def test_additional_ignore_sources(project_cli, tmp_path, monkeypatch, kind):
    from csk import gitignore_gate
    root, home, _ = project_cli
    excludes = root / "review-excludes"
    if kind == "tilde":
        excludes = Path.home() / "review-excludes"
    elif kind == "xdg":
        # The default Git ignore source must be local to this test; the
        # session's shared XDG directory would affect unrelated later projects.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        excludes = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "git/ignore"
        excludes.parent.mkdir(parents=True, exist_ok=True)
    excludes.parent.mkdir(parents=True, exist_ok=True)
    excludes.write_text("/.mcp.json\n/.codex/config.toml\n/.cursor/mcp.json\n")
    if kind == "included-relative":
        include = root / "ignore-config"
        include.write_text("[core]\n\texcludesFile = review-excludes\n")
        subprocess.run(["git", "-C", str(root), "config", "include.path", str(include)], check=True)
    elif kind != "xdg":
        value = "~/review-excludes" if kind == "tilde" else "review-excludes"
        subprocess.run(["git", "-C", str(root), "config", "core.excludesFile", value], check=True)
    project = root
    entries = ["/" + path for path in PATHS]
    if kind == "subdirectory":
        project = root / "subproject"
        project.mkdir()
        excludes.write_text("/subproject/.mcp.json\n/subproject/.codex/config.toml\n/subproject/.cursor/mcp.json\n")
    before = _snapshot(root, home)
    assert gitignore_gate.plan_ignored(project, entries, fix=True) is None
    stage = tmp_path / "private-ignore"
    stage.mkdir()
    gitignore_gate.verify_planned_ignored(project, entries, None, stage)
    assert _snapshot(root, home) == before


# Consent preserves argument boundaries for opaque programs and limits the
# unverifiable-program disclosure to non-runner commands, with no execution.
@pytest.mark.parametrize("command,args,opaque", [
    ("my-server", ["a b", '\"quoted\"', "--node-id"], True),
    pytest.param("npx", ["pkg@1.2.3"], False, marks=pytest.mark.skipif(os.name == "nt", reason="Windows npx batch refusal is covered before consent")),
    ("uvx", ["pkg==1.2.3"], False),
])
def test_consent_exact_argv_and_scope(project_cli, monkeypatch, capsys, tmp_path, command, args, opaque):
    from csk import cli
    root, home, declare = project_cli
    if os.name == "nt":
        # Native PE admission fixtures; consent never dispatches them.
        native = tmp_path / "native"; native.mkdir()
        (native / (command + ".exe")).write_bytes(b"MZfixture")
        monkeypatch.setenv("PATH", str(native) + os.pathsep + os.environ["PATH"])
    declare(stdio(command, args))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert cli.main(["install", "app"]) == 0
    output = capsys.readouterr().out
    assert json.dumps([command, *args]) in output
    assert ("csk cannot verify what this program runs." in output) == opaque


# Relative implicit global excludes have the same root-context obligation as
# core.excludesFile; this also keeps subdirectory callers from changing policy.
@pytest.mark.parametrize("kind", ["xdg", "home"])
def test_relative_default_excludes(project_cli, tmp_path, monkeypatch, kind):
    from csk import gitignore_gate
    root, home, _ = project_cli
    if kind == "xdg":
        monkeypatch.setenv("XDG_CONFIG_HOME", "global-config")
        excludes = root / "global-config/git/ignore"
    else:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", "relative-home")
        excludes = root / "relative-home/.config/git/ignore"
    excludes.parent.mkdir(parents=True, exist_ok=True)
    excludes.write_text("/.mcp.json\n/.codex/config.toml\n/.cursor/mcp.json\n")
    entries = ["/" + path for path in PATHS]
    assert gitignore_gate.plan_ignored(root, entries, fix=True) is None
    before = _snapshot(root, home)
    stage = tmp_path / "private-default-ignore"
    stage.mkdir()
    gitignore_gate.verify_planned_ignored(root, entries, None, stage)
    assert _snapshot(root, home) == before
