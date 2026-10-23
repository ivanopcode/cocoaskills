from __future__ import annotations

import hashlib
import ipaddress
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from csk import protocol_json
from csk.audit import redaction
from csk.mcp_base_commands import EXEC_CAPABLE_BASE_COMMANDS, is_base_command, is_base_recipe
from csk.mcp_command_aliases import EVALUATOR_ALIASES
from csk.mcp_configs import known_agents
from csk.skillspec import McpServerRequirement


# Skillfile `mcp_servers` declarations (schema 3, slice 1): a closed grammar
# for inline stdio/http servers, per-agent native entry rendering, digests and
# a pure requirement check. Everything here is pure: no filesystem, network,
# environment, subprocess or clock access. Project installs compose these
# declarations with the native codecs and protected ownership planner.

SLICE1_AGENTS: tuple[str, ...] = ("claude_code", "codex_cli", "cursor")

_DECLARATION_DOMAIN = b"csk-mcp-declaration-v1\0"
_ENTRY_DOMAIN = b"csk-mcp-entry-v1\0"

_MAX_DECLARATIONS = 64
_MAX_ARGS = 64
_MAX_ARG_LENGTH = 4096
_MAX_ENV_NAMES = 32
_MAX_HEADERS = 16
_MAX_HEADER_NAME = 128
_MAX_URL = 2048

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_WINDOWS_RESERVED_RE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])", re.IGNORECASE)
_COMMAND_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_HEADER_TOKEN_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")
_PORT_RE = re.compile(r"[0-9]{1,5}")

_DECLARATION_FIELDS = frozenset({"server", "reference", "agents", "scope", "auth", "git_policy"})
_STDIO_FIELDS = frozenset({"transport", "command", "args", "env_names"})
_HTTP_FIELDS = frozenset({"transport", "url", "headers", "env_names"})
_AUTH_FIELDS = frozenset({"kind", "help_url"})
_HEADER_REF_FIELDS = frozenset({"env", "prefix"})

_COMMAND_KINDS = {
    **dict.fromkeys(
        ("sh", "ash", "hush", "rbash", "rksh", "ksh93", "bash", "zsh", "fish", "dash", "ksh", "mksh", "csh", "tcsh", "cmd", "powershell", "pwsh"),
        "shell",
    ),
    **dict.fromkeys((
        "env", "xargs", "nohup", "sudo", "doas", "su", "busybox", "script",
        "nice", "arch", "caffeinate", "timeout", "gtimeout", "stdbuf", "gstdbuf",
        "chrt", "taskset", "ionice", "time", "command", "exec", "launchctl", "start",
        "setsid", "flock", "chronic", "unbuffer",
        "chroot", "chpst", "setpriv", "runuser", "nsenter", "unshare", "prlimit",
        "run-parts", "start-stop-daemon", "at", "batch", "crontab", "systemd-run",
        "systemctl", "service", "schtasks",
    ), "indirection"),
    **dict.fromkeys((
        "osascript", "open", "automator", "shortcuts", "xdg-open", "gio", "gtk-launch",
        "xterm", "gnome-terminal", "konsole",
    ), "launcher"),
    # Policy inventory (default OS tools plus commonly bundled language runtimes):
    # text/languages: can evaluate programs or launch processes, even without -e;
    # editors/debuggers/build/VCS/remote tools: expose command execution;
    # archivers/stream tools/pagers: expose command hooks or shell escapes;
    # macros/SQL/remote clients: expose system(), .shell or proxy programs;
    # native compilers/loaders and OS package tools: load code or execute hooks;
    # Windows launchers: execute script, DLL or registered handler code.
    # This evaluator table is separate from the base-system command inventory.
    # Unknown bare programs remain opaque; this inventory is not a sandbox.
    **dict.fromkeys((
        "awk", "gawk", "mawk", "nawk", "sed", "gsed", "node", "nodejs", "deno", "bun",
        "python", "pythonw", "pypy", "ruby", "perl", "php", "lua", "luajit", "tclsh", "wish",
        "rscript", "r", "wscript", "cscript", "mshta", "rundll32", "regsvr32",
        "find", "gfind", "make", "gmake", "ed", "ex", "vi", "vim", "nvim", "emacs",
        "gdb", "lldb", "git", "ssh", "toybox", "expect",
        "tar", "gtar", "bsdtar", "rsync", "zip", "cpio", "sort", "split", "watch",
        "less", "more", "man", "info", "sdiff", "forfiles", "wmic",
        "m4", "sqlite3", "dc", "scp", "sftp", "openssl", "rpm", "apt", "apt-get",
        "dpkg", "dnf", "yum", "apk", "gcc", "g++", "cc", "c++", "clang", "clang++", "ld", "ar",
    ), "evaluator"),
    **dict.fromkeys(("bunx", "pnpx", "pipx"), "unrecognized"),
    **dict.fromkeys(("npm", "pnpm", "yarn"), "manager"),
    "npx": "npx", "uvx": "uvx", "uv": "uv",
}
# Preserve specific shell/wrapper/runner classifications while sharing aliases.
for _alias in EVALUATOR_ALIASES:
    _COMMAND_KINDS.setdefault(_alias, "evaluator")
