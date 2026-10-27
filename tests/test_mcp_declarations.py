from __future__ import annotations

import copy
import datetime
import hashlib
import json
import unicodedata
from dataclasses import asdict, replace
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


# Every wrapper in the product table refuses regardless of payload pinning or executable case/suffix.
@pytest.mark.parametrize("command", sorted(mcp._INDIRECTION_COMMANDS | mcp._SHELL_COMMANDS | mcp._EVALUATOR_LAUNCHERS))
@pytest.mark.parametrize("suffix", ["", ".exe"])
@pytest.mark.parametrize("payload", [["npx", "pkg"], ["npx", "pkg@1.2.3"], ["-e", "do shell script 'true'"]])
def test_wrapper_table_cannot_hide_runner(command, suffix, payload):
    refused(stdio(command=command.upper() + suffix, args=payload), "mcp_command_invalid")
    assert parse_one(stdio(command="npx", args=["pkg@1.2.3"])).server.command == "npx"
    assert parse_one(stdio()).server.command == "uvx"


# Independently listed launchers must stay in the refusal tables; generation alone cannot detect omissions.
def test_required_wrapper_coverage():
    assert {"nice", "arch", "caffeinate", "timeout", "stdbuf", "chrt", "taskset", "ionice",
            "time", "command", "exec", "launchctl", "start"} <= mcp._INDIRECTION_COMMANDS
    assert {"osascript", "open"} <= mcp._EVALUATOR_LAUNCHERS
    assert {"cmd", "powershell", "pwsh"} <= mcp._SHELL_COMMANDS


# Literal credential signatures in URLs/arguments refuse without echoing their values; normal URLs remain valid.
@pytest.mark.parametrize("secret", ["ghp_0123456789abcdefghijklmnop", "API_TOKEN=private-example-value", "sk-example0123456789abcdef"])
@pytest.mark.parametrize("location", ["url", "args"])
def test_secret_material_is_not_publishable(secret, location):
    declaration = http(url=URL + "/" + secret) if location == "url" else stdio(command="my-server", args=[secret])
    exc = refused(declaration, "mcp_declaration_invalid")
    assert secret not in str(exc)
    assert parse_one(http()).server.url == URL


# Public error paths preserve server names that happen to equal former internal field names.
@pytest.mark.parametrize("name", ["auth", "agents", "scope", "git_policy"])
def test_public_error_path_keeps_server_name(name):
    refused(http(url="http://example.invalid"), "mcp_url_invalid", f"mcp_servers.{name}.server.url", name=name)


def parse_one(declaration: dict, *, name: str = "docs", scope: str = "project", targets=None):
    return parse_mcp_servers(
        {name: declaration},
        invocation_scope=scope,
        target_agents=TARGETS if targets is None else targets,
        allow_deprecated=True,  # Historical fixtures exercise the private legacy reader.
    )[name]


def stdio(command: str = "uvx", args=("example-tools==1.2.3",), env_names=(), auth=None, **top) -> dict:
    server = {"transport": "stdio", "command": command, "args": list(args), "env_names": list(env_names)}
    return object_decl(server, auth=auth, **top)


def http(headers=None, url: str = URL, auth=None, **top) -> dict:
    return object_decl({"transport": "http", "url": url}, headers=headers, auth=auth, **top)


def object_decl(server, *, headers=None, auth=None, environments=None, **extensions):
    if environments is not None:
        server["environments"] = environments
    if auth is not None:
        extensions["auth"] = auth
    if headers is not None:
        extensions["headers"] = headers
    return {"server": server, "install": extensions}


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
    # The vector table pairs known unsupported selection with grammar/capability refusals.
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


