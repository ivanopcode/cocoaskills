from __future__ import annotations

import copy
import datetime
import hashlib
import json
from pathlib import Path

import pytest

from csk import mcp_declarations as mcp
from csk.mcp_declarations import (
    EffectiveServer,
    McpDeclarationError,
    check_requirements,
    declaration_digest,
    entry_digest,
    parse_mcp_servers,
    render_native_entry,
)
from csk.skillspec import McpServerRequirement


TARGETS = ["claude_code", "codex_cli", "cursor"]
URL = "https://example.invalid/mcp"
VECTORS = json.loads((Path(__file__).parent / "fixtures" / "mcp" / "slice1-vectors.json").read_text("utf-8"))


def parse_one(declaration: dict, *, name: str = "docs", scope: str = "project", targets=None):
    return parse_mcp_servers(
        {name: declaration},
        invocation_scope=scope,
        target_agents=TARGETS if targets is None else targets,
    )[name]


def stdio(command: str = "uvx", args=("example-tools==1.2.3",), env_names=(), auth=None, **top) -> dict:
    declaration = {
        "server": {"transport": "stdio", "command": command, "args": list(args), "env_names": list(env_names)},
        "auth": auth or {"kind": "env" if env_names else "none"},
    }
    declaration.update(top)
    return declaration


def http(headers=None, url: str = URL, auth=None, **top) -> dict:
    server: dict = {"transport": "http", "url": url}
    if headers is not None:
        server["headers"] = headers
    declaration = {"server": server, "auth": auth or {"kind": "env" if headers else "none"}}
    declaration.update(top)
    return declaration


def bearer(env: str = "DOCS_TOKEN") -> dict:
    return {"Authorization": {"env": env, "prefix": "bearer"}}


def refused(declaration, code: str, field: str | None = None, **kwargs) -> McpDeclarationError:
    name = kwargs.pop("name", "docs")
    with pytest.raises(McpDeclarationError) as info:
        parse_mcp_servers({name: declaration}, invocation_scope=kwargs.pop("scope", "project"),
                          target_agents=kwargs.pop("targets", TARGETS))
    assert info.value.code == code, str(info.value)
    if field is not None:
        assert info.value.field == field, str(info.value)
    return info.value


# --- vectors V01-V13 ----------------------------------------------------------


def test_vector_table_covers_v01_to_v13():
    assert [vector["id"] for vector in VECTORS["vectors"]] == [f"V{n:02d}" for n in range(1, 14)]


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda vector: vector["id"])
def test_slice1_vector(vector):
    kwargs = {"invocation_scope": VECTORS["invocation_scope"], "target_agents": VECTORS["targets"]}
    raw = {vector["name"]: vector["declaration"]}
    expect = vector["expect"]
    if expect in ("admit", "mcp_plan_unapproved"):
        decl = parse_mcp_servers(raw, **kwargs)[vector["name"]]
        assert mcp.authority(decl).consent_required is vector["consent_required"]
    else:
        with pytest.raises(McpDeclarationError) as info:
            parse_mcp_servers(raw, **kwargs)
        assert info.value.code == expect


# --- top level and declaration ------------------------------------------------


def test_empty_object_parses_to_nothing():
    assert parse_mcp_servers({}, invocation_scope="project", target_agents=TARGETS) == {}


@pytest.mark.parametrize("raw", [[], None, "docs", 3, [{"docs": {}}]])
def test_raw_must_be_an_object(raw):
    with pytest.raises(McpDeclarationError) as info:
        parse_mcp_servers(raw, invocation_scope="project", target_agents=TARGETS)
    assert (info.value.code, info.value.field) == ("mcp_declaration_invalid", "mcp_servers")


def test_at_most_64_declarations():
    ok = {f"s{n}": http() for n in range(64)}
    assert len(parse_mcp_servers(ok, invocation_scope="project", target_agents=TARGETS)) == 64
    with pytest.raises(McpDeclarationError) as info:
        parse_mcp_servers({**ok, "extra": http()}, invocation_scope="project", target_agents=TARGETS)
    assert info.value.code == "mcp_declaration_invalid"


@pytest.mark.parametrize(
    "name",
    ["", "-lead", "_lead", "a.b", "a b", "a/b", "docs\n", "ü", "x" * 129, "con", "CON", "Nul", "aux", "prn",
     "com1", "COM9", "lpt1", "LPT9", "con.txt", "nul.json"],
)
def test_bad_names_are_refused(name):
    refused(http(), "mcp_declaration_invalid", f"mcp_servers.{name}", name=name)


@pytest.mark.parametrize("name", ["a", "A1", "x" * 128, "my-server_2", "com0", "console", "lpt10", "com"])
def test_good_names_are_admitted(name):
    assert parse_one(http(), name=name).name == name


def test_non_string_name_is_refused():
    with pytest.raises(McpDeclarationError) as info:
        parse_mcp_servers({1: http()}, invocation_scope="project", target_agents=TARGETS)
    assert info.value.code == "mcp_declaration_invalid"


def test_case_insensitive_duplicate_names_are_refused():
    with pytest.raises(McpDeclarationError) as info:
        parse_mcp_servers({"Docs": http(), "docs": http()}, invocation_scope="project", target_agents=TARGETS)
    assert info.value.code == "mcp_declaration_invalid"


def test_results_are_ordered_by_name():
    result = parse_mcp_servers({"b": http(), "a": http()}, invocation_scope="project", target_agents=TARGETS)
    assert list(result) == ["a", "b"]