# These views and all launcher decisions share one policy inventory.
_SHELL_COMMANDS = frozenset(k for k, v in _COMMAND_KINDS.items() if v == "shell")
_INDIRECTION_COMMANDS = frozenset(k for k, v in _COMMAND_KINDS.items() if v == "indirection")
_EVALUATOR_LAUNCHERS = frozenset(k for k, v in _COMMAND_KINDS.items() if v == "launcher")
# Default installations also expose versioned interpreter executable names.
_VERSIONED_EVALUATOR_RE = re.compile(r"(pythonw?|pypy|lua(?:jit)?|tclsh|wish|perl|ruby|php)[0-9.]*", re.ASCII)
_ARGUMENT_WORD_RE = re.compile(r"[A-Za-z0-9_.+-]+", re.ASCII)
_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1", ".com")
_UNRECOGNIZED_RECIPES = frozenset(k for k, v in _COMMAND_KINDS.items() if v == "unrecognized")
_PACKAGE_MANAGERS = frozenset(k for k, v in _COMMAND_KINDS.items() if v == "manager")
_PACKAGE_MANAGER_RUNNERS = frozenset({"exec", "dlx"})
_PACKAGE_MANAGER_ALIASES = frozenset({"x", "create"})
_NPM_RUNNER_ALIASES = frozenset({"x", "init", "create"})
_BUN_RUNNER_ALIASES = frozenset({"x", "create"})

_NPX_FLAGS = frozenset({"-y", "--yes", "-q", "--quiet"})
_UVX_FLAGS = frozenset({"-q", "--quiet", "--offline", "--no-cache", "-n"})

# Exact SemVer 2.0.0 (https://semver.org, the grammar behind its official regex):
# numeric identifiers have no leading zeros, prerelease and build are dotted identifiers.
_SEMVER_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_SEMVER = (
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    rf"(?:-{_SEMVER_IDENTIFIER}(?:\.{_SEMVER_IDENTIFIER})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_NPX_PACKAGE_RE = re.compile(
    r"(?:@[A-Za-z0-9][A-Za-z0-9._~-]*/)?[A-Za-z0-9][A-Za-z0-9._~-]*@" + _SEMVER
)
# One exact PEP 440 version, in any spelling the specification normalizes: epoch,
# release, pre, post, dev and `+local`. No wildcard and no operator.
_PEP440_VERSION = (
    r"v?(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*"
    r"(?:[-_.]?(?:alpha|a|beta|b|preview|pre|c|rc)[-_.]?[0-9]*)?"
    r"(?:-[0-9]+|[-_.]?(?:post|rev|r)[-_.]?[0-9]*)?"
    r"(?:[-_.]?dev[-_.]?[0-9]*)?"
    r"(?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?"
)
_UVX_PIN_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(?:\[[A-Za-z0-9._,-]+\])?==" + _PEP440_VERSION,
    re.IGNORECASE | re.ASCII,
)

_RESERVED_ENV_NAMES = frozenset(
    {
        "PATH", "HOME", "SHELL", "PWD", "IFS", "ENV", "BASH_ENV", "ZDOTDIR", "CDPATH", "PATHEXT",
        "COMSPEC", "SYSTEMROOT", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TMPDIR", "TEMP", "TMP",
        "CODEX_HOME", "CLAUDE_CONFIG_DIR",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "NO_PROXY", "RES_OPTIONS",
        "HOSTALIASES", "LOCALDOMAIN", "WINDIR", "__PYVENV_LAUNCHER__",
    }
)  # fmt: skip
_RESERVED_ENV_PREFIXES = (
    "LD_", "DYLD_", "NODE_", "NPM_CONFIG_", "PYTHON", "CSK_", "XDG_", "GIT_", "UV_", "PIP_",
)  # fmt: skip

_FORBIDDEN_HEADER_NAMES = frozenset(
    {
        "host", "content-length", "transfer-encoding", "connection", "upgrade", "te", "trailer",
        "keep-alive", "cookie",
    }
)  # fmt: skip

_Transport = Literal["stdio", "http"]
_Prefix = Literal["none", "bearer"]
_AuthKind = Literal["none", "env", "oauth"]


class McpDeclarationError(ValueError):
    """A refusal with a stable `code` and the dotted `field` it applies to.

    Only recognized launcher words may be named. Other declared argument and
    header values can carry material the operator did not mean to publish.
    """

    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(f"{code}: {message} ({field})")
        self.code = code
        self.field = field
        self.public_field = False


@dataclass(frozen=True)
class HeaderRef:
    name: str
    env: str
    prefix: _Prefix = "none"


@dataclass(frozen=True)
class ServerSpec:
    transport: _Transport
    command: str | None = None
    args: tuple[str, ...] = ()
    env_names: tuple[str, ...] = ()
    url: str | None = None
    headers: tuple[HeaderRef, ...] = ()


@dataclass(frozen=True)
class AuthSpec:
    kind: _AuthKind
    help_url: str | None = None


@dataclass(frozen=True)
class McpDeclaration:
    name: str
    server: ServerSpec
    auth: AuthSpec
    agents: tuple[str, ...]
    scope: Literal["project"] = "project"
    git_policy: Literal["local"] = "local"
    # Reporting metadata is excluded from the writable model and its consent digest.
    unsupported_agents: tuple[str, ...] = ()


@dataclass(frozen=True)
class Authority:
    executes_code: bool
    routes_secret: bool

    @property
    def consent_required(self) -> bool:
        return self.executes_code or self.routes_secret