# These cases prove the closed public declaration grammar rejects invalid inputs.
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
    refused(http(), "mcp_declaration_invalid", "mcp_servers.<invalid name #0>", name=name)


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
    ("cwd-top-field", {**http(), "cwd": "/"}, "mcp_declaration_invalid", "mcp_servers.docs.cwd"),
    ("enabled-top-field", {**http(), "enabled": True}, "mcp_declaration_invalid", "mcp_servers.docs.enabled"),
    ("not-an-object", [], "mcp_declaration_invalid", "mcp_servers.docs"),
    ("unknown-top-field", {**http(), "trust": True}, "mcp_declaration_invalid", "mcp_servers.docs.trust"),
    ("env-top-field", {**http(), "env": {"A": "b"}}, "mcp_declaration_invalid", "mcp_servers.docs.env"),
    ('server-and-reference', {**http(), 'reference': {'kind': 'git'}}, 'mcp_declaration_invalid', 'mcp_servers.docs.reference'),
    ("neither-server-nor-reference", {'install': {'auth': {'kind': 'none'}}}, "mcp_declaration_invalid", "mcp_servers.docs"),
    ("auth-not-object", {**http(), 'install': {'auth': 'none'}}, "mcp_declaration_invalid", "mcp_servers.docs.install.auth"),
    ("auth-unknown-field", {**http(), 'install': {'auth': {'kind': 'none', 'token': 'x'}}}, "mcp_declaration_invalid",
     "mcp_servers.docs.install.auth.token"),
    ("auth-kind-missing", {**http(), 'install': {'auth': {}}}, "mcp_declaration_invalid", "mcp_servers.docs.install.auth.kind"),
    ("auth-kind-unknown", {**http(), 'install': {'auth': {'kind': 'basic'}}}, "mcp_declaration_invalid",
     "mcp_servers.docs.install.auth.kind"),
    ("auth-help-url-not-string", {**http(), 'install': {'auth': {'kind': 'none', 'help_url': 3}}}, "mcp_declaration_invalid",
     "mcp_servers.docs.install.auth.help_url"),
    ("server-not-object", {'server': 'x', 'install': {'auth': {'kind': 'none'}}}, "mcp_declaration_invalid",
     "mcp_servers.docs.server"),
    ("transport-missing", {'server': {'url': URL}, 'install': {'auth': {'kind': 'none'}}}, "mcp_declaration_invalid",
     "mcp_servers.docs.server.transport"),
    ("transport-unknown", {'server': {'transport': 'ws', 'url': URL}, 'install': {'auth': {'kind': 'none'}}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.transport"),
    ("transport-not-string", {'server': {'transport': 1, 'url': URL}, 'install': {'auth': {'kind': 'none'}}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.transport"),
    ("http-with-command", {'server': {'transport': 'http', 'url': URL, 'command': 'x'}, 'install': {'auth': {'kind': 'none'}}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.command"),
    ("http-with-trust", {'server': {'transport': 'http', 'url': URL, 'trust': True}, 'install': {'auth': {'kind': 'none'}}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.trust"),
    ("http-without-url", {'server': {'transport': 'http'}, 'install': {'auth': {'kind': 'none'}}}, "mcp_declaration_invalid",
     "mcp_servers.docs.server.url"),
    ("http-url-not-string", {'server': {'transport': 'http', 'url': 1}, 'install': {'auth': {'kind': 'none'}}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.url"),
    ("http-headers-not-object", {'server': {'transport': 'http', 'url': URL}, 'install': {'auth': {'kind': 'none'}, 'headers': []}}, "mcp_declaration_invalid",
     "mcp_servers.docs.install.headers"),
    ("stdio-with-url", {'server': {'transport': 'stdio', 'command': 'uvx', 'url': URL}, 'install': {'auth': {'kind': 'none'}}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.url"),
    ("stdio-with-headers", {'server': {'transport': 'stdio', 'command': 'uvx'}, 'install': {'auth': {'kind': 'none'}, 'headers': {}}}, "mcp_declaration_invalid", "mcp_servers.docs.install.headers"),
    ("stdio-with-cwd", {'server': {'transport': 'stdio', 'command': 'uvx', 'cwd': '/'}, 'install': {'auth': {'kind': 'none'}}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.cwd"),
    ("stdio-with-literal-env", {'server': {'transport': 'stdio', 'command': 'uvx', 'env': {'A': 'b'}}, 'install': {'auth': {'kind': 'none'}}}, "mcp_declaration_invalid", "mcp_servers.docs.server.env"),
    ("stdio-without-command", {'server': {'transport': 'stdio'}, 'install': {'auth': {'kind': 'none'}}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.command"),
    ("stdio-command-not-string", {'server': {'transport': 'stdio', 'command': ['uvx']}, 'install': {'auth': {'kind': 'none'}}},
     "mcp_declaration_invalid", "mcp_servers.docs.server.command"),
    ("stdio-args-not-list", {'server': {'transport': 'stdio', 'command': 'uvx', 'args': 'x'}, 'install': {'auth': {'kind': 'none'}}}, "mcp_declaration_invalid", "mcp_servers.docs.server.args"),
    ("stdio-arg-not-string", {'server': {'transport': 'stdio', 'command': 'uvx', 'args': [1]}, 'install': {'auth': {'kind': 'none'}}}, "mcp_declaration_invalid", "mcp_servers.docs.server.args"),
    ("stdio-env-names-not-list", {'server': {'transport': 'stdio', 'command': 'uvx', 'env_names': 'A'}, 'install': {'auth': {'kind': 'none'}}}, "mcp_declaration_invalid",
     "mcp_servers.docs.server.env_names"),
    ('reference-refused', {'reference': {'kind': 'git'}, 'install': {'auth': {'kind': 'none'}}}, 'mcp_declaration_invalid', 'mcp_servers.docs.reference'),
    ('reference-with-agents-refused', {'reference': {'kind': 'file'}, 'install': {'auth': {'kind': 'none'}}}, 'mcp_declaration_invalid', 'mcp_servers.docs.reference'),
    ('reference-requires-auth', {'reference': {'kind': 'git'}}, 'mcp_declaration_invalid', 'mcp_servers.docs.reference'),
    ("sse-refused", {'server': {'transport': 'sse', 'url': 'https://example.invalid/sse'}, 'install': {'auth': {'kind': 'none'}}}, "mcp_capability_unsupported", "mcp_servers.docs.server.transport"),
]


@pytest.mark.parametrize(("declaration", "code", "field"), [row[1:] for row in DECLARATION_NEGATIVES],
                         ids=[row[0] for row in DECLARATION_NEGATIVES])
def test_declaration_negatives(declaration, code, field):
    refused(declaration, code, field)


# These cases prove environment selection and project-only capability boundaries.
# --- agents, scope, git_policy ------------------------------------------------


def test_omitted_agents_resolve_to_slice1_targets_sorted():
    decl = parse_one(http(), targets=["cursor", "gemini", "claude_code"])
    assert decl.agents == mcp.SLICE1_AGENTS


def test_explicit_agents_are_sorted():
    assert parse_one(http(environments=["cursor", "claude_code"])).agents == ("claude_code", "cursor")


# Canonical environment ids are accepted; aliases still refuse even when listed as install targets.
@pytest.mark.parametrize(("canonical", "aliases"), [
    ("claude_code", ["claude", "claude-code", "Claude_Code"]),
    ("codex_cli", ["codex", "codex-cli", "Codex_Cli"]),
    ("cursor", ["cursor_ai", "cursor-cli", "Cursor"]),  # Input-only legacy lane in parse_one.
])
def test_environment_ids_have_no_aliases(canonical, aliases):
    assert parse_one(http(environments=[canonical]), targets=[canonical]).agents == (canonical,)
    for alias in aliases:
        refused(http(environments=[alias]), "mcp_declaration_invalid",
                "mcp_servers.docs.server.environments.0", targets=[canonical, alias])


AGENT_NEGATIVES = [
    ("empty-list", http(environments=[]), TARGETS, "mcp_declaration_invalid"),
    ("duplicate", http(environments=["cursor", "cursor"]), TARGETS, "mcp_declaration_invalid"),
    ("unknown-agent", http(environments=["nonesuch"]), TARGETS + ["nonesuch"], "mcp_declaration_invalid"),
]


@pytest.mark.parametrize(("declaration", "targets", "code"), [row[1:] for row in AGENT_NEGATIVES],
                         ids=[row[0] for row in AGENT_NEGATIVES])
def test_agent_selection_negatives(declaration, targets, code):
    refused(declaration, code, targets=targets)


@pytest.mark.parametrize("agents", ["cursor", [1], {"a": 1}])
def test_agents_must_be_a_list_of_strings(agents):
    refused(http(environments=agents), "mcp_declaration_invalid", "mcp_servers.docs.server.environments")


def test_unknown_agent_is_selection_error_not_capability():
    error = refused(http(environments=["nonesuch"]), "mcp_declaration_invalid", targets=TARGETS + ["nonesuch"])
    assert error.field == "mcp_servers.docs.server.environments.0"


SCOPE_NEGATIVES = [
    ("user-in-project", http(scope="user"), "project", "mcp_capability_unsupported"),
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
    refused(http(scope="user", git_policy="shared", environments=["windsurf"]), "mcp_capability_unsupported")


def test_agent_selection_precedes_capability():
    refused(http(git_policy="shared", environments=["nonesuch"]), "mcp_declaration_invalid", targets=["claude_code"])


def test_syntax_precedes_everything():
    refused(http(scope="user", trust=True), "mcp_declaration_invalid")


def test_every_declaration_passes_each_step_before_the_next_step_runs():
    raw = {"a": http(git_policy="shared"), "b": http(environments=["nonesuch"])}
    with pytest.raises(McpDeclarationError) as info:
        parse_mcp_servers(raw, invocation_scope="project", target_agents=TARGETS)
    assert info.value.code == "mcp_declaration_invalid"


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
    refused(stdio(command, args=["server.js", flag]), "mcp_command_invalid", "mcp_servers.docs.server.command")


def test_evaluator_arguments_are_refused_for_cased_and_exe_commands():
    refused(stdio("Python.EXE", args=["-c", "x"]), "mcp_command_invalid")


# A script path does not remove an evaluator's execution capability.
def test_evaluator_commands_without_eval_arguments_are_refused():
    for command, args in [("python3", ["server.py", "--port", "8080"]), ("node", ["server.js"])]:
        refused(stdio(command, args=args), "mcp_command_invalid", "mcp_servers.docs.server.command")


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
    ["pkg@1.2.3+build"],
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
                                  ["--from", "pkg==1.2.3", "pkg-cli"], ["--from", "pkg==1.2.3", "pkg-cli", "--flag"]])
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

# SemVer 2.0.0: the valid and invalid examples of the specification and of its official regex suite.
SEMVER_VALID = [
    "1.0.0-0.3.7", "1.0.0-x.7.z.92", "1.0.0-x-y-z.--", "1.0.0-alpha+001", "1.0.0+20130313144700",
    "1.0.0-beta+exp.sha.5114f85", "1.0.0+21AF26D3----117B344092BD", "1.0.0+001", "1.0.0+build.001",
    "0.0.4", "1.2.3", "10.20.30", "1.1.2-prerelease+meta", "1.1.2+meta", "1.1.2+meta-valid", "1.0.0-alpha",
    "1.0.0-beta", "1.0.0-alpha.beta", "1.0.0-alpha.beta.1", "1.0.0-alpha.1", "1.0.0-alpha0.valid",
    "1.0.0-alpha.0valid", "1.0.0-alpha-a.b-c-somethinglong+build.1-aef.1-its-okay", "1.0.0-rc.1+build.1",
    "2.0.0-rc.1+build.123", "1.2.3-beta", "10.2.3-DEV-SNAPSHOT", "1.2.3-SNAPSHOT-123", "1.0.0", "2.0.0", "1.1.7",
    "2.0.0+build.1848", "2.0.1-alpha.1227", "1.0.0-alpha+beta", "1.2.3----RC-SNAPSHOT.12.9.1--.12+788",
    "1.2.3----R-S.12.9.1--.12+meta", "1.2.3----RC-SNAPSHOT.12.9.1--.12", "1.0.0+0.build.1-rc.10000aaa-kk-0.1",
    "99999999999999999999999.999999999999999999.99999999999999999", "1.0.0-0A.is.legal", "1.2.3-0", "1.2.3-0.0",
    "1.2.3-a.10", "0.0.0",
]
SEMVER_INVALID = [
    "1", "1.2", "1.2.3-0123", "1.2.3-0123.0123", "1.1.2+.123", "+invalid", "-invalid", "-invalid+invalid",
    "-invalid.01", "alpha", "alpha.beta", "alpha.beta.1", "alpha.1", "alpha+beta", "alpha_beta", "alpha.", "alpha..",
    "beta", "1.0.0-alpha_beta", "-alpha.", "1.0.0-alpha..", "1.0.0-alpha..1", "1.0.0-alpha...1", "01.1.1",
    "1.01.1", "1.1.01", "1.2.3.DEV", "1.2-SNAPSHOT", "1.2.31.2.3----RC-SNAPSHOT.12.09.1--..12+788",
    "1.2-RC-SNAPSHOT", "-1.0.3-gamma+b7718", "+justmeta", "9.8.7+meta+meta", "9.8.7-whatever+meta+meta",
    "99999999999999999999999.999999999999999999.99999999999999999----RC-SNAPSHOT.12.09.1--------------------------"
    "------..12",
    # numeric prerelease identifiers with leading zeros, the rev-1 bypasses
    "1.2.3-01", "1.2.3-alpha.01", "1.2.3-00.beta", "1.2.3-00", "1.2.3-0.00", "1.2.3-1.02.3",
    "1.2.3+", "1.2.3-", "1.2.3-+x", "1.2.3+a..b", "1.2.3 ", "１.２.３",
]


@pytest.mark.parametrize("version", SEMVER_VALID)
def test_npx_semver_spec_valid_examples_are_admitted(version):
    args = [f"pkg@{version}"]
    assert parse_one(stdio("npx", args=args)).server.args == tuple(args)


@pytest.mark.parametrize("version", SEMVER_INVALID, ids=repr)
def test_npx_semver_spec_invalid_examples_are_refused(version):
    refused(stdio("npx", args=[f"pkg@{version}"]), "mcp_runtime_pin_required")
    refused(stdio("npx", args=[f"@scope/pkg@{version}"]), "mcp_runtime_pin_required")


# PEP 440: the specification's own examples and the spellings its normalization admits.
PEP440_VALID = [
    "1.0-dev1", "1.0_dev1", "1.0dev1", "1.0.a1-dev", "1.0a1_dev2", "1.0.post1-dev2",
    "1.0.dev456", "1.0a1", "1.0a2.dev456", "1.0a12.dev456", "1.0a12", "1.0b1.dev456", "1.0b2", "1.0b2.post345.dev456",
    "1.0b2.post345", "1.0rc1.dev456", "1.0rc1", "1.0", "1.0+abc.5", "1.0+abc.7", "1.0+5", "1.0.post456.dev34",
    "1.0.post456", "1.1.dev1", "1.2.3+local", "1.2.3+local.1", "1.2.3RC1", "1.2.3rc1", "v1.2.3", "V1.2.3",
    "1.2.3-post1", "1.2.3.dev", "1.2.3alpha1", "1.2.3-alpha-1", "1.2.3_beta_2", "1.2.3.pre4", "1.2.3c1",
    "1.0.post0", "1!2.0", "0!1.0", "1!2.0.post1.dev3+x.y", "1.0-1", "1.0.rev3", "1.0-r3", "1.0.post", "1.0a",
    "1.0.dev", "1.0_post_1", "2.0.0rc1", "00.01", "1.0+ABC", "1.0+a-b_c.d",
]
PEP440_INVALID = [
    "1.0+", "1.0+_a", "1.0+a_", "", "1.", "1..0", "a1.0", "1.0.0.", "1.0-", "1.*", "1.0.*", "*", "1.0+a+b", "1!",
    "!1.0", "1.0 ", " 1.0", "1.0++a", "1.0abc", "1.0.0-1.2", "1.0,<2", "1.0,", "1.2.3==1", "latest", "1.0;x",
    "１.０", "1.0+ä", "1.0+a.", "1.0+.a", "1.0 .post1", "1.0.post1.post2", "1.0.dev1.dev2",
]


@pytest.mark.parametrize("version", PEP440_VALID)
@pytest.mark.parametrize("form", ["direct", "from"])
def test_uvx_pep440_valid_versions_are_admitted(version, form):
    args = [f"pkg=={version}"] if form == "direct" else ["--from", f"pkg=={version}", "pkg"]
    assert parse_one(stdio("uvx", args=args)).server.args == tuple(args)


@pytest.mark.parametrize("version", PEP440_INVALID, ids=repr)
@pytest.mark.parametrize("form", ["direct", "from"])
def test_uvx_pep440_invalid_versions_are_refused(version, form):
    args = [f"pkg=={version}"] if form == "direct" else ["--from", f"pkg=={version}", "pkg"]
    refused(stdio("uvx", args=args), "mcp_runtime_pin_required")


@pytest.mark.parametrize("spec", ["pkg===1.0", "pkg>=1.0", "pkg<=1.0", "pkg~=1.0", "pkg!=1.0", "pkg<1.0", "pkg>1.0",
                                  "pkg=1.0", "pkg==", "pkg", "pkg==1.0 ; python_version>'3'", "pkg@1.0",
                                  "pkg[x]===1.0", "==1.0", "pkg ==1.0"])
@pytest.mark.parametrize("form", ["direct", "from"])
def test_uvx_operators_other_than_exact_equality_are_refused(spec, form):
    args = [spec] if form == "direct" else ["--from", spec, "pkg"]
    refused(stdio("uvx", args=args), "mcp_runtime_pin_required")


@pytest.mark.parametrize("flags", [["-q"], ["--quiet"], ["--offline"], ["--no-cache"], ["-n"],
                                   ["-q", "--offline", "--no-cache", "-n", "--quiet"]])
@pytest.mark.parametrize("form", ["direct", "from"])
def test_uvx_boolean_flags_may_precede_the_package(flags, form):
    tail = ["pkg==1.2.3"] if form == "direct" else ["--from", "pkg==1.2.3", "pkg-cli"]
    args = flags + tail
    assert parse_one(stdio("uvx", args=args)).server.args == tuple(args)


@pytest.mark.parametrize(
    "flags",
    [["--with", "other"], ["--with=other"], ["--python", "3.12"], ["-p", "3.12"], ["--index-url", "https://x.invalid"],
     ["--index-url=https://x.invalid"], ["--from=pkg==1.2.3"], ["--isolated"], ["--no-config"], ["-w", "other"],
     ["--offline", "--with", "other"], ["-q", "--python=3.12"], ["--env-file", "x"], ["-"], ["--"]],
    ids=lambda flags: " ".join(flags),
)
@pytest.mark.parametrize("form", ["direct", "from"])
def test_uvx_value_flags_are_refused_before_the_package(flags, form):
    tail = ["pkg==1.2.3"] if form == "direct" else ["--from", "pkg==1.2.3", "pkg-cli"]
    refused(stdio("uvx", args=flags + tail), "mcp_runtime_pin_required")


def test_uvx_flags_only_and_flag_after_from_are_refused():
    refused(stdio("uvx", args=["--offline"]), "mcp_runtime_pin_required")
    refused(stdio("uvx", args=["--offline", "--from"]), "mcp_runtime_pin_required")
    refused(stdio("uvx", args=["--from", "pkg==1.2.3", "--offline"]), "mcp_runtime_pin_required")


def test_uvx_flags_after_the_package_belong_to_the_tool():
    args = ["pkg==1.2.3", "--with", "x", "--python", "3.12"]
    assert parse_one(stdio("uvx", args=args)).server.args == tuple(args)


EVALUATOR_FORMS = [
    ("node", ["--eval=1"]), ("node", ["--eval", "1"]), ("node", ["--evaluate"]), ("node", ["-e", "1"]),
    ("node", ["-e1"]), ("node", ["-pe", "1"]), ("node", ["-p"]), ("node", ["--print=1"]), ("nodejs", ["-pe=1"]),
    ("python", ["-cprint(1)"]), ("python", ["-c=print(1)"]), ("python", ["-ec"]), ("python", ["-c", "1"]),
    ("perl", ["-pe", "1"]), ("perl", ["-e1"]), ("perl", ["-ple", "1"]), ("ruby", ["-e1"]), ("ruby", ["--eval=1"]),
    ("php", ["-pe"]), ("bun", ["--print=1"]), ("bun", ["--eval", "1"]), ("deno", ["eval", "1"]),
    ("deno", ["--eval=1"]), ("deno", ["-e1"]), ("deno", ["run", "eval"]),
    ("python3.12", ["-c", "print(1)"]), ("python3.12", ["-cprint(1)"]), ("python3.12", ["server.py", "-c"]),
    ("python2", ["-c1"]), ("python3.", ["-c1"]), ("python.3", ["-c1"]),
    ("Python3.12.EXE", ["-c1"]), ("NODE.exe", ["--eval=1"]), ("Deno", ["eval"]),
    ("PERL", ["-pe", "1"]), ("Ruby.Exe", ["-e1"]), ("python311", ["-c1"]), ("python3.12.exe", ["--print=1"]),
]


@pytest.mark.parametrize(("command", "args"), EVALUATOR_FORMS, ids=lambda value: str(value))
def test_glued_and_combined_evaluator_forms_are_refused(command, args):
    error = refused(stdio(command, args=args), "mcp_command_invalid")
    assert error.field == "mcp_servers.docs.server.command"


@pytest.mark.parametrize(
    ("command", "args"),
    [("node", ["server.js", "--port=8080"]), ("node", ["--inspect", "server.js"]), ("node", ["--experimental-vm"]),
     ("python3.12", ["-m", "server", "--port", "1"]), ("python", ["-u", "server.py"]), ("python", ["-m", "pkg.eval"]),
     ("deno", ["run", "-A", "server.ts"]), ("bun", ["run", "server.ts"]), ("ruby", ["server.rb"]),
     ("php", ["server.php"]), ("pythonista", ["-c"]), ("python-foo", ["-c"]), ("nodemon", ["-e", "js"]),
     ("perlbrew", ["-e"]), ("denoise", ["eval"]), ("bunx2", ["-c"]), ("my-python", ["-c"])],
    ids=lambda value: str(value),
)
# Plain scripts still use evaluator commands; unrelated lookalike executable
# names retain the documented opaque-program boundary.
def test_evaluator_lookalikes_and_scripts_respect_capability_boundary(command, args):
    if command.lower() in {"node", "python3.12", "python", "deno", "bun", "ruby", "php"}:
        refused(stdio(command, args=args), "mcp_command_invalid", "mcp_servers.docs.server.command")
    else:
        assert parse_one(stdio(command, args=args)).server.args == tuple(args)


@pytest.mark.parametrize("args", [["x", "pkg@1.2.3"], ["x", "pkg@latest"], ["--yes", "x", "pkg@1.2.3"],
                                  ["--prefix", "dir", "x", "pkg@1.2.3"], ["x"]])
def test_npm_x_is_a_refused_runner_alias(args):
    refused(stdio("npm", args=args), "mcp_runtime_pin_required", "mcp_servers.docs.server.command")
    refused(stdio("NPM.exe", args=args), "mcp_runtime_pin_required", "mcp_servers.docs.server.command")


def test_npm_x_alias_is_scoped_to_npm():
    # The revision-3 closed list also refuses pnpm/yarn x. Unlisted commands stay inert.
    assert parse_one(stdio("my-npm", args=["x", "serve"])).server.command == "my-npm"
    assert parse_one(stdio("tool", args=["x"])).server.command == "tool"



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


# Proxy, resolver and launcher controls refuse in both reference surfaces and cases; prefixed app names remain valid.
@pytest.mark.parametrize("name", [
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "NO_PROXY", "RES_OPTIONS",
    "HOSTALIASES", "LOCALDOMAIN", "WINDIR", "__PYVENV_LAUNCHER__",
])
@pytest.mark.parametrize("variant", [str, str.lower], ids=["upper", "lower"])
@pytest.mark.parametrize("surface", ["stdio", "header"])
def test_interoperable_reserved_env_names(name, variant, surface):
    name = variant(name)
    allowed = "APP_" + name
    if surface == "stdio":
        assert parse_one(stdio("tool", args=[], env_names=[allowed])).server.env_names == (allowed,)
        body = stdio("tool", args=[], env_names=[name])
        field = "mcp_servers.docs.server.env_names.0"
    else:
        assert parse_one(http({"X-Key": {"env": allowed}})).server.headers[0].env == allowed
        body = http({"X-Key": {"env": name}})
        field = "mcp_servers.docs.install.headers.X-Key.env"
    refused(body, "mcp_env_name_reserved", field)


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
    "https://example.invalid/a\\b",
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


IPV6_REFUSED = [
    # Unicode zone identifiers (the panels' reproductions)
    "https://[::1%é]/mcp", "https://[fe80::1%接口]:443/mcp", "https://[fe80::1%é]/mcp", "https://[::1%日本]/",
    # ASCII zone identifiers and their percent-encodings
    "https://[fe80::1%eth0]/mcp", "https://[fe80::1%25eth0]/mcp", "https://[fe80::1%25]/mcp",
    "https://[fe80::1%]/mcp", "https://[::1%1]:8443/mcp", "https://[fe80::1%25%C3%A9]/mcp", "https://[::1%25é]/mcp",
    # Unicode inside a bracketed literal without a zone
    "https://[::１]/mcp", "https://[é::1]/mcp", "https://[::1é]/",
    # malformed literals and ports
    "https://[::1", "https://[::1]]/", "https://[not-ip]/", "https://[::1]:0/", "https://[::1]:65536/",
    "https://[::1]:/", "https://[::1]suffix/", "https://[]/", "https://[1.2.3.4]/", "https://[::1]x:80/",
    # the same ASCII rule for every other host form
    "https://exa%CC%81mple.invalid/", "https://exa%mple.invalid/", "https://é.invalid/", "https://xn--é.invalid/",
    "https://１２７.0.0.1/",
]


@pytest.mark.parametrize("url", IPV6_REFUSED)
def test_ipv6_zone_identifiers_and_non_ascii_hosts_are_refused(url):
    refused(http(url=url), "mcp_url_invalid", "mcp_servers.docs.server.url")


@pytest.mark.parametrize("url", IPV6_REFUSED)
def test_help_url_applies_the_same_host_rules(url):
    refused(http(auth={"kind": "none", "help_url": url}), "mcp_declaration_invalid", "mcp_servers.docs.install.auth.help_url")


@pytest.mark.parametrize("url", ["https://[::1]/mcp", "https://[::1]:8443/mcp", "https://[2001:db8::1]/",
                                 "https://[2001:DB8::ff]:1/x", "https://[::ffff:192.0.2.1]/", "https://[fe80::1]/"])
def test_plain_ipv6_literals_are_admitted(url):
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
    refused(http({**ok, "X-H16": {"env": "E16"}}), "mcp_declaration_invalid", "mcp_servers.docs.install.headers")


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
    "command-only": _edit(BASE_STDIO, ("server", "command"), "tool"),
    "command": _edit(BASE_STDIO, ("server", "command"), "uvx") | {},
    "arg-order": _edit(BASE_STDIO, ("server", "args"), ["pkg@1.2.3", "-y"]),
    "arg-value": _edit(BASE_STDIO, ("server", "args"), ["-y", "pkg@1.2.4"]),
    "arg-added": _edit(BASE_STDIO, ("server", "args"), ["-y", "pkg@1.2.3", "x"]),
    "env-name": _edit(BASE_STDIO, ("server", "env_names"), ["T_TWO"]),
    "env-added": _edit(BASE_STDIO, ("server", "env_names"), ["T_ONE", "T_TWO"]),
    "env-removed": _edit(_edit(BASE_STDIO, ("server", "env_names"), []), ("install", "auth",), {"kind": "none"}),
    "auth-help-url": _edit(BASE_STDIO, ("install", "auth", "help_url"), "https://example.invalid/other"),
    "auth-help-url-removed": _edit(BASE_STDIO, ("install", "auth",), {"kind": "env"}),
    "agents": _edit(BASE_STDIO, ("server", "environments",), ["cursor"]),
    "agents-wider": _edit(BASE_STDIO, ("server", "environments",), ["claude_code", "cursor"]),
}
STDIO_VARIANTS["command"]["server"]["args"] = ["pkg==1.2.3"]
HTTP_VARIANTS = {
    "url": _edit(BASE_HTTP, ("server", "url"), "https://example.invalid/mcp2"),
    "header-env": _edit(BASE_HTTP, ("install", "headers", "Authorization", "env"), "T_TWO"),
    "header-prefix": _edit(BASE_HTTP, ("install", "headers", "Authorization", "prefix"), "none"),
    "header-name": _edit(BASE_HTTP, ("install", "headers"), {"authorization": {"env": "T_ONE", "prefix": "bearer"}}),
    "header-added": _edit(BASE_HTTP, ("install", "headers", "X-Key"), {"env": "T_TWO"}),
    "transport-kind-oauth": _edit(_edit(BASE_HTTP, ("install", "headers"), {}), ("install", "auth",), {"kind": "oauth"}),
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
    # Isolate auth.kind on a valid remote declaration with no environment references.
    assert _digest(http(auth={"kind": "none"})) != _digest(http(auth={"kind": "oauth"}))
    # Dataclass fields fixed by the slice-1 parser are still part of the frozen digest API.
    decl = parse_one(http())
    assert declaration_digest(replace(decl, scope="user")) != declaration_digest(decl)
    assert declaration_digest(replace(decl, git_policy="shared")) != declaration_digest(decl)
    assert declaration_digest(replace(decl, server=replace(decl.server, transport="stdio"))) != declaration_digest(decl)
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


def test_declaration_digest_preserves_absence_independent_of_agent_resolution():
    narrow = parse_one(http(), targets=["claude_code"])
    wide = parse_one(http(), targets=TARGETS)
    assert declaration_digest(narrow) == declaration_digest(wide)


def test_declaration_digest_uses_its_domain():
    from csk import protocol_json

    decl = parse_one(http(environments=TARGETS))
    normalized = json.loads(json.dumps(asdict(decl)))
    normalized.pop("unsupported_agents")
    normalized.pop("environments")
    normalized.pop("local_agents")
    # Optional launcher fields do not change the pre-launcher digest contract.
    normalized["server"].pop("launcher")
    normalized["server"].pop("resolved_launcher")
    canonical = protocol_json.canonical_bytes(normalized)
    assert declaration_digest(decl) == hashlib.sha256(b"csk-mcp-declaration-v1\0" + canonical).hexdigest()
    assert declaration_digest(decl) != hashlib.sha256(canonical).hexdigest()
    assert declaration_digest(decl) != entry_digest(normalized)
    assert entry_digest(normalized) == hashlib.sha256(b"csk-mcp-entry-v1\0" + canonical).hexdigest()
    assert declaration_digest(decl) == "21589f9ba2f82559637066e7f9f079b16e5ec9e1aa1d3e88f12f22eb0bbc525b"
    assert entry_digest(normalized) == "5a8a871bf2b51b37f2604446e3e3d946dff37910bce805547f0d2ad772cf5c72"
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


def test_module_remains_pure_when_install_is_wired():
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
    # Read-only check and installation share the pure grammar; host resolution
    # composes it separately without introducing I/O into the grammar module.
    assert sorted(callers) == ["cli.py", "mcp_command_resolution.py", "mcp_local_targets.py", "mcp_project.py", "mcp_settings.py", "skillfile_compat.py"]


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


IPV6_REFUSED = [
    # Unicode zone identifiers (the panels' reproductions)
    "https://[::1%é]/mcp", "https://[fe80::1%接口]:443/mcp", "https://[fe80::1%é]/mcp", "https://[::1%日本]/",
    # ASCII zone identifiers and their percent-encodings
    "https://[fe80::1%eth0]/mcp", "https://[fe80::1%25eth0]/mcp", "https://[fe80::1%25]/mcp",
    "https://[fe80::1%]/mcp", "https://[::1%1]:8443/mcp", "https://[fe80::1%25%C3%A9]/mcp", "https://[::1%25é]/mcp",
    # Unicode inside a bracketed literal without a zone
    "https://[::１]/mcp", "https://[é::1]/mcp", "https://[::1é]/",
    # malformed literals and ports
    "https://[::1", "https://[::1]]/", "https://[not-ip]/", "https://[::1]:0/", "https://[::1]:65536/",
    "https://[::1]:/", "https://[::1]suffix/", "https://[]/", "https://[1.2.3.4]/", "https://[::1]x:80/",
    # the same ASCII rule for every other host form
    "https://exa%CC%81mple.invalid/", "https://exa%mple.invalid/", "https://é.invalid/", "https://xn--é.invalid/",
    "https://１２７.0.0.1/",
]


@pytest.mark.parametrize("url", IPV6_REFUSED)
def test_ipv6_zone_identifiers_and_non_ascii_hosts_are_refused(url):
    refused(http(url=url), "mcp_url_invalid", "mcp_servers.docs.server.url")


@pytest.mark.parametrize("url", IPV6_REFUSED)
def test_help_url_applies_the_same_host_rules(url):
    refused(http(auth={"kind": "none", "help_url": url}), "mcp_declaration_invalid", "mcp_servers.docs.install.auth.help_url")


@pytest.mark.parametrize("url", ["https://[::1]/mcp", "https://[::1]:8443/mcp", "https://[2001:db8::1]/",
                                 "https://[2001:DB8::ff]:1/x", "https://[::ffff:192.0.2.1]/", "https://[fe80::1]/"])
def test_plain_ipv6_literals_are_admitted(url):
    assert parse_one(http(url=url)).server.url == url


# These cases prove unsupported forms never gain authority through nested fields.
# --- unsupported forms are validated before they are refused (A7) ----------------


def sse(url: str = "https://example.invalid/sse", auth=None, headers=None, **extra) -> dict:
    return object_decl({"transport": "sse", "url": url, **extra}, headers=headers, auth=auth or {"kind": "none"})


def test_valid_sse_is_refused_as_a_capability():
    refused(sse(), "mcp_capability_unsupported", "mcp_servers.docs.server.transport")


SSE_INVALID = [
    ("cwd", sse(cwd="repo"), "mcp_declaration_invalid", "mcp_servers.docs.server.cwd"),
    ("command", sse(command="x"), "mcp_declaration_invalid", "mcp_servers.docs.server.command"),
    ("args", sse(args=[]), "mcp_declaration_invalid", "mcp_servers.docs.server.args"),
    ("env-names", sse(env_names=["A"]), "mcp_declaration_invalid", "mcp_servers.docs.install.auth.kind"),
    ("trust", sse(trust=True), "mcp_declaration_invalid", "mcp_servers.docs.server.trust"),
    ("no-url", {'server': {'transport': 'sse'}, 'install': {'auth': {'kind': 'none'}}}, "mcp_declaration_invalid",
     "mcp_servers.docs.server.url"),
    ("url-not-string", sse(url=3), "mcp_declaration_invalid", "mcp_servers.docs.server.url"),
    ("headers-not-object", sse(headers=[]), "mcp_declaration_invalid", "mcp_servers.docs.install.headers"),
    ("literal-header", sse(headers={"Authorization": "literal"}), "mcp_declaration_invalid",
     "mcp_servers.docs.install.headers.Authorization"),
    ("header-unknown-field", sse(headers={"X": {"env": "A", "x": 1}}, auth={"kind": "env"}),
     "mcp_declaration_invalid", "mcp_servers.docs.install.headers.X.x"),
    ("http-url", sse(url="http://example.invalid/sse"), "mcp_url_invalid", "mcp_servers.docs.server.url"),
    ("url-userinfo", sse(url="https://u:p@example.invalid/sse"), "mcp_url_invalid", "mcp_servers.docs.server.url"),
    ("url-zone", sse(url="https://[fe80::1%eth0]/sse"), "mcp_url_invalid", "mcp_servers.docs.server.url"),
    ("url-query", sse(url="https://example.invalid/sse?x=1"), "mcp_url_invalid", "mcp_servers.docs.server.url"),
    ("reserved-header-env", sse(headers={"X": {"env": "PATH"}}, auth={"kind": "env"}), "mcp_env_name_reserved",
     "mcp_servers.docs.install.headers.X.env"),
    ("forbidden-header", sse(headers={"Host": {"env": "A"}}, auth={"kind": "env"}), "mcp_declaration_invalid",
     "mcp_servers.docs.install.headers.Host"),
    ("auth-env-without-reference", sse(auth={"kind": "env"}), "mcp_declaration_invalid",
     "mcp_servers.docs.install.auth.kind"),
    ("auth-none-with-reference", sse(headers={"X": {"env": "A"}}), "mcp_declaration_invalid",
     "mcp_servers.docs.install.auth.kind"),
    ("help-url", sse(auth={"kind": "none", "help_url": "http://example.invalid"}), "mcp_declaration_invalid",
     "mcp_servers.docs.install.auth.help_url"),
    ("scope-unsupported", {**sse(), "install": {"scope": "user"}}, "mcp_capability_unsupported", "mcp_servers.docs.install.scope"),
]


@pytest.mark.parametrize(("declaration", "code", "field"), [row[1:] for row in SSE_INVALID],
                         ids=[row[0] for row in SSE_INVALID])
def test_sse_is_validated_like_http_before_it_is_refused(declaration, code, field):
    refused(declaration, code, field)


def test_sse_steps_one_and_two_precede_the_capability_and_agent_steps():
    refused(object_decl({"transport": "sse", "url": URL, "cwd": "x"}, environments=["windsurf"], git_policy="shared"), "mcp_declaration_invalid",
            "mcp_servers.docs.server.cwd")
    refused(object_decl({"transport": "sse", "url": "http://x.invalid"}, environments=["windsurf"], git_policy="shared"), "mcp_url_invalid")
    refused(object_decl({"transport": "sse", "url": URL}, environments=["nonesuch"]), "mcp_declaration_invalid", targets=TARGETS)


def test_sse_with_oauth_and_headerless_auth_is_still_only_a_capability_refusal():
    refused(sse(auth={"kind": "oauth"}), "mcp_capability_unsupported")


HELP_BAD = "http://example.invalid/help"


@pytest.mark.parametrize("payload", [{"kind": "git"}, {}, {"url": "https://example.invalid/r", "sha256": "0" * 64},
                                     {"anything": [1, {"x": None}]}])
def test_reference_contents_do_not_admit_a_removed_public_field(payload):
    refused({'reference': payload, 'install': {'auth': {'kind': 'none'}}}, "mcp_declaration_invalid",
            "mcp_servers.docs.reference")


@pytest.mark.parametrize("payload", [[], "git", None, 3, True, []])
def test_reference_payload_must_be_an_object(payload):
    refused({'reference': payload, 'install': {'auth': {'kind': 'none'}}}, "mcp_declaration_invalid", "mcp_servers.docs.reference")


@pytest.mark.parametrize("auth", [
    {"kind": "none", "help_url": HELP_BAD},
    {"kind": "none", "help_url": "https://user@example.invalid/"},
    {"kind": "none", "help_url": "https://[fe80::1%eth0]/"},
    {"kind": "none", "help_url": "https://exämple.invalid/"},
    {"kind": "none", "help_url": "https://example.invalid/${X}"},
    {"kind": "none", "help_url": 3},
], ids=["http", "userinfo", "zone", "unicode-host", "expansion", "not-a-string"])
@pytest.mark.parametrize("form", ["sse", "stdio", "http"])
def test_auth_help_url_is_validated_whatever_the_server_form(auth, form):
    declaration = {
        "reference": {"reference": {"kind": "git"}},
        "sse": {"server": {"transport": "sse", "url": "https://example.invalid/sse"}},
        "stdio": {"server": {"transport": "stdio", "command": "tool"}},
        "http": {"server": {"transport": "http", "url": URL}},
    }[form]
    refused({**declaration, 'install': {'auth': auth}}, "mcp_declaration_invalid", "mcp_servers.docs.install.auth.help_url")


@pytest.mark.parametrize("auth", [{}, {"kind": "basic"}, {"kind": "none", "token": "x"}, "none", None])
def test_reference_is_unknown_even_with_an_invalid_auth_shape(auth):
    refused({'reference': {'kind': 'git'}, 'install': {'auth': auth}}, "mcp_declaration_invalid")


def test_reference_with_a_valid_help_url_is_an_unknown_field():
    refused({'reference': {'kind': 'git'}, 'install': {'auth': {'kind': 'env', 'help_url': 'https://example.invalid/h?x=1#y'}}},
            "mcp_declaration_invalid", "mcp_servers.docs.reference")


def test_reference_unknown_field_precedes_scope_capability():
    refused({'reference': {'kind': 'git'}, 'install': {'auth': {'kind': 'none'}, 'scope': 'user'}}, "mcp_declaration_invalid")


# --- map keys are step-1 syntax ---------------------------------------------------


def test_every_name_is_checked_before_any_declaration_body():
    # "a" sorts first and has a broken body; the later bad name still wins because
    # all map keys are validated before any body is shaped.
    raw = {"a": {'server': 3, 'install': {'auth': {'kind': 'none'}}}, "bad name": http()}
    with pytest.raises(McpDeclarationError) as info:
        parse_mcp_servers(raw, invocation_scope="project", target_agents=TARGETS)
    assert (info.value.code, info.value.field) == ("mcp_declaration_invalid", "mcp_servers.<invalid name #1>")


def test_duplicate_names_are_reported_before_any_declaration_body():
    raw = {"Docs": http(), "a": [], "docs": http()}
    with pytest.raises(McpDeclarationError) as info:
        parse_mcp_servers(raw, invocation_scope="project", target_agents=TARGETS)
    assert (info.value.code, info.value.field) == ("mcp_declaration_invalid", "mcp_servers.docs")


def test_a_bad_name_precedes_a_step_two_error_in_another_declaration():
    raw = {"a": http(url="http://example.invalid"), "con": http()}
    with pytest.raises(McpDeclarationError) as info:
        parse_mcp_servers(raw, invocation_scope="project", target_agents=TARGETS)
    assert info.value.field == "mcp_servers.<invalid name #1>"


# --- revision 3: the recorded robustness findings -------------------------------


HOSTILE_KEYS = ["bad\x1b[31mkey", "bad\nkey", "bad\u202ekey", "bad\ud800key", "x" * 100_000,
                "bad.key", "", 42]


@pytest.mark.parametrize("key", HOSTILE_KEYS, ids=lambda key: repr(key)[:40])
@pytest.mark.parametrize("surface", ["name", "declaration", "server", "auth", "header", "header-ref"])
def test_errors_never_echo_unvalidated_keys_or_names(surface, key):
    raw = {"first": http(), "docs": http()}
    body = raw["docs"]
    if surface == "name":
        raw = {"good": http(), key: http()}
    elif surface == "header":
        body["install"]["headers"] = {key: {"env": "TOKEN", "prefix": "bearer"}}
    elif surface == "header-ref":
        body["install"]["headers"] = {"X-Key": {"env": "TOKEN", key: True}}
    else:
        if surface == "auth":
            body["install"]["auth"] = {"kind": "none"}
        target = body if surface == "declaration" else body["install"]["auth"] if surface == "auth" else body[surface]
        target[key] = True
    with pytest.raises(McpDeclarationError) as info:
        parse_mcp_servers(raw, invocation_scope="project", target_agents=TARGETS)
    error = info.value
    assert error.code == "mcp_declaration_invalid"
    for output in (error.field, str(error)):
        assert len(output) <= 768
        assert all(unicodedata.category(char) not in ("Cc", "Cf", "Cs") for char in output)
        if isinstance(key, str) and key:
            assert key not in output
    if surface == "name":
        assert error.field == "mcp_servers.<invalid name #1>"
    else:
        assert "<invalid name #1>" in error.field


def test_invalid_name_index_is_declared_insertion_index_not_sorted_index():
    with pytest.raises(McpDeclarationError) as info:
        parse_mcp_servers({"z": http(), "bad name": http(), "y": http()},
                          invocation_scope="project", target_agents=TARGETS)
    assert info.value.field == "mcp_servers.<invalid name #1>"


@pytest.mark.parametrize("agents", [["pi", "opencode", "claude_code"], ["opencode", "claude_code"],
                                  ["muse", "pi", "opencode", "claude_code"]])
def test_known_agents_are_partitioned_for_writing_and_reporting(agents):
    # Explicit known ids outside project targets remain writable or reporting-only, never refused.
    declaration = parse_one(http(environments=agents), targets=["codex_cli"])
    assert declaration.agents == tuple(sorted(a for a in agents if a in mcp.SLICE1_AGENTS))
    assert declaration.unsupported_agents == tuple(sorted(a for a in agents if a not in mcp.SLICE1_AGENTS))


RUNNER_ALIAS_CASES = [
    ("pnpm", ["x", "pkg"]), ("yarn", ["x", "pkg"]), ("uv", ["tool", "run", "pkg"]),
    ("uv", ["run", "pkg"]), ("bun", ["x", "pkg@latest"]),
    ("npm", ["init", "pkg"]), ("npm", ["create", "pkg"]), ("pnpm", ["create", "pkg"]),
    ("yarn", ["create", "pkg"]), ("bun", ["create", "pkg"]), ("deno", ["run", "npm:pkg@latest"]),
    ("npx.cmd", ["pkg@latest"]), ("NPX.BAT", ["pkg@latest"]), ("pnpm.cmd", ["dlx", "pkg"]),
    ("bunx.cmd", ["pkg"]),
]


@pytest.mark.parametrize(("command", "args"), RUNNER_ALIAS_CASES, ids=[f"{cmd}-{'-'.join(args[:2])}" for cmd, args in RUNNER_ALIAS_CASES])
def test_panel_runner_aliases_are_refused(command, args):
    refused(stdio(command, args=args), "mcp_command_invalid" if command in {"bun", "deno"} else "mcp_runtime_pin_required")


@pytest.mark.parametrize(("command", "args"), [("bun", ["run", "server.ts"]), ("uv", ["sync"]),
                                             ("deno", ["run", "server.ts"]), ("deno", ["run", "./npm:server.ts"]),
                                             ("tool", ["create", "x", "run"]), ("uvtool", ["run", "pkg"])])
# Evaluator-capable runners refuse all forms; ordinary non-evaluator names
# retain the closed runner-recipe interpretation.
def test_runner_alias_list_is_closed(command, args):
    if command in {"bun", "deno"}:
        refused(stdio(command, args=args), "mcp_command_invalid", "mcp_servers.docs.server.command")
    else:
        assert parse_one(stdio(command, args=args)).server.args == tuple(args)


@pytest.mark.parametrize("name", ["-x", "--x", ".x", "_x", "x-", "x.", "x_"])
@pytest.mark.parametrize("form", ["direct", "from"])
def test_uvx_package_name_must_start_and_end_alphanumeric(name, form):
    args = [f"{name}==1.0"] if form == "direct" else ["--from", f"{name}==1.0", "cmd"]
    refused(stdio("uvx", args=args), "mcp_runtime_pin_required")


@pytest.mark.parametrize("name", ["x", "1", "x-y", "x_y", "x.y", "x--y"])
@pytest.mark.parametrize("form", ["direct", "from"])
def test_uvx_package_name_interior_punctuation_is_admitted(name, form):
    args = [f"{name}==1.0"] if form == "direct" else ["--from", f"{name}==1.0", "pkg-cli"]
    assert parse_one(stdio("uvx", args=args)).server.args == tuple(args)


def test_help_url_refuses_backslash_in_path():
    refused(http(auth={"kind": "none", "help_url": "https://example.invalid/a\\b"}),
            "mcp_declaration_invalid", "mcp_servers.docs.install.auth.help_url")


# Independent contract inventory, deliberately not derived from the product table.
_ARGUMENT_RUNNERS = (
    'npx', 'uvx', 'bunx', 'pipx', 'uv', 'npm', 'pnpm', 'yarn', 'bun', 'deno',
    'node', 'nodejs', 'python', 'python3', 'python27', 'python3.14', 'ruby', 'perl',
    'php', 'sh', 'bash', 'zsh', 'dash', 'ksh', 'fish', 'mksh', 'csh', 'tcsh',
    'cmd', 'powershell', 'pwsh', 'osascript', 'open', 'env', 'pnpx',
)
_ARGUMENT_WRAPPERS = (
    'setsid', 'flock', 'unbuffer', 'chronic', 'find', 'unknown-wrapper',
    'busybox', 'xargs', 'script', 'nohup', 'sudo', 'doas', 'nice',
)
_TOKEN_FORMS = (
    '{}', '{}.exe', '{}.cmd', '{}.bat', '{}.ps1', '{}.com',
    './{}', '/usr/bin/{}', 'C:\\bin\\{}.PS1',
)


# Every independent runner spelling refuses at beginning/middle/end of unknown-wrapper args.
# Known wrappers also refuse; the unknown wrappers are the witness for the structural invariant.
@pytest.mark.parametrize('wrapper', _ARGUMENT_WRAPPERS)
@pytest.mark.parametrize('form', _TOKEN_FORMS)
def test_runner_in_args_inventory(wrapper, form):
    for runner in _ARGUMENT_RUNNERS:
        for token in (form.format(runner), form.format(runner.upper())):
            for position in (0, 1, 4):
                args = ['ordinary', '--port', '1', 'tail']
                args.insert(position, token)
                exc = refused(stdio(wrapper, args=args), 'mcp_command_invalid')
                if wrapper == 'unknown-wrapper':
                    assert exc.field == f'mcp_servers.docs.server.args.{position}'
                    assert repr(token.replace("\\", "/").rsplit("/", 1)[-1]) in str(exc)
    assert parse_one(stdio('my-server', args=['--port', '1'])).server.command == 'my-server'


# Direct command aliases obey pin/evaluation checks; the suffix does not bypass policy.
@pytest.mark.parametrize('suffix', ['', '.exe', '.cmd', '.bat', '.ps1', '.com'])
def test_direct_command_aliases(suffix):
    for runner, package in [('npx', 'pkg@1.2.3'), ('uvx', 'pkg==1.2.3')]:
        command = runner.upper() + suffix
        assert parse_one(stdio(command, args=[package])).server.command == command
        refused(stdio(command, args=['pkg']), 'mcp_runtime_pin_required')
    for command, args in [('nice', ['npx', 'pkg']), ('cmd', ['/c', 'true']),
                          ('osascript', ['-e', 'true']), ('python', ['-c', 'print(1)'])]:
        refused(stdio(command.upper() + suffix, args=args), 'mcp_command_invalid')


# Fail closed even when a launcher token was intended as data, with its exact field/message.
def test_runner_argument_used_as_data():
    exc = refused(stdio('my-server', args=['--tool', 'node']), 'mcp_command_invalid',
                  'mcp_servers.docs.server.args.1')
    assert "'node'" in str(exc)
    assert parse_one(stdio('my-server', args=['--tool', 'ordinary'])).server.command == 'my-server'


# Pinning the distribution never authorizes a shell/runner in the --from command slot.
def test_uvx_from_runner_is_refused():
    refused(stdio('uvx', args=['--from', 'pkg==1.2.3', 'cmd']), 'mcp_command_invalid',
            'mcp_servers.docs.server.args.2')
    assert parse_one(stdio('uvx', args=['--from', 'pkg==1.2.3', 'pkg-cli'])).server.command == 'uvx'


# Independent cross-platform inventory: each evaluator refuses even inert argv;
# versioned interpreter families are checked separately from the product table.
_EVALUATORS = (
    "awk gawk mawk nawk sed gsed perl perl5.34 python python3 pythonw python3.14 pypy pypy3 "
    "ruby ruby3.3 php php8.3 node nodejs deno bun lua lua5.4 luajit luajit2.1 "
    "tclsh tclsh8.6 wish wish8.6 Rscript R osascript open sh ash bash dash zsh fish ksh ksh93 "
    "mksh csh tcsh cmd powershell pwsh wscript cscript mshta rundll32 regsvr32 "
    "xargs find gfind make gmake ed ex vi vim nvim emacs gdb lldb git ssh busybox toybox env "
    "script expect tar gtar rsync zip sort split watch less more forfiles "
    "nice setsid flock chronic unbuffer nohup sudo doas su arch caffeinate timeout gtimeout "
    "stdbuf gstdbuf chrt taskset ionice time command exec launchctl start "
    "hush rbash rksh chroot chpst setpriv runuser nsenter unshare prlimit run-parts "
    "start-stop-daemon at batch crontab systemd-run systemctl service schtasks "
    "automator shortcuts xdg-open gio gtk-launch xterm gnome-terminal konsole "
    "bsdtar cpio man info sdiff wmic m4 sqlite3 dc scp sftp openssl rpm apt apt-get "
    "dpkg dnf yum apk gcc g++ cc c++ clang clang++ ld ar"
).split()


@pytest.mark.parametrize("command", _EVALUATORS)
@pytest.mark.parametrize("suffix", ["", ".EXE", ".cmd", ".bat", ".ps1", ".com"])
def test_evaluator_command_inventory(command, suffix):
    refused(stdio(command.upper() + suffix, args=[]), "mcp_command_invalid",
            "mcp_servers.docs.server.command")


# Word boundaries are the specified ASCII alphabet, not shell parsing. A known
# name embedded in executable-looking text or ordinary data fails closed alike.
@pytest.mark.parametrize("form", ["a={} pkg", 'system("{} pkg")', "--exec={}", "'{}'", "{}@latest",
                                   "C:\\bin\\{}.EXE", "/usr/bin/{}.ps1", "({})", "{} pkg"])
def test_argument_word_inventory(form):
    for name in [*_EVALUATORS, "npx", "uvx", "bunx", "pnpx", "pipx", "uv", "npm", "pnpm", "yarn"]:
        token = form.format(name.upper())
        refused(stdio("my-server", args=["--port", "1", token]), "mcp_command_invalid",
                "mcp_servers.docs.server.args.2")


# Recognized pin recipes and unknown bare programs retain their explicit scope;
# hyphens are word characters. Base-system names have their own refusal policy.
@pytest.mark.parametrize("command,args", [("npx", ["pkg@1.2.3"]), ("uvx", ["pkg==1.2.3"]),
    ("my-server", ["--port", "1"]), ("my-server", ["--node-id", "ordinary"]), ("my-filter", [".name"])])
def test_word_scan_positive_controls(command, args):
    assert parse_one(stdio(command, args=args)).server.args == tuple(args)


# Refusal identifies only the recognized word, without echoing opaque data
# around it. This does not certify that arbitrary argument data is secret-free.
def test_embedded_word_diagnostic_does_not_echo_other_data():
    exc = refused(stdio("my-server", args=["private-example npx other-value"]),
                  "mcp_command_invalid", "mcp_servers.docs.server.args.0")
    assert "'npx'" in str(exc)
    assert "private-example" not in str(exc) and "other-value" not in str(exc)