DECLARATION_NEGATIVES = [
    ("not-an-object", [], "mcp_declaration_invalid", "mcp_servers.docs"),
    ("unknown-top-field", {**http(), "trust": True}, "mcp_declaration_invalid", "mcp_servers.docs.trust"),
    ("env-top-field", {**http(), "env": {"A": "b"}}, "mcp_declaration_invalid", "mcp_servers.docs.env"),
    ("server-and-reference", {**http(), "reference": {"kind": "git"}}, "mcp_declaration_invalid", "mcp_servers.docs"),
    ("neither-server-nor-reference", {"auth": {"kind": "none"}}, "mcp_declaration_invalid", "mcp_servers.docs"),
    ("auth-missing", {"server": http()["server"]}, "mcp_declaration_invalid", "mcp_servers.docs.auth"),
    ("auth-not-object", {**http(), "auth": "none"}, "mcp_declaration_invalid", "mcp_servers.docs.auth"),
    ("auth-unknown-field", {**http(), "auth": {"kind": "none", "token": "x"}}, "mcp_declaration_invalid",
     "mcp_servers.docs.auth.token"),
    ("auth-kind-missing", {**http(), "auth": {}}, "mcp_declaration_invalid", "mcp_servers.docs.auth.kind"),
    ("auth-kind-unknown", {**http(), "auth": {"kind": "basic"}}, "mcp_declaration_invalid",
     "mcp_servers.docs.auth.kind"),
    ("auth-help-url-not-string", {**http(), "auth": {"kind": "none", "help_url": 3}}, "mcp_declaration_invalid",
     "mcp_servers.docs.auth.help_url"),
    ("server-not-object", {"server": "x", "auth": {"kind": "none"}}, "mcp_declaration_invalid",
     "mcp_servers.docs.server"),
    ("transport-missing", {"server": {"url": URL}, "auth": {"kind": "none"}}, "mcp_declaration_invalid",
     "mcp_servers.docs.server.transport"),
    ("transport-unknown", {"server": {"transport": "ws", "url": URL}, "auth": {"kind": "none"}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.transport"),
    ("transport-not-string", {"server": {"transport": 1, "url": URL}, "auth": {"kind": "none"}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.transport"),
    ("http-with-command", {"server": {"transport": "http", "url": URL, "command": "x"}, "auth": {"kind": "none"}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.command"),
    ("http-with-trust", {"server": {"transport": "http", "url": URL, "trust": True}, "auth": {"kind": "none"}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.trust"),
    ("http-without-url", {"server": {"transport": "http"}, "auth": {"kind": "none"}}, "mcp_declaration_invalid",
     "mcp_servers.docs.server.url"),
    ("http-url-not-string", {"server": {"transport": "http", "url": 1}, "auth": {"kind": "none"}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.url"),
    ("http-headers-not-object", {"server": {"transport": "http", "url": URL, "headers": []},
                                  "auth": {"kind": "none"}}, "mcp_declaration_invalid",
     "mcp_servers.docs.server.headers"),
    ("stdio-with-url", {"server": {"transport": "stdio", "command": "uvx", "url": URL}, "auth": {"kind": "none"}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.url"),
    ("stdio-with-headers", {"server": {"transport": "stdio", "command": "uvx", "headers": {}},
                            "auth": {"kind": "none"}}, "mcp_declaration_invalid", "mcp_servers.docs.server.headers"),
    ("stdio-with-cwd", {"server": {"transport": "stdio", "command": "uvx", "cwd": "/"}, "auth": {"kind": "none"}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.cwd"),
    ("stdio-with-literal-env", {"server": {"transport": "stdio", "command": "uvx", "env": {"A": "b"}},
                                "auth": {"kind": "none"}}, "mcp_declaration_invalid", "mcp_servers.docs.server.env"),
    ("stdio-without-command", {"server": {"transport": "stdio"}, "auth": {"kind": "none"}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.command"),
    ("stdio-command-not-string", {"server": {"transport": "stdio", "command": ["uvx"]}, "auth": {"kind": "none"}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.command"),
    ("stdio-args-not-list", {"server": {"transport": "stdio", "command": "uvx", "args": "x"},
                             "auth": {"kind": "none"}}, "mcp_declaration_invalid", "mcp_servers.docs.server.args"),
    ("stdio-arg-not-string", {"server": {"transport": "stdio", "command": "uvx", "args": [1]},
                              "auth": {"kind": "none"}}, "mcp_declaration_invalid", "mcp_servers.docs.server.args"),
    ("stdio-env-names-not-list", {"server": {"transport": "stdio", "command": "uvx", "env_names": "A"},
                                  "auth": {"kind": "none"}}, "mcp_declaration_invalid",
     "mcp_servers.docs.server.env_names"),
    ("reference-refused", {"reference": {"kind": "git"}, "auth": {"kind": "none"}}, "mcp_capability_unsupported",
     "mcp_servers.docs.reference"),
    ("reference-with-agents-refused", {"reference": {"kind": "file"}, "agents": ["cursor"],
                                       "auth": {"kind": "none"}}, "mcp_capability_unsupported",
     "mcp_servers.docs.reference"),
    ("reference-requires-auth", {"reference": {"kind": "git"}}, "mcp_declaration_invalid",
     "mcp_servers.docs.auth"),
    ("sse-refused", {"server": {"transport": "sse", "url": "https://example.invalid/sse"},
                     "auth": {"kind": "none"}}, "mcp_capability_unsupported", "mcp_servers.docs.server.transport"),
]


@pytest.mark.parametrize(("declaration", "code", "field"), [row[1:] for row in DECLARATION_NEGATIVES],
                         ids=[row[0] for row in DECLARATION_NEGATIVES])
def test_declaration_negatives(declaration, code, field):
    refused(declaration, code, field)


# --- agents, scope, git_policy ------------------------------------------------


def test_omitted_agents_resolve_to_slice1_targets_sorted():
    decl = parse_one(http(), targets=["cursor", "gemini", "claude_code"])
    assert decl.agents == ("claude_code", "cursor")


def test_explicit_agents_are_sorted():
    assert parse_one(http(agents=["cursor", "claude_code"])).agents == ("claude_code", "cursor")


AGENT_NEGATIVES = [
    ("omitted-no-slice1-target", http(), ["gemini", "opencode"], "mcp_agent_selection_invalid"),
    ("omitted-no-targets", http(), [], "mcp_agent_selection_invalid"),
    ("empty-list", http(agents=[]), TARGETS, "mcp_agent_selection_invalid"),
    ("duplicate", http(agents=["cursor", "cursor"]), TARGETS, "mcp_agent_selection_invalid"),
    ("unknown-agent", http(agents=["nonesuch"]), TARGETS + ["nonesuch"], "mcp_agent_selection_invalid"),
    ("known-but-not-a-target", http(agents=["windsurf"]), TARGETS, "mcp_agent_selection_invalid"),
    ("slice1-agent-not-a-target", http(agents=["cursor"]), ["claude_code"], "mcp_agent_selection_invalid"),
    ("one-of-two-not-a-target", http(agents=["claude_code", "cursor"]), ["claude_code"],
     "mcp_agent_selection_invalid"),
    ("known-outside-slice1", http(agents=["gemini"]), TARGETS + ["gemini"], "mcp_capability_unsupported"),
    ("opencode-outside-slice1", http(agents=["cursor", "opencode"]), TARGETS + ["opencode"],
     "mcp_capability_unsupported"),
    ("windsurf-outside-slice1", http(agents=["windsurf"]), TARGETS + ["windsurf"], "mcp_capability_unsupported"),
]


@pytest.mark.parametrize(("declaration", "targets", "code"), [row[1:] for row in AGENT_NEGATIVES],
                         ids=[row[0] for row in AGENT_NEGATIVES])
def test_agent_selection_negatives(declaration, targets, code):
    refused(declaration, code, targets=targets)


@pytest.mark.parametrize("agents", ["cursor", [1], {"a": 1}])
def test_agents_must_be_a_list_of_strings(agents):
    refused(http(agents=agents), "mcp_declaration_invalid", "mcp_servers.docs.agents")


def test_unknown_agent_is_selection_error_not_capability():
    error = refused(http(agents=["nonesuch"]), "mcp_agent_selection_invalid", targets=TARGETS + ["nonesuch"])
    assert error.field == "mcp_servers.docs.agents.0"


SCOPE_NEGATIVES = [
    ("user-in-project", http(scope="user"), "project", "mcp_scope_mismatch"),
    ("project-in-user", http(scope="project"), "user", "mcp_scope_mismatch"),
    ("bogus-scope", http(scope="team"), "project", "mcp_declaration_invalid"),
    ("scope-not-string", http(scope=1), "project", "mcp_declaration_invalid"),
    ("user-invocation-unsupported", http(scope="user"), "user", "mcp_capability_unsupported"),
    ("user-invocation-omitted-scope", http(), "user", "mcp_capability_unsupported"),
    ("bogus-invocation-scope", http(), "global", "mcp_declaration_invalid"),
    ("shared-http", http(git_policy="shared"), "project", "mcp_capability_unsupported"),
    ("shared-stdio", stdio(git_policy="shared"), "project", "mcp_capability_unsupported"),
    ("bogus-git-policy", http(git_policy="tracked"), "project", "mcp_declaration_invalid"),
    ("git-policy-not-string", http(git_policy=True), "project", "mcp_declaration_invalid"),
]


@pytest.mark.parametrize(("declaration", "scope", "code"), [row[1:] for row in SCOPE_NEGATIVES],
                         ids=[row[0] for row in SCOPE_NEGATIVES])
def test_scope_and_git_policy_negatives(declaration, scope, code):
    refused(declaration, code, scope=scope)


def test_scope_and_git_policy_defaults_and_explicit_values():
    assert (parse_one(http()).scope, parse_one(http()).git_policy) == ("project", "local")
    explicit = parse_one(http(scope="project", git_policy="local"))
    assert (explicit.scope, explicit.git_policy) == ("project", "local")


def test_scope_mismatch_precedes_capability_and_agent_errors():
    refused(http(scope="user", git_policy="shared", agents=["windsurf"]), "mcp_scope_mismatch")


def test_agent_selection_precedes_capability():
    refused(http(git_policy="shared", agents=["cursor"]), "mcp_agent_selection_invalid", targets=["claude_code"])


def test_syntax_precedes_everything():
    refused(http(scope="user", trust=True), "mcp_declaration_invalid")


def test_every_declaration_passes_each_step_before_the_next_step_runs():
    raw = {"a": http(git_policy="shared"), "b": http(agents=["windsurf"])}
    with pytest.raises(McpDeclarationError) as info:
        parse_mcp_servers(raw, invocation_scope="project", target_agents=TARGETS)
    assert info.value.code == "mcp_agent_selection_invalid"


# --- stdio command, evaluators, runtime pins ---------------------------------


@pytest.mark.parametrize("command", ["./tools/server", "/usr/bin/uvx", "..", "a b", "", "-x", "a\\b", "a/b",
                                     "~/x", "$HOME", "x" * 129, "uvx;ls", "tool\n"])
def test_command_must_be_a_bare_name(command):
    refused(stdio(command, args=[]), "mcp_command_invalid", "mcp_servers.docs.server.command")


@pytest.mark.parametrize(
    "command",
    ["sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "csh", "tcsh", "cmd", "powershell", "pwsh",
     "env", "xargs", "nohup", "sudo", "doas", "su", "busybox", "script"],
)
@pytest.mark.parametrize("variant", [str, str.upper, lambda c: c + ".exe", lambda c: c.upper() + ".EXE"])
def test_shells_and_indirection_are_refused(command, variant):
    refused(stdio(variant(command), args=[]), "mcp_command_invalid", "mcp_servers.docs.server.command")


@pytest.mark.parametrize("command", ["node", "nodejs", "deno", "bun", "python", "python3", "ruby", "perl", "php"])
@pytest.mark.parametrize("flag", ["-e", "--eval", "-c", "-p", "--print", "eval"])
def test_evaluator_arguments_are_refused(command, flag):
    refused(stdio(command, args=["server.js", flag]), "mcp_command_invalid", "mcp_servers.docs.server.args.1")


def test_evaluator_arguments_are_refused_for_cased_and_exe_commands():
    refused(stdio("Python.EXE", args=["-c", "x"]), "mcp_command_invalid")


def test_evaluator_commands_without_eval_arguments_are_admitted():
    assert parse_one(stdio("python3", args=["server.py", "--port", "8080"])).server.command == "python3"
    assert parse_one(stdio("node", args=["server.js"])).server.command == "node"


def test_eval_flags_are_fine_for_other_commands():
    assert parse_one(stdio("mytool", args=["-e", "-c", "-p"])).server.args == ("-e", "-c", "-p")


NPX_REFUSED = [
    ["-y", "@example/tools@latest"],
    ["-y", "pkg@latest"],
    ["pkg"],
    ["@example/tools"],
    ["pkg@next"],
    ["pkg@1"],
    ["pkg@1.2"],
    ["pkg@^1.2.3"],
    ["pkg@~1.2.3"],
    ["pkg@>1.2.3"],
    ["pkg@<2.0.0"],
    ["pkg@*"],
    ["pkg@x"],
    ["pkg@1.x"],
    ["pkg@1.2.x"],
    ["pkg@1.2.3 || 2.0.0"],
    ["pkg@1.2.3 - 2.0.0"],
    ["pkg@1.2.3+build"],
    ["pkg@01.2.3"],
    ["git+https://example.invalid/r.git"],
    ["github:example/tools"],
    ["http://example.invalid/pkg.tgz"],
    ["https://example.invalid/pkg.tgz"],
    ["file:../pkg"],
    ["./pkg"],
    ["../pkg@1.2.3"],
    ["/abs/pkg@1.2.3"],
    [],
    ["-y"],
    ["-y", "--registry=https://example.invalid", "pkg@1.2.3"],
    ["--package", "pkg@1.2.3", "bin"],
    ["-p", "pkg@1.2.3"],
    ["-c", "pkg@1.2.3"],
    ["--yes", "--call=x", "pkg@1.2.3"],
]


@pytest.mark.parametrize("args", NPX_REFUSED, ids=[" ".join(a) or "no-args" for a in NPX_REFUSED])
def test_npx_requires_an_exact_version(args):
    refused(stdio("npx", args=args), "mcp_runtime_pin_required")


@pytest.mark.parametrize("command", ["NPX", "npx.exe", "Npx.EXE"])
def test_npx_recipe_is_matched_case_insensitively(command):
    refused(stdio(command, args=["pkg@latest"]), "mcp_runtime_pin_required")


NPX_ADMITTED = [
    ["pkg@1.2.3"],
    ["-y", "pkg@1.2.3"],
    ["--yes", "-q", "@example/tools@1.2.3"],
    ["--quiet", "-y", "@example/tools@10.20.30-beta.1"],
    ["@scope/name@0.0.1-rc1", "--flag", "value"],
    ["-y", "pkg@1.2.3", "@latest", "--registry=x"],
]


@pytest.mark.parametrize("args", NPX_ADMITTED, ids=[" ".join(a) for a in NPX_ADMITTED])
def test_npx_exact_versions_are_admitted(args):
    assert parse_one(stdio("npx", args=args)).server.args == tuple(args)


UVX_REFUSED = [
    [],
    ["pkg"],
    ["pkg@1.2.3"],
    ["pkg>=1.2.3"],
    ["pkg~=1.2.3"],
    ["pkg==1.*"],
    ["pkg==*"],
    ["pkg==1.2.3,<2"],
    ["pkg===1.2.3"],
    ["pkg==latest"],
    ["pkg==1.2.3+local"],
    ["pkg!=1.2.3"],
    ["--from", "pkg"],
    ["--from", "pkg==1.*", "cmd"],
    ["--from", "pkg==1.2.3"],
    ["--from", "pkg>=1.2.3", "cmd"],
    ["--from", "git+https://example.invalid/r.git", "cmd"],
    ["--from", "pkg==1.2.3", "--verbose"],
    ["--python", "3.12", "pkg==1.2.3"],
    ["--with", "other", "pkg==1.2.3"],
    ["git+https://example.invalid/r.git"],
]


@pytest.mark.parametrize("args", UVX_REFUSED, ids=[" ".join(a) or "no-args" for a in UVX_REFUSED])
def test_uvx_requires_an_exact_pin(args):
    refused(stdio("uvx", args=args), "mcp_runtime_pin_required")


@pytest.mark.parametrize("args", [["pkg==1.2.3"], ["example-tools==0.1"], ["pkg==2.0.0rc1", "serve"],
                                  ["pkg==1.0.post2"], ["pkg==1!2.0"], ["pkg[extra]==1.2.3"],
                                  ["--from", "pkg==1.2.3", "cmd"], ["--from", "pkg==1.2.3", "cmd", "--flag"]])
def test_uvx_exact_pins_are_admitted(args):
    assert parse_one(stdio("uvx", args=args)).server.args == tuple(args)


@pytest.mark.parametrize("command", ["bunx", "pnpx", "pipx", "BUNX.exe"])
def test_unrecognized_runner_commands_are_refused(command):
    refused(stdio(command, args=["pkg@1.2.3"]), "mcp_runtime_pin_required")


@pytest.mark.parametrize("command", ["npm", "pnpm", "yarn"])
@pytest.mark.parametrize("verb", ["exec", "dlx"])
def test_package_manager_runners_are_refused(command, verb):
    refused(stdio(command, args=[verb, "pkg@1.2.3"]), "mcp_runtime_pin_required")


def test_package_managers_without_a_runner_verb_and_other_commands_are_admitted():
    assert parse_one(stdio("npm", args=["run", "serve"])).server.command == "npm"
    assert parse_one(stdio("my-server", args=[])).server.args == ()
    assert parse_one(stdio("docker", args=["run", "--rm", "image@sha256:abc"])).server.command == "docker"


# --- stdio args ---------------------------------------------------------------


def test_args_count_and_length_bounds():
    assert len(parse_one(stdio("tool", args=["a"] * 64)).server.args) == 64
    refused(stdio("tool", args=["a"] * 65), "mcp_declaration_invalid", "mcp_servers.docs.server.args")
    assert len(parse_one(stdio("tool", args=["a" * 4096])).server.args[0]) == 4096
    refused(stdio("tool", args=["a" * 4097]), "mcp_declaration_invalid", "mcp_servers.docs.server.args.0")


@pytest.mark.parametrize(
    "arg",
    ["a\x00b", "a\nb", "a\tb", "a\x7fb", "\x1b[31m", "a‮b", "​x", "⁦x", "﻿x", "a\ud800b",
     "${GITHUB_TOKEN}", "--token=${GITHUB_TOKEN}", "x${", "${env:HOME}", "$${X}"],
    ids=["nul", "newline", "tab", "del", "esc", "bidi-override", "zero-width", "bidi-isolate", "bom", "surrogate",
         "expansion", "embedded-expansion", "dangling-expansion", "cursor-expansion", "double-dollar"],
)
def test_hostile_args_are_refused(arg):
    refused(stdio("tool", args=["ok", arg]), "mcp_declaration_invalid", "mcp_servers.docs.server.args.1")


@pytest.mark.parametrize("arg", ["$HOME", "$", "{x}", "%PATH%", "a b", "--k=v", "ü", "日本語", "$(x)", "`x`"])
def test_inert_dollar_and_unicode_args_are_admitted(arg):
    assert parse_one(stdio("tool", args=[arg])).server.args == (arg,)


# --- env_names ----------------------------------------------------------------


def test_env_names_are_sorted_and_bounded():
    decl = parse_one(stdio("tool", args=[], env_names=["B_TOKEN", "a_token", "C"]))
    assert decl.server.env_names == ("a_token", "B_TOKEN", "C")
    assert len(parse_one(stdio("tool", args=[], env_names=[f"V{n}" for n in range(32)])).server.env_names) == 32
    refused(stdio("tool", args=[], env_names=[f"V{n}" for n in range(33)]), "mcp_declaration_invalid",
            "mcp_servers.docs.server.env_names")


@pytest.mark.parametrize("names", [["A", "a"], ["TOKEN", "Token"], ["A", "B", "b"]])
def test_env_names_are_unique_ignoring_case(names):
    refused(stdio("tool", args=[], env_names=names), "mcp_declaration_invalid")


@pytest.mark.parametrize("name", ["1A", "A-B", "A B", "", "A.B", "A=B", "A\n", "$A", "Ä", "A" * 129])
def test_bad_env_names_are_refused(name):
    refused(stdio("tool", args=[], env_names=[name]), "mcp_declaration_invalid",
            "mcp_servers.docs.server.env_names.0")


RESERVED_ENV = [
    "PATH", "HOME", "SHELL", "PWD", "IFS", "ENV", "BASH_ENV", "ZDOTDIR", "CDPATH", "PATHEXT", "COMSPEC",
    "SYSTEMROOT", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TMPDIR", "TEMP", "TMP", "CODEX_HOME",
    "CLAUDE_CONFIG_DIR", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "NODE_OPTIONS", "NPM_CONFIG_REGISTRY",
    "PYTHONPATH", "PYTHON", "CSK_HOME", "XDG_CONFIG_HOME", "GIT_SSH_COMMAND", "UV_INDEX_URL", "PIP_INDEX_URL",
]


@pytest.mark.parametrize("name", RESERVED_ENV)
@pytest.mark.parametrize("variant", [str, str.lower, str.title], ids=["upper", "lower", "title"])
def test_reserved_env_names_are_refused_in_any_case(name, variant):
    refused(stdio("tool", args=[], env_names=[variant(name)]), "mcp_env_name_reserved",
            "mcp_servers.docs.server.env_names.0")


@pytest.mark.parametrize("name", ["PATHS", "HOMEDIR", "MY_PATH", "LDX", "NODE", "GITHUB_TOKEN", "UVX", "PIPX_HOME",
                                  "CODEX_API_KEY", "TOOLS_TOKEN", "XDG", "ENVIRON"])
def test_near_miss_env_names_are_admitted(name):
    assert parse_one(stdio("tool", args=[], env_names=[name])).server.env_names == (name,)


# --- http url -----------------------------------------------------------------


URL_REFUSED = [
    "http://example.invalid/mcp",
    "ftp://example.invalid/mcp",
    "example.invalid/mcp",
    "//example.invalid/mcp",
    "https:/example.invalid",
    "https://",
    "https:///mcp",
    "https://user@example.invalid/mcp",
    "https://user:pw@example.invalid/mcp",
    "https://:pw@example.invalid/mcp",
    "https://example.invalid/mcp?x=1",
    "https://example.invalid/mcp?",
    "https://example.invalid?x=1",
    "https://example.invalid/mcp#frag",
    "https://example.invalid#",
    "https://example.invalid/${TOKEN}",
    "https://example.invalid/a$b",
    "https://example.invalid/{x}",
    "https://example.invalid/a}",
    "https://example.invalid\\mcp",
    "https://example.invalid/a b",
    "https://example.invalid/a\tb",
    "https://example.invalid/a\nb",
    "https://example.invalid/‮",
    "https://example.invalid/​",
    "https://example.invalid/a\ud800",
    "https://exämple.invalid/mcp",
    "https://例え.invalid/mcp",
    "https://example.invalid:/mcp",
    "https://example.invalid:0/mcp",
    "https://example.invalid:65536/mcp",
    "https://example.invalid:80a/mcp",
    "https://exa mple.invalid/mcp",
    "https://.example.invalid/mcp",
    "https://exa..mple.invalid/mcp",
    "https://[::1/mcp",
    "https://[nonsense]/mcp",
    "https://example.invalid/" + "a" * 2048,
    " https://example.invalid/mcp",
    "https://example.invalid/mcp ",
]


@pytest.mark.parametrize("url", URL_REFUSED)
def test_bad_urls_are_refused(url):
    refused(http(url=url), "mcp_url_invalid", "mcp_servers.docs.server.url")


@pytest.mark.parametrize(
    "url",
    ["https://example.invalid/mcp", "HTTPS://Example.Invalid/mcp", "https://example.invalid", "https://example.invalid/",
     "https://example.invalid:8443/mcp", "https://127.0.0.1:1/mcp", "https://[::1]:8443/mcp", "https://[::1]/mcp",
     "https://a-b.example.invalid/p%20q/r", "https://example.invalid/" + "a" * 2020, "https://example.invalid:65535/"],
)
def test_good_urls_are_admitted(url):
    assert parse_one(http(url=url)).server.url == url


def test_url_length_boundary():
    prefix = "https://example.invalid/"
    assert parse_one(http(url=prefix + "a" * (2048 - len(prefix)))).server.url is not None
    refused(http(url=prefix + "a" * (2049 - len(prefix))), "mcp_url_invalid")


# --- headers ------------------------------------------------------------------


def test_headers_are_sorted_ignoring_case_and_prefix_defaults_to_none():
    decl = parse_one(http({"X-Key": {"env": "K"}, "authorization": {"env": "T", "prefix": "bearer"}}))
    assert [(h.name, h.env, h.prefix) for h in decl.server.headers] == [
        ("authorization", "T", "bearer"),
        ("X-Key", "K", "none"),
    ]


def test_header_count_bound():
    ok = {f"X-H{n}": {"env": f"E{n}"} for n in range(16)}
    assert len(parse_one(http(ok)).server.headers) == 16
    refused(http({**ok, "X-H16": {"env": "E16"}}), "mcp_declaration_invalid", "mcp_servers.docs.server.headers")


HEADER_NEGATIVES = [
    ("literal-string", {"Authorization": "literal-token"}, "mcp_declaration_invalid"),
    ("literal-bearer-string", {"Authorization": "Bearer abc"}, "mcp_declaration_invalid"),
    ("expansion-string", {"Authorization": "${TOKEN}"}, "mcp_declaration_invalid"),
    ("value-null", {"Authorization": None}, "mcp_declaration_invalid"),
    ("value-list", {"Authorization": ["T"]}, "mcp_declaration_invalid"),
    ("env-missing", {"Authorization": {"prefix": "bearer"}}, "mcp_declaration_invalid"),
    ("env-not-string", {"Authorization": {"env": 1}}, "mcp_declaration_invalid"),
    ("unknown-value-field", {"Authorization": {"env": "T", "value": "x"}}, "mcp_declaration_invalid"),
    ("literal-next-to-env", {"Authorization": {"env": "T", "literal": "x"}}, "mcp_declaration_invalid"),
    ("bad-prefix", {"Authorization": {"env": "T", "prefix": "basic"}}, "mcp_declaration_invalid"),
    ("prefix-not-string", {"Authorization": {"env": "T", "prefix": 1}}, "mcp_declaration_invalid"),
    ("bearer-on-custom-header", {"X-Key": {"env": "T", "prefix": "bearer"}}, "mcp_declaration_invalid"),
    ("bad-env-format", {"X-Key": {"env": "1T"}}, "mcp_declaration_invalid"),
    ("reserved-env", {"X-Key": {"env": "PATH"}}, "mcp_env_name_reserved"),
    ("reserved-env-lowercase", {"Authorization": {"env": "node_options", "prefix": "bearer"}},
     "mcp_env_name_reserved"),
    ("name-with-space", {"X Key": {"env": "T"}}, "mcp_declaration_invalid"),
    ("name-with-colon", {"X:Key": {"env": "T"}}, "mcp_declaration_invalid"),
    ("name-empty", {"": {"env": "T"}}, "mcp_declaration_invalid"),
    ("name-with-newline", {"X-Key\n": {"env": "T"}}, "mcp_declaration_invalid"),
    ("name-non-ascii", {"X-Kéy": {"env": "T"}}, "mcp_declaration_invalid"),
    ("name-too-long", {"X" * 129: {"env": "T"}}, "mcp_declaration_invalid"),
    ("duplicate-ignoring-case", {"Authorization": {"env": "A"}, "authorization": {"env": "B"}},
     "mcp_declaration_invalid"),
    ("duplicate-x-header-case", {"X-Key": {"env": "A"}, "x-KEY": {"env": "B"}}, "mcp_declaration_invalid"),
]
FORBIDDEN_HEADERS = ["Host", "Content-Length", "Transfer-Encoding", "Connection", "Upgrade", "TE", "Trailer",
                     "Keep-Alive", "Cookie", "Proxy-Authorization", "Proxy-Foo", "proxy-x", "HOST", "cookie",
                     "te", "tE"]
HEADER_NEGATIVES += [(f"forbidden-{name}", {name: {"env": "T"}}, "mcp_declaration_invalid")
                     for name in FORBIDDEN_HEADERS]


@pytest.mark.parametrize(("headers", "code"), [row[1:] for row in HEADER_NEGATIVES],
                         ids=[row[0] for row in HEADER_NEGATIVES])
def test_header_negatives(headers, code):
    refused(http(headers), code)


def test_header_name_length_boundary_and_token_characters():
    assert parse_one(http({"X" * 128: {"env": "T"}})).server.headers[0].name == "X" * 128
    assert parse_one(http({"X-Api_Key.1": {"env": "T"}})).server.headers[0].name == "X-Api_Key.1"


def test_two_headers_may_share_one_env_name():
    decl = parse_one(http({"X-A": {"env": "T"}, "X-B": {"env": "T"}}))
    assert [h.env for h in decl.server.headers] == ["T", "T"]


# --- auth ---------------------------------------------------------------------


AUTH_NEGATIVES = [
    ("env-kind-without-reference-http", http(auth={"kind": "env"})),
    ("env-kind-without-reference-stdio", stdio(auth={"kind": "env"})),
    ("none-kind-with-env-names", stdio(env_names=["T"], auth={"kind": "none"})),
    ("none-kind-with-header", http(bearer(), auth={"kind": "none"})),
    ("oauth-kind-with-env-names", stdio(env_names=["T"], auth={"kind": "oauth"})),
    ("oauth-kind-with-header", http(bearer(), auth={"kind": "oauth"})),
    ("oauth-kind-on-stdio", stdio(auth={"kind": "oauth"})),
    ("help-url-http-scheme", http(auth={"kind": "none", "help_url": "http://example.invalid/help"})),
    ("help-url-userinfo", http(auth={"kind": "none", "help_url": "https://u:p@example.invalid/help"})),
    ("help-url-expansion", http(auth={"kind": "none", "help_url": "https://example.invalid/${X}"})),
    ("help-url-whitespace", http(auth={"kind": "none", "help_url": "https://example.invalid/a b"})),
    ("help-url-control", http(auth={"kind": "none", "help_url": "https://example.invalid/‮"})),
    ("help-url-empty-host", http(auth={"kind": "none", "help_url": "https:///x"})),
]


@pytest.mark.parametrize(("declaration",), [(row[1],) for row in AUTH_NEGATIVES], ids=[row[0] for row in AUTH_NEGATIVES])
def test_auth_negatives(declaration):
    refused(declaration, "mcp_declaration_invalid")


def test_auth_valid_combinations_and_help_url_allows_query_and_fragment():
    help_url = "https://example.invalid/account/tokens?tab=api#new"
    decl = parse_one(http(bearer(), auth={"kind": "env", "help_url": help_url}))
    assert decl.auth.help_url == help_url
    assert parse_one(http(auth={"kind": "oauth"})).auth.kind == "oauth"
    assert parse_one(stdio(env_names=["T"], auth={"kind": "env"})).auth.kind == "env"
    assert parse_one(http(auth={"kind": "none"})).auth.help_url is None


# --- authority (D1) ------------------------------------------------------------


AUTHORITY_ROWS = [
    ("stdio-bare", stdio("tool", args=[]), True, False),
    ("stdio-with-env", stdio("tool", args=[], env_names=["T"]), True, True),
    ("http-bare", http(), False, False),
    ("http-oauth", http(auth={"kind": "oauth"}), False, False),
    ("http-header-ref", http({"X-Key": {"env": "T"}}), False, True),
    ("http-bearer-ref", http(bearer()), False, True),
]


@pytest.mark.parametrize(("declaration", "executes", "routes"), [row[1:] for row in AUTHORITY_ROWS],
                         ids=[row[0] for row in AUTHORITY_ROWS])
def test_authority_classification(declaration, executes, routes):
    result = mcp.authority(parse_one(declaration))
    assert (result.executes_code, result.routes_secret) == (executes, routes)
    assert result.consent_required is (executes or routes)


# --- rendering ----------------------------------------------------------------


def render_json(agent: str, declaration: dict) -> str:
    entry = render_native_entry(agent, parse_one(declaration))
    assert entry_digest(entry) is not None
    return json.dumps(entry, separators=(",", ":"))


STDIO_ENV = stdio("npx", args=["-y", "@example/tools@1.2.3"], env_names=["TOOLS_TOKEN", "OTHER_KEY"])
STDIO_PLAIN = stdio("uvx", args=["example-tools==1.2.3"])
STDIO_NO_ARGS = stdio("my-server", args=[])
HTTP_PLAIN = http()
HTTP_BEARER = http(bearer())
HTTP_NONE_PREFIX = http({"X-Api-Key": {"env": "API_KEY"}})
HTTP_BOTH = http({"X-Api-Key": {"env": "API_KEY"}, "Authorization": {"env": "DOCS_TOKEN", "prefix": "bearer"}})
HTTP_AUTH_NO_PREFIX = http({"Authorization": {"env": "DOCS_TOKEN"}})

GOLDEN = {
    ("claude_code", "stdio-env"): '{"type":"stdio","command":"npx","args":["-y","@example/tools@1.2.3"],'
                                 '"env":{"OTHER_KEY":"${OTHER_KEY}","TOOLS_TOKEN":"${TOOLS_TOKEN}"}}',
    ("claude_code", "stdio-plain"): '{"type":"stdio","command":"uvx","args":["example-tools==1.2.3"]}',
    ("claude_code", "stdio-no-args"): '{"type":"stdio","command":"my-server","args":[]}',
    ("claude_code", "http-plain"): '{"type":"http","url":"https://example.invalid/mcp"}',
    ("claude_code", "http-bearer"): '{"type":"http","url":"https://example.invalid/mcp",'
                                   '"headers":{"Authorization":"Bearer ${DOCS_TOKEN}"}}',
    ("claude_code", "http-none-prefix"): '{"type":"http","url":"https://example.invalid/mcp",'
                                        '"headers":{"X-Api-Key":"${API_KEY}"}}',
    ("claude_code", "http-both"): '{"type":"http","url":"https://example.invalid/mcp",'
                                 '"headers":{"Authorization":"Bearer ${DOCS_TOKEN}","X-Api-Key":"${API_KEY}"}}',
    ("claude_code", "http-auth-no-prefix"): '{"type":"http","url":"https://example.invalid/mcp",'
                                           '"headers":{"Authorization":"${DOCS_TOKEN}"}}',
    ("cursor", "stdio-env"): '{"type":"stdio","command":"npx","args":["-y","@example/tools@1.2.3"],'
                            '"env":{"OTHER_KEY":"${env:OTHER_KEY}","TOOLS_TOKEN":"${env:TOOLS_TOKEN}"}}',
    ("cursor", "stdio-plain"): '{"type":"stdio","command":"uvx","args":["example-tools==1.2.3"]}',
    ("cursor", "stdio-no-args"): '{"type":"stdio","command":"my-server","args":[]}',
    ("cursor", "http-plain"): '{"url":"https://example.invalid/mcp"}',
    ("cursor", "http-bearer"): '{"url":"https://example.invalid/mcp","headers":{"Authorization":"Bearer ${env:DOCS_TOKEN}"}}',
    ("cursor", "http-none-prefix"): '{"url":"https://example.invalid/mcp","headers":{"X-Api-Key":"${env:API_KEY}"}}',
    ("cursor", "http-both"): '{"url":"https://example.invalid/mcp","headers":{"Authorization":"Bearer ${env:DOCS_TOKEN}",'
                            '"X-Api-Key":"${env:API_KEY}"}}',
    ("cursor", "http-auth-no-prefix"): '{"url":"https://example.invalid/mcp","headers":{"Authorization":"${env:DOCS_TOKEN}"}}',
    ("codex_cli", "stdio-env"): '{"command":"npx","args":["-y","@example/tools@1.2.3"],"env_vars":["OTHER_KEY","TOOLS_TOKEN"]}',
    ("codex_cli", "stdio-plain"): '{"command":"uvx","args":["example-tools==1.2.3"]}',
    ("codex_cli", "stdio-no-args"): '{"command":"my-server","args":[]}',
    ("codex_cli", "http-plain"): '{"url":"https://example.invalid/mcp"}',
    ("codex_cli", "http-bearer"): '{"url":"https://example.invalid/mcp","bearer_token_env_var":"DOCS_TOKEN"}',
    ("codex_cli", "http-none-prefix"): '{"url":"https://example.invalid/mcp","env_http_headers":{"X-Api-Key":"API_KEY"}}',
    ("codex_cli", "http-both"): '{"url":"https://example.invalid/mcp","bearer_token_env_var":"DOCS_TOKEN",'
                               '"env_http_headers":{"X-Api-Key":"API_KEY"}}',
    ("codex_cli", "http-auth-no-prefix"): '{"url":"https://example.invalid/mcp","env_http_headers":{"Authorization":"DOCS_TOKEN"}}',
}
GOLDEN_INPUTS = {
    "stdio-env": STDIO_ENV, "stdio-plain": STDIO_PLAIN, "stdio-no-args": STDIO_NO_ARGS, "http-plain": HTTP_PLAIN,
    "http-bearer": HTTP_BEARER, "http-none-prefix": HTTP_NONE_PREFIX, "http-both": HTTP_BOTH,
    "http-auth-no-prefix": HTTP_AUTH_NO_PREFIX,
}


@pytest.mark.parametrize(("agent", "case"), list(GOLDEN), ids=lambda value: str(value))
def test_golden_native_entries(agent, case):
    # Compared as serialized text so that key order is part of the contract.
    assert render_json(agent, GOLDEN_INPUTS[case]) == GOLDEN[(agent, case)]


def test_golden_table_is_complete():
    assert set(GOLDEN) == {(agent, case) for agent in mcp.SLICE1_AGENTS for case in GOLDEN_INPUTS}


def test_rendering_ignores_declared_order_of_env_names_and_headers():
    a = http({"X-A": {"env": "A"}, "X-B": {"env": "B"}})
    b = http({"X-B": {"env": "B"}, "X-A": {"env": "A"}})
    for agent in mcp.SLICE1_AGENTS:
        assert render_json(agent, a) == render_json(agent, b)
    c = stdio("tool", args=[], env_names=["A_X", "B_X"])
    d = stdio("tool", args=[], env_names=["B_X", "A_X"])
    for agent in mcp.SLICE1_AGENTS:
        assert render_json(agent, c) == render_json(agent, d)


def test_render_never_emits_resolved_values_or_unmapped_keys():
    entry = render_native_entry("claude_code", parse_one(HTTP_BOTH))
    assert set(entry) == {"type", "url", "headers"}


@pytest.mark.parametrize("agent", ["gemini", "opencode", "windsurf", "nonesuch", ""])
def test_render_refuses_agents_outside_slice1(agent):
    with pytest.raises(McpDeclarationError) as info:
        render_native_entry(agent, parse_one(HTTP_PLAIN))
    assert info.value.code == "mcp_capability_unsupported"


def test_rendered_lists_are_not_shared_with_the_declaration():
    decl = parse_one(STDIO_PLAIN)
    entry = render_native_entry("claude_code", decl)
    assert isinstance(entry["args"], list)
    entry["args"].append("mutated")  # type: ignore[union-attr]
    assert decl.server.args == ("example-tools==1.2.3",)


# --- digests ------------------------------------------------------------------


BASE_STDIO = stdio("npx", args=["-y", "pkg@1.2.3"], env_names=["T_ONE"], auth={"kind": "env", "help_url": "https://example.invalid/h"})
BASE_HTTP = http({"Authorization": {"env": "T_ONE", "prefix": "bearer"}},
                 auth={"kind": "env", "help_url": "https://example.invalid/h"})


def _edit(base: dict, path: tuple, value) -> dict:
    edited = copy.deepcopy(base)
    target = edited
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return edited


STDIO_VARIANTS = {
    "command": _edit(BASE_STDIO, ("server", "command"), "uvx") | {},
    "arg-order": _edit(BASE_STDIO, ("server", "args"), ["pkg@1.2.3", "-y"]),
    "arg-value": _edit(BASE_STDIO, ("server", "args"), ["-y", "pkg@1.2.4"]),
    "arg-added": _edit(BASE_STDIO, ("server", "args"), ["-y", "pkg@1.2.3", "x"]),
    "env-name": _edit(BASE_STDIO, ("server", "env_names"), ["T_TWO"]),
    "env-added": _edit(BASE_STDIO, ("server", "env_names"), ["T_ONE", "T_TWO"]),
    "env-removed": _edit(_edit(BASE_STDIO, ("server", "env_names"), []), ("auth",), {"kind": "none"}),
    "auth-help-url": _edit(BASE_STDIO, ("auth", "help_url"), "https://example.invalid/other"),
    "auth-help-url-removed": _edit(BASE_STDIO, ("auth",), {"kind": "env"}),
    "agents": _edit(BASE_STDIO, ("agents",), ["cursor"]),
    "agents-wider": _edit(BASE_STDIO, ("agents",), ["claude_code", "cursor"]),
}
STDIO_VARIANTS["command"]["server"]["args"] = ["pkg==1.2.3"]
HTTP_VARIANTS = {
    "url": _edit(BASE_HTTP, ("server", "url"), "https://example.invalid/mcp2"),
    "header-env": _edit(BASE_HTTP, ("server", "headers", "Authorization", "env"), "T_TWO"),
    "header-prefix": _edit(BASE_HTTP, ("server", "headers", "Authorization", "prefix"), "none"),
    "header-name": _edit(BASE_HTTP, ("server", "headers"), {"authorization": {"env": "T_ONE", "prefix": "bearer"}}),
    "header-added": _edit(BASE_HTTP, ("server", "headers", "X-Key"), {"env": "T_TWO"}),
    "transport-kind-oauth": _edit(_edit(BASE_HTTP, ("server", "headers"), {}), ("auth",), {"kind": "oauth"}),
}


def _digest(declaration: dict, name: str = "docs") -> str:
    return declaration_digest(parse_one(declaration, name=name))


@pytest.mark.parametrize("base", [BASE_STDIO, BASE_HTTP], ids=["stdio", "http"])
def test_declaration_digest_is_deterministic_hex(base):
    first, second = _digest(base), _digest(copy.deepcopy(base))
    assert first == second
    assert len(first) == 64 and set(first) <= set("0123456789abcdef")


def test_declaration_digest_changes_with_every_normalized_field():
    base = _digest(BASE_STDIO)
    digests = {label: _digest(variant) for label, variant in STDIO_VARIANTS.items()}
    assert all(value != base for value in digests.values()), digests
    assert len(set(digests.values())) == len(digests)
    assert _digest(BASE_STDIO, name="other") != base
    base_http = _digest(BASE_HTTP)
    digests = {label: _digest(variant) for label, variant in HTTP_VARIANTS.items()}
    assert all(value != base_http for value in digests.values()), digests
    assert len(set(digests.values())) == len(digests)


def test_declaration_digest_is_independent_of_input_order_of_unordered_fields():
    a = stdio("tool", args=["x"], env_names=["A_X", "B_X"])
    b = stdio("tool", args=["x"], env_names=["B_X", "A_X"])
    assert _digest(a) == _digest(b)
    c = http({"X-A": {"env": "A"}, "X-B": {"env": "B"}})
    d = http({"X-B": {"env": "B"}, "X-A": {"env": "A"}})
    assert _digest(c) == _digest(d)


def test_declaration_digest_changes_with_target_agent_resolution():
    narrow = parse_one(http(), targets=["claude_code"])
    wide = parse_one(http(), targets=TARGETS)
    assert declaration_digest(narrow) != declaration_digest(wide)


def test_declaration_digest_uses_its_domain():
    from csk import protocol_json

    decl = parse_one(http())
    bare = hashlib.sha256(protocol_json.canonical_bytes({"name": "docs"})).hexdigest()
    assert declaration_digest(decl) != bare
    entry = render_native_entry("cursor", decl)
    canonical = protocol_json.canonical_bytes(entry)
    assert entry_digest(entry) == hashlib.sha256(b"csk-mcp-entry-v1\0" + canonical).hexdigest()
    assert entry_digest(entry) != hashlib.sha256(b"csk-mcp-declaration-v1\0" + canonical).hexdigest()
    assert entry_digest(entry) != hashlib.sha256(canonical).hexdigest()


def test_entry_digest_is_deterministic_and_key_order_independent():
    value = {"type": "http", "url": URL, "headers": {"A": "b", "C": "d"}}
    reordered = {"headers": {"C": "d", "A": "b"}, "url": URL, "type": "http"}
    assert entry_digest(value) == entry_digest(copy.deepcopy(value)) == entry_digest(reordered)


ENTRY_EDITS = [
    {"type": "http", "url": URL + "2", "headers": {"A": "b"}},
    {"type": "sse", "url": URL, "headers": {"A": "b"}},
    {"type": "http", "url": URL, "headers": {"A": "c"}},
    {"type": "http", "url": URL, "headers": {"B": "b"}},
    {"type": "http", "url": URL, "headers": {}},
    {"type": "http", "url": URL},
    {"type": "http", "url": URL, "headers": {"A": "b"}, "extra": 1},
    {"type": "http", "url": URL, "headers": {"A": "b"}, "extra": True},
    {"type": "http", "url": URL, "headers": {"A": "b"}, "extra": None},
    {"type": "http", "url": URL, "headers": {"A": "b"}, "extra": []},
]


def test_entry_digest_changes_with_every_field():
    base = {"type": "http", "url": URL, "headers": {"A": "b"}}
    digests = [entry_digest(edit) for edit in ENTRY_EDITS]
    assert None not in digests
    assert len(set(digests)) == len(digests) and entry_digest(base) not in digests


def test_entry_digest_distinguishes_value_types():
    assert len({entry_digest({"a": 1}), entry_digest({"a": "1"}), entry_digest({"a": True}),
                entry_digest({"a": [1]}), entry_digest({"a": None})}) == 5


@pytest.mark.parametrize(
    "value",
    [
        {"a": 1.5},
        {"a": 1.0},
        {"a": float("nan")},
        {"a": datetime.datetime(2026, 10, 13)},
        {"a": datetime.date(2026, 10, 13)},
        {1: "a"},
        {None: "a"},
        {("a",): "b"},
        {"a": ("t", "u")},
        {"a": {1, 2}},
        {"a": b"bytes"},
        {"a": object()},
        {"a": 2**60},
        {"a": "x\ud800"},
        {"a": {1: "nested"}},
        {"a": [1, 2.5]},
        1.5,
        datetime.datetime(2026, 10, 13),
    ],
    ids=["float", "float-integral", "nan", "datetime", "date", "int-key", "none-key", "tuple-key", "tuple-value",
         "set", "bytes", "object", "unsafe-int", "lone-surrogate", "nested-int-key", "float-in-list",
         "top-float", "top-datetime"],
)
def test_entry_digest_is_none_when_not_ccj1_representable(value):
    assert entry_digest(value) is None


def test_entry_digest_is_none_for_a_self_referential_value():
    cyclic: dict = {}
    cyclic["self"] = cyclic
    assert entry_digest(cyclic) is None


@pytest.mark.parametrize("value", [{}, [], "text", 7, True, None, {"a": [1, {"b": None}]}])
def test_entry_digest_accepts_ccj1_values(value):
    assert isinstance(entry_digest(value), str)


# --- check_requirements --------------------------------------------------------


def req(name="sheets", hint="Connect Sheets.", transport=None, required_in="any") -> McpServerRequirement:
    return McpServerRequirement(name=name, hint=hint, transport=transport, required_in=required_in)


def server(agent, name="sheets", origin="planned", transport="http", state="active") -> EffectiveServer:
    return EffectiveServer(name=name, agent=agent, origin=origin, transport=transport, state=state)


def run(requirement, view, targets=TARGETS, skill="skill-a"):
    return check_requirements({skill: {requirement.name: requirement}}, targets, view)


def test_any_is_satisfied_by_one_agent():
    report = run(req(), [server("cursor")])
    assert report.errors == [] and report.warnings == []
    assert report.found == {"skill-a": {"sheets": ["cursor"]}}


def test_any_error_text_matches_the_installer():
    report = run(req(), [])
    assert report.errors == [
        "MCP server 'sheets' required by skill-a is not configured in any target agent environment. "
        "Hint: Connect Sheets."
    ]
    assert report.found == {"skill-a": {"sheets": []}}


def test_all_requires_every_target_agent():
    one = run(req(required_in="all"), [server("cursor")])
    assert one.errors == [
        "MCP server 'sheets' required by skill-a is not configured for agent(s): claude_code, codex_cli. "
        "Hint: Connect Sheets."
    ]
    assert one.found["skill-a"]["sheets"] == ["cursor"]
    every = run(req(required_in="all"), [server(agent) for agent in TARGETS])
    assert every.errors == [] and every.found["skill-a"]["sheets"] == sorted(TARGETS)


def test_all_over_unselected_agent_stays_missing():
    report = run(req(required_in="all"), [server("cursor")], targets=["cursor", "gemini"])
    assert "gemini" in report.errors[0] and "cursor" not in report.errors[0].split("agent(s):")[1]


def test_entries_for_agents_outside_the_targets_do_not_count():
    assert run(req(), [server("gemini")], targets=["cursor"]).errors != []
    assert run(req(required_in="all"), [server("cursor"), server("gemini")], targets=["cursor"]).errors == []


def test_planned_and_external_both_satisfy():
    assert run(req(), [server("cursor", origin="external")]).errors == []
    assert run(req(), [server("cursor", origin="planned")]).errors == []
    mixed = run(req(required_in="all"), [server("claude_code", origin="planned"),
                                         server("codex_cli", origin="external"),
                                         server("cursor", origin="external")])
    assert mixed.errors == []


@pytest.mark.parametrize("origin", ["planned", "external"])
@pytest.mark.parametrize("state", ["disabled", "rejected"])
def test_inactive_entries_never_satisfy(origin, state):
    report = run(req(), [server("cursor", origin=origin, state=state)])
    assert report.errors and report.found["skill-a"]["sheets"] == []
    report = run(req(required_in="all"), [server(a, origin=origin, state=state) for a in TARGETS])
    assert report.errors


def test_an_active_entry_beats_a_disabled_twin():
    view = [server("cursor", origin="external", state="disabled"), server("cursor", origin="planned")]
    assert run(req(), view).errors == []


def test_entries_are_matched_by_name():
    report = run(req(), [server("cursor", name="other")])
    assert report.errors and report.found["skill-a"]["sheets"] == []


def test_transport_mismatch_on_planned_entry_does_not_satisfy():
    report = run(req(transport="http"), [server("cursor", transport="stdio")])
    assert report.found["skill-a"]["sheets"] == []
    assert report.errors[0].startswith("MCP server 'sheets' required by skill-a is not configured in any target")
    assert any("cursor" in error and "transport" in error for error in report.errors[1:])


def test_transport_mismatch_applies_only_to_planned_entries():
    for transport in ("stdio", "sse", "unknown", "http"):
        assert run(req(transport="http"), [server("cursor", origin="external", transport=transport)]).errors == []
    assert run(req(transport="stdio"), [server("cursor", transport="http")]).errors != []
    assert run(req(transport="http"), [server("cursor", transport="http")]).errors == []


def test_planned_entries_are_not_checked_when_the_requirement_declares_no_transport():
    for transport in ("stdio", "http", "sse", "unknown"):
        assert run(req(transport=None), [server("cursor", transport=transport)]).errors == []


def test_mismatching_planned_agent_does_not_fail_a_satisfied_any_requirement():
    view = [server("cursor", transport="stdio"), server("claude_code", transport="http")]
    report = run(req(transport="http"), view)
    assert report.errors == [] and report.found["skill-a"]["sheets"] == ["claude_code"]


def test_all_with_one_mismatching_planned_agent_fails():
    view = [server("cursor", transport="stdio"), server("claude_code"), server("codex_cli")]
    report = run(req(transport="http", required_in="all"), view)
    assert report.errors and "cursor" in report.errors[0]


def test_external_entry_rescues_a_planned_mismatch_on_the_same_agent():
    view = [server("cursor", transport="stdio"), server("cursor", origin="external", transport="stdio")]
    assert run(req(transport="http"), view).errors == []


def test_multiple_skills_and_requirements_report_independently():
    requirements = {
        "skill-a": {"sheets": req(), "docs": req("docs", hint="Docs.")},
        "skill-b": {"sheets": req(required_in="all")},
        "skill-c": {},
    }
    view = [server("cursor"), server("claude_code", name="docs")]
    report = check_requirements(requirements, TARGETS, view)
    assert report.found == {
        "skill-a": {"sheets": ["cursor"], "docs": ["claude_code"]},
        "skill-b": {"sheets": ["cursor"]},
    }
    assert len(report.errors) == 1 and "skill-b" in report.errors[0]
    assert report.warnings == []


def test_view_may_be_a_one_shot_iterator_and_targets_are_deduplicated():
    report = check_requirements({"skill-a": {"sheets": req(required_in="all")}}, ["cursor", "cursor"],
                                iter([server("cursor")]))
    assert report.errors == []


# --- purity ---------------------------------------------------------------------


def test_module_is_pure_and_unwired():
    import ast

    source = Path(mcp.__file__).read_text("utf-8")
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(name=node.module or "")])
    }
    assert not imported & {"os", "pathlib", "subprocess", "socket", "urllib", "time", "datetime", "shutil", "tempfile",
                           "http", "ssl", "sys", "io"}
    src = Path(mcp.__file__).parent
    callers = [path.name for path in src.rglob("*.py") if "mcp_declarations" in path.read_text("utf-8")
               and path.name != "mcp_declarations.py"]
    assert callers == []


def test_public_api_touches_no_ambient_state(monkeypatch):
    # Behavioural counterpart of the import scan above: a dynamic import or any
    # other route to the filesystem, environment, clock or network fails here.
    import builtins
    import os
    import socket
    import subprocess
    import time

    def boom(*args, **kwargs):
        raise AssertionError("ambient access from a pure module")

    for owner, names in (
        (builtins, ("open",)),
        (os, ("stat", "lstat", "getcwd", "getenv", "listdir", "scandir", "environ", "system", "popen")),
        (socket, ("socket", "getaddrinfo", "create_connection")),
        (subprocess, ("Popen", "run")),
        (time, ("time", "monotonic", "time_ns", "perf_counter")),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, boom)
    try:
        raw = {"docs": HTTP_BOTH, "tools": STDIO_ENV}
        parsed = parse_mcp_servers(raw, invocation_scope="project", target_agents=TARGETS)
        for decl in parsed.values():
            mcp.authority(decl)
            declaration_digest(decl)
            for agent in mcp.SLICE1_AGENTS:
                entry_digest(render_native_entry(agent, decl))
        check_requirements({"s": {"docs": req("docs")}}, TARGETS, [server("cursor", name="docs")])
        for bad in ({"x": {}}, {"x": http(url="http://x")}, {"x": stdio("bash")}):
            with pytest.raises(McpDeclarationError):
                parse_mcp_servers(bad, invocation_scope="project", target_agents=TARGETS)
    finally:
        # pytest itself reads os.environ between the call and the fixture teardown.
        monkeypatch.undo()