@dataclass(frozen=True)
class EffectiveServer:
    name: str
    agent: str
    origin: Literal["planned", "external"]
    transport: Literal["stdio", "http", "sse", "unknown"]
    state: Literal["active", "disabled", "rejected"]


@dataclass(frozen=True)
class RequirementReport:
    found: dict[str, dict[str, list[str]]]
    errors: list[str]
    warnings: list[str] = field(default_factory=list)


# --- parsing -----------------------------------------------------------------


@dataclass(frozen=True)
class _Draft:
    """One declaration after step 1 (syntax, types, closed fields)."""

    name: str
    path: str
    is_reference: bool
    transport: str | None
    server: ServerSpec | None
    auth: AuthSpec
    agents: tuple[str, ...] | None
    scope: str | None
    git_policy: str
    index: int


def parse_mcp_servers(
    raw: object, *, invocation_scope: str, target_agents: Sequence[str]
) -> dict[str, McpDeclaration]:
    """Parse the closed server/install grammar and normalize URL shorthand."""
    try:
        return _parse_mcp_servers(
            raw, invocation_scope=invocation_scope, target_agents=target_agents
        )
    except McpDeclarationError as exc:
        if exc.public_field:
            raise
        field = exc.field
        parts = field.split(".", 2)
        if len(parts) == 3:
            suffix = "." + parts[2]
            for old, new in (
                (".server.headers", ".install.headers"),
                (".agents", ".server.environments"),
                (".auth", ".install.auth"),
                (".scope", ".install.scope"),
                (".git_policy", ".install.git_policy"),
            ):
                if suffix == old or suffix.startswith(old + "."):
                    suffix = new + suffix[len(old) :]
                    break
            field = ".".join(parts[:2]) + suffix
        message = str(exc).split(": ", 1)[1].rsplit(" (", 1)[0]
        raise McpDeclarationError(exc.code, field, message) from None


def _parse_mcp_servers(
    raw: object, *, invocation_scope: str, target_agents: Sequence[str]
) -> dict[str, McpDeclaration]:
    """Parse and classify a Skillfile `mcp_servers` object.

    Validation follows the decided order across the whole map: (1) syntax,
    names, types and closed fields, (2) scope, env, url and recipe rules,
    (3) agent selection, (4) capability. Ownership, consent, the requirement
    check and publication belong to later stages and other modules.
    """
    if invocation_scope not in ("project", "user"):
        raise McpDeclarationError(
            "mcp_declaration_invalid", "invocation_scope", "invocation scope must be project or user"
        )
    drafts = _step1_shape(raw)
    for draft in drafts:
        if draft.scope == "user":
            raise _fail(
                "mcp_capability_unsupported",
                f"{draft.path}.scope",
                "only project scope is supported",
            )
    _step2_rules(drafts, invocation_scope)
    selected = {draft.name: _step3_agents(draft, target_agents) for draft in drafts}
    for draft in drafts:
        _step4_capability(draft, invocation_scope)
    declarations: dict[str, McpDeclaration] = {}
    for draft in drafts:
        assert draft.server is not None
        declarations[draft.name] = McpDeclaration(
            name=draft.name,
            server=_normalized(draft.server),
            auth=draft.auth,
            agents=tuple(a for a in selected[draft.name] if a in SLICE1_AGENTS),
            unsupported_agents=tuple(a for a in selected[draft.name] if a not in SLICE1_AGENTS),
        )
    return declarations


def _normalized(server: ServerSpec) -> ServerSpec:
    """Order-insensitive fields are sorted so equal declarations render and hash equally."""
    return ServerSpec(
        transport=server.transport,
        command=server.command,
        args=server.args,
        env_names=tuple(sorted(server.env_names, key=lambda name: (name.upper(), name))),
        url=server.url,
        headers=tuple(sorted(server.headers, key=lambda header: (header.name.lower(), header.name))),
    )


def _fail(code: str, path: str, message: str) -> McpDeclarationError:
    return McpDeclarationError(code, path, message)


def _invalid(path: str, message: str) -> McpDeclarationError:
    return McpDeclarationError("mcp_declaration_invalid", path, message)


def _path_name(value: object, index: int) -> str:
    """Only bounded portable names may appear in a diagnostic path."""
    if (
        isinstance(value, str)
        and _NAME_RE.fullmatch(value)
        and not _WINDOWS_RESERVED_RE.fullmatch(value.split(".", 1)[0])
    ):
        return value
    return f"<invalid name #{index}>"


def _step1_shape(raw: object) -> list[_Draft]:
    if not isinstance(raw, dict):
        raise _invalid("mcp_servers", "must be an object mapping server names to declarations")
    if len(raw) > _MAX_DECLARATIONS:
        raise _invalid("mcp_servers", f"at most {_MAX_DECLARATIONS} declarations are allowed")
    drafts: list[_Draft] = []
    indices = {name: index for index, name in enumerate(raw)}
    for name in raw:
        if not isinstance(name, str):
            raise _invalid(f"mcp_servers.{_path_name(name, indices[name])}", "server names must be strings")
    # Map keys are step-1 syntax: every name is checked before any declaration body.
    names = sorted(raw)
    seen: set[str] = set()
    for name in names:
        _check_name(name, indices[name])
        if name.lower() in seen:
            raise _invalid(f"mcp_servers.{name}", "server names must be unique ignoring case")
        seen.add(name.lower())
    for name in names:
        drafts.append(_shape_declaration(name, raw[name], indices[name]))
    return drafts


def _public_invalid(path: str, message: str) -> McpDeclarationError:
    error = _invalid(path, message)
    error.public_field = True
    return error


def _shape_declaration(name: str, body: object, index: int) -> _Draft:
    path = f"mcp_servers.{name}"
    if isinstance(body, str):
        body = {"server": {"transport": "http", "url": body}}
    if not isinstance(body, dict):
        raise _public_invalid(path, "declaration must be an object or HTTPS URL")
    for key in body:
        if key not in {"server", "install"}:
            raise _public_invalid(
                f"{path}.{_path_name(key, index)}", "unknown declaration field"
            )
    if "server" not in body:
        raise _public_invalid(path, "server is required")
    server = body["server"]
    if not isinstance(server, dict):
        raise _public_invalid(f"{path}.server", "server must be an object")
    for key in server:
        if key not in {
            "transport",
            "url",
            "command",
            "args",
            "env_names",
            "environments",
        }:
            raise _public_invalid(
                f"{path}.server.{_path_name(key, index)}", "unknown server field"
            )
    extensions = body.get("install", {})
    if not isinstance(extensions, dict):
        raise _public_invalid(f"{path}.install", "install must be an object")
    for key in extensions:
        if key not in {"headers", "auth", "scope", "git_policy"}:
            raise _public_invalid(
                f"{path}.install.{_path_name(key, index)}", "unknown install field"
            )
    normalized_server = {
        key: value for key, value in server.items() if key != "environments"
    }
    if "headers" in extensions:
        normalized_server["headers"] = extensions["headers"]
    normalized: dict[str, object] = {"server": normalized_server}
    normalized.update(
        {key: value for key, value in extensions.items() if key != "headers"}
    )
    if "environments" in server:
        normalized["agents"] = server["environments"]
    if "auth" not in normalized:
        normalized["auth"] = {
            "kind": "env"
            if server.get("env_names") or extensions.get("headers")
            else "none"
        }
    return _shape_normalized(name, normalized, index)


def _shape_normalized(name: str, body: object, index: int) -> _Draft:
    path = f"mcp_servers.{name}"
    if isinstance(body, str):
        body = {"server": {"transport": "http", "url": body}, "auth": {"kind": "none"}}
    if not isinstance(body, dict):
        raise _invalid(path, "declaration must be an object or HTTPS URL")
    for key in body:
        if key not in _DECLARATION_FIELDS:
            raise _invalid(f"{path}.{_path_name(key, index)}", "unknown field")
    has_server = "server" in body
    has_reference = "reference" in body
    if has_server == has_reference:
        raise _invalid(path, "exactly one of server and reference is required")
    if "auth" not in body:
        server_raw = body.get("server")
        if not (
            isinstance(server_raw, dict)
            and server_raw.get("transport") == "http"
            and "headers" not in server_raw
        ):
            raise _invalid(f"{path}.auth", "auth is required")
    auth = _shape_auth(body.get("auth", {"kind": "none"}), f"{path}.auth", index)

    agents: tuple[str, ...] | None = None
    if "agents" in body:
        agents_raw = body["agents"]
        if not isinstance(agents_raw, list) or not all(isinstance(a, str) for a in agents_raw):
            raise _invalid(f"{path}.agents", "must be a list of agent names")
        agents = tuple(agents_raw)
    scope: str | None = None
    if "scope" in body:
        scope_raw = body["scope"]
        if scope_raw not in ("project", "user") or not isinstance(scope_raw, str):
            raise _invalid(f"{path}.scope", "must be project or user")
        scope = scope_raw
    git_policy = "local"
    if "git_policy" in body:
        policy_raw = body["git_policy"]
        if policy_raw not in ("local", "shared") or not isinstance(policy_raw, str):
            raise _invalid(f"{path}.git_policy", "must be local or shared")
        git_policy = policy_raw

    if has_reference:
        # Recognized so that it can be refused precisely at the capability step;
        # the payload must be an object but its contents are not interpreted.
        if not isinstance(body["reference"], dict):
            raise _invalid(f"{path}.reference", "reference must be an object")
        return _Draft(name, path, True, None, None, auth, agents, scope, git_policy, index)
    transport, server = _shape_server(body["server"], f"{path}.server", index)
    return _Draft(name, path, False, transport, server, auth, agents, scope, git_policy, index)


def _shape_auth(body: object, path: str, index: int) -> AuthSpec:
    if not isinstance(body, dict):
        raise _invalid(path, "auth must be an object")
    for key in body:
        if key not in _AUTH_FIELDS:
            raise _invalid(f"{path}.{_path_name(key, index)}", "unknown field")
    kind = body.get("kind")
    if kind not in ("none", "env", "oauth") or not isinstance(kind, str):
        raise _invalid(f"{path}.kind", "must be none, env or oauth")
    help_url = body.get("help_url")
    if "help_url" in body and not isinstance(help_url, str):
        raise _invalid(f"{path}.help_url", "must be a string")
    return AuthSpec(kind=kind, help_url=help_url)  # type: ignore[arg-type]


def _shape_server(body: object, path: str, index: int) -> tuple[str, ServerSpec]:
    if not isinstance(body, dict):
        raise _invalid(path, "server must be an object")
    transport = body.get("transport")
    if transport not in ("stdio", "http", "sse") or not isinstance(transport, str):
        raise _invalid(f"{path}.transport", "must be stdio, http or sse")
    # sse has the shape of http; it is refused at the capability step, after its fields
    # have passed the same closed-field and url rules.
    allowed = _STDIO_FIELDS if transport == "stdio" else _HTTP_FIELDS
    for key in body:
        if key not in allowed:
            raise _invalid(f"{path}.{_path_name(key, index)}", "unknown field")
    if transport == "stdio":
        command = body.get("command")
        if not isinstance(command, str):
            raise _invalid(f"{path}.command", "command is required and must be a string")
        args = _string_list(body.get("args", []), f"{path}.args")
        env_names = _string_list(body.get("env_names", []), f"{path}.env_names")
        return transport, ServerSpec(
            transport="stdio", command=command, args=tuple(args), env_names=tuple(env_names)
        )
    url = body.get("url")
    if not isinstance(url, str):
        raise _invalid(f"{path}.url", "url is required and must be a string")
    headers_raw = body.get("headers", {})
    if not isinstance(headers_raw, dict):
        raise _invalid(f"{path}.headers", "must be an object")
    headers: list[HeaderRef] = []
    for header_name, value in headers_raw.items():
        header_path = f"{path}.headers.{_path_name(header_name, index)}"
        if not isinstance(header_name, str):
            raise _invalid(header_path, "header names must be strings")
        if not isinstance(value, dict):
            raise _invalid(header_path, "header value must be an env reference object")
        for key in value:
            if key not in _HEADER_REF_FIELDS:
                raise _invalid(f"{header_path}.{_path_name(key, index)}", "unknown field")
        env = value.get("env")
        if not isinstance(env, str):
            raise _invalid(f"{header_path}.env", "env is required and must be a string")
        prefix = value.get("prefix", "none")
        if prefix not in ("none", "bearer") or not isinstance(prefix, str):
            raise _invalid(f"{header_path}.prefix", "must be none or bearer")
        headers.append(HeaderRef(name=header_name, env=env, prefix=prefix))  # type: ignore[arg-type]
    env_names = _string_list(body.get("env_names", []), f"{path}.env_names")
    return transport, ServerSpec(
        transport="http", url=url, headers=tuple(headers), env_names=tuple(env_names)
    )


def _string_list(value: object, path: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _invalid(path, "must be a list of strings")
    return list(value)


def _step2_rules(drafts: list[_Draft], invocation_scope: str) -> None:
    for draft in drafts:
        if draft.scope is not None and draft.scope != invocation_scope:
            raise _fail("mcp_scope_mismatch", f"{draft.path}.scope", "scope differs from the invocation scope")
        server = draft.server
        if server is not None:
            server_path = f"{draft.path}.server"
            if server.transport == "stdio":
                _check_stdio(server, server_path)
            else:
                _check_http(server, server_path, draft.index)
        _check_auth(draft, server)


def _check_name(name: str, index: int) -> None:
    path = f"mcp_servers.{_path_name(name, index)}"
    if not _NAME_RE.fullmatch(name):
        raise _invalid(path, "server name must match [A-Za-z0-9][A-Za-z0-9_-]{0,127}")
    if _WINDOWS_RESERVED_RE.fullmatch(name.split(".", 1)[0]):
        raise _invalid(path, "server name is a reserved Windows device name")


def _has_forbidden_char(value: str) -> bool:
    return any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value)


def _normalize_command(token: str) -> str:
    base = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base.endswith(_EXECUTABLE_SUFFIXES):
        base = base.rsplit(".", 1)[0]
    return base


def _command_kind(base: str) -> str | None:
    family = _VERSIONED_EVALUATOR_RE.fullmatch(base)
    return _COMMAND_KINDS.get(family[1] if family else base)


def is_recognized_runner(command: str) -> bool:
    """Whether stdio consent can describe a recognized runner recipe."""
    return _command_kind(_normalize_command(command)) in {"npx", "uvx", "uv", "manager", "unrecognized"}


def _check_argument_launchers(args: tuple[str, ...], path: str) -> None:
    for index, arg in enumerate(args):
        for word in _ARGUMENT_WORD_RE.findall(arg):
            normalized = _normalize_command(word)
            if _command_kind(normalized) is not None or normalized in EXEC_CAPABLE_BASE_COMMANDS:
                raise _fail(
                    "mcp_command_invalid", f"{path}.args.{index}",
                    f"runner/evaluator token {word!r} is refused in args; use a direct command",
                )


def _check_stdio(server: ServerSpec, path: str) -> None:
    command = server.command
    assert command is not None
    if not _COMMAND_RE.fullmatch(command):
        raise _fail("mcp_command_invalid", f"{path}.command", "command must be a bare executable name")
    base = _normalize_command(command)
    recipe = is_base_recipe(command, server.args)
    if is_base_command(base) and not recipe:
        raise _fail("mcp_command_invalid", f"{path}.command", "base-system programs cannot be MCP servers; only verified exact Apple MCP recipes are allowed")
    if not recipe and (base in _SHELL_COMMANDS or base in _INDIRECTION_COMMANDS):
        raise _fail("mcp_command_invalid", f"{path}.command", "shells and indirection commands are refused")
    if not recipe and base in _EVALUATOR_LAUNCHERS:
        raise _fail("mcp_command_invalid", f"{path}.command", "evaluator launchers are refused")
    if not recipe and _command_kind(base) == "evaluator":
        raise _fail("mcp_command_invalid", f"{path}.command", "evaluator commands are refused")
    args = server.args
    if len(args) > _MAX_ARGS:
        raise _invalid(f"{path}.args", f"at most {_MAX_ARGS} arguments are allowed")
    for index, arg in enumerate(args):
        arg_path = f"{path}.args.{index}"
        if len(arg) > _MAX_ARG_LENGTH:
            raise _invalid(arg_path, f"arguments are limited to {_MAX_ARG_LENGTH} characters")
        if _has_forbidden_char(arg):
            raise _invalid(arg_path, "control, format and surrogate characters are refused")
        if "${" in arg:
            raise _invalid(arg_path, "variable expansion syntax is refused")
    for index, arg in enumerate(args):
        _check_secret_material(arg, f"{path}.args.{index}")
    _check_runtime_pin(base, args, path)
    _check_argument_launchers(args, path)
    _check_env_names(server.env_names, f"{path}.env_names")


def _check_runtime_pin(base: str, args: tuple[str, ...], path: str) -> None:
    if base == "npx":
        _check_npx(args, path)
    elif base == "uvx":
        _check_uvx(args, path)
    elif (
        base in _UNRECOGNIZED_RECIPES
        or (base in _PACKAGE_MANAGERS and any(arg in _PACKAGE_MANAGER_RUNNERS for arg in args))
        or (base in ("pnpm", "yarn") and any(arg in _PACKAGE_MANAGER_ALIASES for arg in args))
        or (base == "npm" and any(arg in _NPM_RUNNER_ALIASES for arg in args))
        or (base == "bun" and any(arg in _BUN_RUNNER_ALIASES for arg in args))
        or (base == "uv" and "run" in args)
        or (base == "deno" and "run" in args and any(arg.startswith("npm:") for arg in args))
    ):
        raise _fail("mcp_runtime_pin_required", f"{path}.command", "runner recipe is not recognized")


def _check_npx(args: tuple[str, ...], path: str) -> None:
    for index, arg in enumerate(args):
        if arg in _NPX_FLAGS:
            continue
        if arg.startswith("-"):
            raise _fail("mcp_runtime_pin_required", f"{path}.args.{index}", "flag is not allowed before the package")
        if not _NPX_PACKAGE_RE.fullmatch(arg):
            raise _fail(
                "mcp_runtime_pin_required", f"{path}.args.{index}", "package must be pinned to an exact version"
            )
        return
    raise _fail("mcp_runtime_pin_required", f"{path}.args", "a pinned package argument is required")


def _check_uvx(args: tuple[str, ...], path: str) -> None:
    # Only boolean flags may precede the package; a flag that takes a value would
    # make the "first non-flag argument" depend on a table csk does not own.
    start = 0
    while start < len(args) and args[start] in _UVX_FLAGS:
        start += 1
    if start == len(args):
        raise _fail("mcp_runtime_pin_required", f"{path}.args", "package must be pinned as name==version")
    first = args[start]
    if first == "--from":
        rest = args[start + 1 :]
        if len(rest) < 2 or not _UVX_PIN_RE.fullmatch(rest[0]) or rest[1].startswith("-"):
            raise _fail(
                "mcp_runtime_pin_required", f"{path}.args", "--from requires an exact name==version and a command"
            )
        return
    if first.startswith("-"):
        raise _fail("mcp_runtime_pin_required", f"{path}.args.{start}", "flag is not allowed before the package")
    if not _UVX_PIN_RE.fullmatch(first):
        raise _fail("mcp_runtime_pin_required", f"{path}.args.{start}", "package must be pinned as name==version")


def _check_env_names(names: Sequence[str], path: str) -> None:
    if len(names) > _MAX_ENV_NAMES:
        raise _invalid(path, f"at most {_MAX_ENV_NAMES} names are allowed")
    seen: set[str] = set()
    for index, name in enumerate(names):
        _check_env_name(name, f"{path}.{index}")
        folded = name.upper()
        if folded in seen:
            raise _invalid(f"{path}.{index}", "environment names must be unique ignoring case")
        seen.add(folded)


def _check_env_name(name: str, path: str) -> None:
    if not _ENV_NAME_RE.fullmatch(name):
        raise _invalid(path, "environment name must match [A-Za-z_][A-Za-z0-9_]{0,127}")
    upper = name.upper()
    if upper in _RESERVED_ENV_NAMES or upper.startswith(_RESERVED_ENV_PREFIXES):
        raise _fail("mcp_env_name_reserved", path, "environment name is reserved")


def _url_problem(url: str, *, allow_query_fragment: bool) -> str | None:
    if len(url) > _MAX_URL:
        return f"url is limited to {_MAX_URL} characters"
    if _has_forbidden_char(url) or any(char.isspace() for char in url):
        return "url must not contain whitespace, control or format characters"
    if any(char in url for char in "${}\\"):
        return "url must not contain $, braces or backslashes"
    if url[:8].lower() != "https://":
        return "url scheme must be https"
    rest = url[8:]
    end = next((i for i, char in enumerate(rest) if char in "/?#"), len(rest))
    authority = rest[:end]
    if "@" in authority:
        return "url must not contain userinfo"
    if not allow_query_fragment and ("?" in url or "#" in url):
        return "url must not contain a query or fragment"
    if authority.startswith("["):
        host, closed, port_part = authority[1:].partition("]")
        if not closed:
            return "url host is invalid"
        port = port_part[1:] if port_part.startswith(":") else ""
        if port_part and not port_part.startswith(":"):
            return "url host is invalid"
        if not host.isascii():
            return "url host must be ASCII"
        if "%" in host:
            return "url host must not carry a zone identifier"
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return "url host is invalid"
        if port_part and not _valid_port(port):
            return "url port is invalid"
        return None
    host, colon, port = authority.partition(":")
    if not host.isascii():
        return "url host must be ASCII"
    if not host or not _HOST_RE.fullmatch(host) or ".." in host:
        return "url host must be a non-empty ASCII host"
    if colon and not _valid_port(port):
        return "url port is invalid"
    return None


def _valid_port(port: str) -> bool:
    return bool(_PORT_RE.fullmatch(port)) and 1 <= int(port) <= 65535


def _check_secret_material(value: str, path: str) -> None:
    # Reuse the audit signatures without treating a long repeated padding
    # string as high entropy. This is static detection, not secret resolution.
    signatures = (
        redaction.ENV_ASSIGNMENT_RE,
        redaction.AUTH_HEADER_RE,
        redaction.PEM_RE,
    )
    opaque = any(
        len(set(match.group())) >= 10
        for match in redaction.HIGH_ENTROPY_RE.finditer(value)
    )
    token = re.search(
        r"(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|sk-[A-Za-z0-9_-]{16,})",
        value,
    )
    if opaque or token or any(pattern.search(value) for pattern in signatures):
        raise _invalid(path, "secret material is refused; use an environment reference")


def _check_http(server: ServerSpec, path: str, index: int) -> None:
    assert server.url is not None
    problem = _url_problem(server.url, allow_query_fragment=False)
    if problem is not None:
        raise _fail("mcp_url_invalid", f"{path}.url", problem)
    _check_secret_material(server.url, f"{path}.url")
    _check_env_names(server.env_names, f"{path}.env_names")
    headers = server.headers
    if len(headers) > _MAX_HEADERS:
        raise _invalid(f"{path}.headers", f"at most {_MAX_HEADERS} headers are allowed")
    seen: set[str] = set()
    for header in headers:
        header_path = f"{path}.headers.{_path_name(header.name, index)}"
        if len(header.name) > _MAX_HEADER_NAME or not _HEADER_TOKEN_RE.fullmatch(header.name):
            raise _invalid(header_path, "header name must be an RFC 9110 token of at most 128 characters")
        folded = header.name.lower()
        if folded in _FORBIDDEN_HEADER_NAMES or folded.startswith("proxy-"):
            raise _invalid(header_path, "header name is refused")
        if folded in seen:
            raise _invalid(header_path, "header names must be unique ignoring case")
        seen.add(folded)
        if header.prefix == "bearer" and folded != "authorization":
            raise _invalid(f"{header_path}.prefix", "bearer is allowed only on Authorization")
        _check_env_name(header.env, f"{header_path}.env")


def _check_auth(draft: _Draft, server: ServerSpec | None) -> None:
    path = f"{draft.path}.auth"
    auth = draft.auth
    if auth.help_url is not None:
        problem = _url_problem(auth.help_url, allow_query_fragment=True)
        if problem is not None:
            raise _invalid(f"{path}.help_url", problem)
    if server is None:
        # A reference payload is not interpreted, so its environment references are unknown.
        return
    has_reference = bool(server.env_names) or bool(server.headers)
    if auth.kind == "env" and not has_reference:
        raise _invalid(f"{path}.kind", "kind env needs at least one environment reference")
    if auth.kind in ("none", "oauth") and has_reference:
        raise _invalid(f"{path}.kind", f"kind {auth.kind} must not have environment references")
    if auth.kind == "oauth" and server.transport != "http":
        raise _invalid(f"{path}.kind", "kind oauth is http-only")


def _step3_agents(draft: _Draft, target_agents: Sequence[str]) -> tuple[str, ...]:
    path = f"{draft.path}.agents"
    targets = set(target_agents)
    if draft.agents is None:
        selected = tuple(sorted(targets & set(known_agents())))
        if not selected and not targets:
            raise _fail("mcp_agent_selection_invalid", path, "no target agent is supported by this slice")
        return selected
    if not draft.agents:
        raise _fail("mcp_agent_selection_invalid", path, "agent list must not be empty")
    if len(set(draft.agents)) != len(draft.agents):
        raise _fail("mcp_agent_selection_invalid", path, "agent list must be unique")
    known = known_agents()
    for index, agent in enumerate(draft.agents):
        if agent not in known:
            raise _fail(
                "mcp_agent_selection_invalid", f"{path}.{index}", "agent is unknown"
            )
    return tuple(sorted(draft.agents))


def _step4_capability(draft: _Draft, invocation_scope: str) -> None:
    if draft.is_reference:
        raise _fail("mcp_capability_unsupported", f"{draft.path}.reference", "references are not supported yet")
    if draft.transport == "sse":
        raise _fail("mcp_capability_unsupported", f"{draft.path}.server.transport", "sse is not supported yet")
    if draft.git_policy == "shared":
        raise _fail("mcp_capability_unsupported", f"{draft.path}.git_policy", "shared git policy is not supported yet")
    if invocation_scope != "project":
        raise _fail("mcp_capability_unsupported", f"{draft.path}.scope", "only project scope is supported")


# --- classification and digests ---------------------------------------------


def authority(decl: McpDeclaration) -> Authority:
    """Classify what a declaration lets csk publish (decision D1)."""
    server = decl.server
    return Authority(
        executes_code=server.transport == "stdio",
        routes_secret=bool(server.env_names) or bool(server.headers),
    )


def declaration_digest(decl: McpDeclaration) -> str:
    server = decl.server
    normalized = {
        "name": decl.name,
        "server": {
            "transport": server.transport,
            "command": server.command,
            "args": list(server.args),
            "env_names": list(server.env_names),
            "url": server.url,
            "headers": [
                {"name": header.name, "env": header.env, "prefix": header.prefix} for header in server.headers
            ],
        },
        "auth": {"kind": decl.auth.kind, "help_url": decl.auth.help_url},
        "agents": list(decl.agents),
        "scope": decl.scope,
        "git_policy": decl.git_policy,
    }
    return hashlib.sha256(_DECLARATION_DOMAIN + protocol_json.canonical_bytes(normalized)).hexdigest()


def entry_digest(value: object) -> str | None:
    """SHA-256 of a native entry's CCJ-1 bytes, None when not representable."""
    try:
        canonical = protocol_json.canonical_bytes(value)
    except (protocol_json.ProtocolJSONError, RecursionError):
        return None
    return hashlib.sha256(_ENTRY_DOMAIN + canonical).hexdigest()


# --- rendering ----------------------------------------------------------------


def render_native_entry(agent: str, decl: McpDeclaration) -> dict[str, object]:
    """Render the entry that goes under the agent's native server key."""
    if agent not in SLICE1_AGENTS:
        raise McpDeclarationError("mcp_capability_unsupported", "agent", "agent is not supported by this slice")
    server = decl.server
    if agent == "codex_cli":
        return _render_codex(server)
    reference = "${%s}" if agent == "claude_code" else "${env:%s}"
    entry: dict[str, object]
    if server.transport == "stdio":
        entry = {"type": "stdio", "command": server.command, "args": list(server.args)}
        if server.env_names:
            entry["env"] = {name: reference % name for name in server.env_names}
        return entry
    entry = {"type": "http", "url": server.url} if agent == "claude_code" else {"url": server.url}
    if server.headers:
        entry["headers"] = {
            header.name: ("Bearer " if header.prefix == "bearer" else "") + reference % header.env
            for header in server.headers
        }
    return entry


def _render_codex(server: ServerSpec) -> dict[str, object]:
    entry: dict[str, object]
    if server.transport == "stdio":
        entry = {"command": server.command, "args": list(server.args)}
        if server.env_names:
            entry["env_vars"] = list(server.env_names)
        return entry
    entry = {"url": server.url}
    env_headers: dict[str, str] = {}
    for header in server.headers:
        if header.prefix == "bearer":
            entry["bearer_token_env_var"] = header.env
        else:
            env_headers[header.name] = header.env
    if env_headers:
        entry["env_http_headers"] = env_headers
    return entry


# --- pure requirement check ---------------------------------------------------


def check_requirements(
    requirements: Mapping[str, Mapping[str, McpServerRequirement]],
    target_agents: Sequence[str],
    view: Iterable[EffectiveServer],
) -> RequirementReport:
    """Decide skill MCP requirements against an effective post-write view.

    Pure replacement for the decision logic of `installer._check_mcp_servers`:
    the missing-command and trust-gated warnings stay with the installer.
    """
    agents = sorted(set(target_agents))
    active: dict[tuple[str, str], list[EffectiveServer]] = {}
    for server in view:
        if server.state == "active":
            active.setdefault((server.name, server.agent), []).append(server)

    found: dict[str, dict[str, list[str]]] = {}
    errors: list[str] = []
    for skill, skill_requirements in requirements.items():
        if not skill_requirements:
            continue
        per_skill: dict[str, list[str]] = {}
        for requirement in skill_requirements.values():
            satisfied: list[str] = []
            mismatched: list[str] = []
            for agent in agents:
                entries = active.get((requirement.name, agent), [])
                if any(_satisfies(requirement, entry) for entry in entries):
                    satisfied.append(agent)
                elif entries:
                    mismatched.append(agent)
            per_skill[requirement.name] = satisfied
            missing = [agent for agent in agents if agent not in satisfied]
            failed = False
            if requirement.required_in == "all":
                if missing:
                    failed = True
                    errors.append(
                        f"MCP server {requirement.name!r} required by {skill} is not configured "
                        f"for agent(s): {', '.join(missing)}. Hint: {requirement.hint}"
                    )
            elif not satisfied:
                failed = True
                errors.append(
                    f"MCP server {requirement.name!r} required by {skill} is not configured "
                    f"in any target agent environment. Hint: {requirement.hint}"
                )
            if failed and mismatched:
                errors.append(
                    f"MCP server {requirement.name!r} required by {skill} is planned with a transport "
                    f"other than {requirement.transport!r} for agent(s): {', '.join(mismatched)}"
                )
        found[skill] = per_skill
    return RequirementReport(found=found, errors=errors)


def _satisfies(requirement: McpServerRequirement, entry: EffectiveServer) -> bool:
    if entry.origin == "planned" and requirement.transport is not None:
        return entry.transport == requirement.transport
    return True
