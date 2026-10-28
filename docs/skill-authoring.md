# Руководство по созданию скиллов CocoaSkills

Это руководство определяет рекомендуемый контракт для репозиториев скиллов CocoaSkills. Документ служит практическим руководством для авторов к [RFC 0003](v0.5-design.md) и текущему [принятому ядру протокола Curator rc.10](https://github.com/relux-works/curator-spec/blob/v1.0.0-rc.10/protocol/core.md).

## 1. Структура репозитория

Рекомендуемая структура каталогов:

```text
skill-example/
  SKILL.md
  agent-skill.json
  agents/
  references/
  assets/
  templates/
  examples/
  data/
  scripts/
  .skill_triggers/
```

Установщик намеренно отфильтровывает контекст промпта. CocoaSkills копирует в `<project>/.agents/skills/<skill>/` только файлы для агента.

Файлы только для runtime относятся к `runtime_roots`. Установщик копирует их в каталог:

```text
~/.cocoaskills/runtime/<skill>/<commit>/
```

Не рассчитывайте на доступность агенту после установки файлов README, тестов, файлов сборки, virtualenv, метаданных пакетов или файлов CI.

### Чего пакет скилла не делает

- **Не коммитьте собранные бинарники** ни в скилл, ни в билд-репозиторий: они обходят trust-модель целиком (нет идентичности тулчейна, receipt, защищённого кеша). Тип команды `binary` не существует, манифест с ним не установится.
- **Keep the installed runtime immutable.** Put virtual environments, caches, generated files and mutable state in user-owned directories such as the XDG cache/state directories, outside every csk-published skill tree. See the runtime contract below.
- **Не держите `agents/runtime.json`.** Это легаси-фоллбэк: старые csk читают его вместо манифеста и превращают "нужен csk новее" в бессмысленный `source file not found`.

### Installed runtime is immutable

On POSIX, csk publishes runtime directories and regular files with all write
bits removed, preserving execute bits. An ordinary skill process therefore
gets `EACCES` when it writes there; Python imports still work and silently skip
unwritable `__pycache__` output. Existing installations acquire these modes on
their next successful install or upgrade. These permissions prevent accidental
writes; they are not a sandbox against an owner who deliberately changes modes.

Install, replacement, upgrade, removal and GC use the transaction engine.
Construction copies are private and writable; recorded final modes are applied
before publication. Old generations are renamed out of the live namespace
before journaled cleanup restores owner write on directories whose entries it
removes. On POSIX, cleanup unlinks regular files without changing their modes:
a hardlink outside the tree retains its permissions. Private file mode changes
refuse multiply-linked inodes rather than changing another link. Recovery
resumes an interrupted mode change, and rollback preserves the old tree and its
recorded modes. Never recursively chmod the live runtime as a repair step.

On Windows csk does not change permissions or ACLs. Foreign-entry detection and
the refusal described below still apply.

If a skill has already created `.venv`, an extra file/directory, a symlink or a
special file in its installed runtime (or another csk-owned published skill
tree), `install` and `upgrade` refuse with `runtime_tree_modified`. Runtime
inventories come from the retained source snapshot; context inventories come
from the install marker. The diagnostic names the skill and member, shows the
offending path relative to `$CSK_HOME` when possible, and prints a shell-quoted
remedy that removes only the foreign entry or its foreign enclosing directory.
On POSIX the removal step is `rm -rf -- '…/.venv'`. For a foreign directory,
the diagnostic first grants owner access to that directory and uses `find -P`
to grant access to directories inside that subtree before removal. This also
handles readable directories that lack write permission and restrictive nested
directories, even when the foreign root is writable.
It never chmods regular files or follows symlinks, preserving outside hardlinks.
On Windows it uses PowerShell
`Remove-Item -LiteralPath '…/.venv' -Recurse -Force`. Inspect that exact path,
remove only that entry, and rerun the same csk command. csk never automatically
deletes foreign entries and never recommends deleting the entire member tree.

If a legacy generation has lost its retained snapshot, csk preserves it and
reports `runtime_inventory_unavailable` rather than trusting its current files
as an inventory. Reinstalling a selected member can restore its exact snapshot
from the trusted source; an old unselected generation needs its corresponding
trusted snapshot restored before collection. GC retains unsafe generations and
their snapshot evidence, reporting the diagnostic as a warning.

## 2. Обязательные файлы

Каждый устанавливаемый скилл содержит файл:

```text
SKILL.md
```

`SKILL.md` определяет контракт для агента. Файл описывает использование скилла агентом, доступные команды и ожидаемые входные и выходные данные команд.

Если скилл экспортирует команды, добавьте файл:

```text
agent-skill.json
```

`agent-skill.json` определяет машиночитаемый манифест runtime. Манифест объявляет экспортируемые скиллом точки входа команд и используемые скиллом зависимости.

## 3. Версии схем `agent-skill.json`

### Схема v1

Схема v1 обеспечивает режим совместимости:

```json
{
  "schema_version": 1,
  "commands": {
    "tool": {
      "type": "script",
      "unix_path": "scripts/tool"
    }
  }
}
```

Используйте схему v1 только для однофайловых скриптов без зависимостей от соседних файлов. CocoaSkills копирует каждую скриптовую команду как отдельный файл в каталог:

```text
~/.cocoaskills/runtime/<skill>/<commit>/bin/<command>
```

### Схема v2

Схема v2 определяет формат runtime для скиллов с многофайловыми командами:

```json
{
  "schema_version": 2,
  "runtime_roots": ["scripts"],
  "commands": {
    "tool": {
      "type": "script",
      "unix_path": "scripts/tool"
    }
  }
}
```

Используйте схему v2, когда команда зависит от соседних файлов, библиотек, модулей Python, вспомогательных шелл-скриптов или других ресурсов runtime.

### Схема v3

Схема v3 добавляет явный конверт возможностей для аудита:

```json
{
  "schema_version": 3,
  "runtime_roots": ["scripts"],
  "capabilities": {
    "network": ["gitlab.example.com"],
    "filesystem": "repo",
    "exec": ["glab"],
    "secrets": "none",
    "env_read": ["HOME"],
    "prompt_scope": "Read merge request metadata and prepare local review output."
  },
  "commands": {
    "review": {
      "type": "script",
      "unix_path": "scripts/review"
    }
  },
  "dependencies": {
    "commands": {
      "glab": {
        "type": "system",
        "command": "glab",
        "hint": "Install GitLab CLI through project bootstrap tooling"
      }
    }
  }
}
```

Используйте схему v3 для скиллов, проходящих строгий аудит. Схемы v1 и v2 остаются устанавливаемыми. Строгий аудит считает их необъявленными: при использовании рабочего процесса доверия скилл либо переходит на схему v3, либо фиксируется по content-hash.

Поля возможностей:

- `network`: `"none"` или маски хостов для сетевых подключений кода скилла.
- `filesystem`: `"repo"`, `"home-config"` или явные пути.
- `exec`: `"none"` или имена исполняемых файлов, вызываемых скиллом.
- `secrets`: `"none"` или имена секретов и связок ключей, читаемых скиллом.
- `env_read`: переменные окружения, читаемые скиллом.
- `prompt_scope`: одно предложение с описанием разрешенных действий агента по промпту.

### Схема v4

Схема v4 добавляет требования скилла к скиллам. Скилл объявляет используемые скиллы в секции `dependencies.skills`. csk разрешает транзитивное замыкание и устанавливает провайдеров. Полный дизайн описан в [RFC 0007](v0.9-design.md).

```json
{
  "schema_version": 4,
  "runtime_roots": ["scripts"],
  "capabilities": {
    "exec": ["trk", "git"],
    "network": "none"
  },
  "commands": {
    "report": { "type": "script", "unix_path": "scripts/report" }
  },
  "dependencies": {
    "skills": {
      "skill-tracker": {
        "git": "git@gitlab.example.com:skills/skill-tracker.git",
        "ref": { "kind": "revision", "value": "0123456789abcdef0123456789abcdef01234567" },
        "mode": "runtime",
        "commands": ["trk"]
      }
    },
    "commands": {
      "git": { "type": "system", "command": "git" }
    }
  }
}
```

Правила требований:

- Поля `git` и `ref` обязательны; запись самодостаточна.
- Поле `ref.kind` принимает значения `tag` или `revision`. Ссылки на ветки и диапазоны версий вызывают ошибку разбора.
- Закрепляйте провайдеров через `revision`. Проект на Skillfile схемы 2 принимает транзитивное требование только с `revision` и отклоняет `tag` с `source_selection_invalid`. Проект на схеме 1 принимает оба варианта, поэтому `revision` работает для любого потребителя.
- Требование к команде провайдера через `dependencies.commands` с типом `skill` провайдера не загружает: потребитель должен перечислить провайдера в своём Skillfile. Чтобы csk ставил провайдера сам, объявите его в `dependencies.skills`.
- Поле `mode` выбирает передаваемые потребителю компоненты провайдера: `full` (по умолчанию) активирует контекст промпта и все экспортируемые команды, `runtime` активирует только команды, `context` активирует только контекст промпта. Необязательный список `commands` ограничивает требование `runtime` указанными экспортами.
- В рамках одного замыкания установки имя скилла разрешается в один commit и один канонический источник. Несогласованные требования прерывают установку.
- Workflow представляет собой скилл с объявленными требованиями без экспорта команд. Потребители устанавливают workflow одной записью в `Skillfile.json`.
- Для разработки файл `Skillfile.dev.json` рядом с проектным `Skillfile.json` подменяет провайдеров локально (указывает `path` к рабочей копии или `git` с любым типом ref, включая ветки). Файл входит в локальный управляемый блок `$GIT_COMMON_DIR/info/exclude` (или `.gitignore` при явном/legacy режиме `gitignore`). Строгий аудит завершается ошибкой при активных подменах.
- Организации ограничивают источники скачивания скиллов с помощью `allowed_sources` в `~/.cocoaskills/config.json`: списка канонических префиксов `host/path`, проверяемого перед операциями clone или fetch.

### Схема v5

Схема v5 добавляет зависимости от MCP-серверов. Скилл объявляет используемые MCP-серверы в секции `dependencies.mcp_servers`. `csk install` проверяет конфигурацию каждого сервера в целевых окружениях агентов перед доставкой скилла. Эта проверка только читает конфигурацию. Объявления для записи задаёт project Skillfile, как описано в [разделе schema 3](#project-mcp-schema-3).

```json
{
  "schema_version": 5,
  "capabilities": { "exec": "none", "network": "none" },
  "dependencies": {
    "mcp_servers": {
      "sheets": {
        "hint": "Add the sheets MCP server to your agent configuration.",
        "transport": "http",
        "required_in": "any"
      }
    }
  }
}
```

Правила зависимостей MCP:

- Поле `hint` обязательно и объясняет оператору подключение сервера.
- Необязательное поле `transport` документирует транспорт: `stdio` или `http`.
- Поле `required_in` выбирает семантику проверки: `any` (по умолчанию) требует сервер хотя бы в одном целевом окружении агента, `all` требует сервер во всех окружениях.
- Проверяемые конфигурации агентов: Claude Code (`<project>/.mcp.json`, `~/.claude.json`), Codex CLI (`<project>/.codex/config.toml`, `~/.codex/config.toml`), Cursor (`<project>/.cursor/mcp.json`, `~/.cursor/mcp.json`), Gemini (`<project>/.gemini/settings.json`, `~/.gemini/settings.json`), Windsurf (`~/.codeium/windsurf/mcp_config.json`), OpenCode (`<project>/opencode.json`, `<project>/opencode.jsonc`, `~/.config/opencode/opencode.json`, `~/.config/opencode/opencode.jsonc`). Отсутствующие или некорректные файлы считаются отсутствием настроенных серверов.
- Неуспешная проверка останавливает установку и выводит подсказку. Маркеры установки фиксируют найденные места расположения серверов.

<a id="project-mcp-schema-3"></a>

### MCP-серверы в project Skillfile (schema 3)

Project Skillfile объявляет MCP-серверы для установки. Поле `mcp_servers` в `Skillfile.json` задаёт записи, которые `csk install` пишет в конфигурации агентов. Поле `dependencies.mcp_servers` в `agent-skill.json` задаёт требования скилла; эта проверка только читает конфигурации. Версия манифеста скилла и schema проектного Skillfile независимы.

Schema 3 сохраняет все поля и правила schema 2. Она добавляет только необязательное поле `mcp_servers`. Выбирайте schema 3, когда проекту нужно это поле. Пустое или отсутствующее `mcp_servers` не создаёт MCP-конфигураций. Собственные команды записи Skillfile не повышают schema до 3 автоматически. Глобальный Skillfile остаётся на schema 1; `csk global install` отклоняет schema 3 с ошибкой обновления версии.

#### URL shorthand

Для HTTP-сервера без env references достаточно URL. Полный project Skillfile для трёх агентов выглядит так:

```json
{
  "schema_version": 3,
  "agents": ["claude_code", "codex_cli", "cursor"],
  "skills": [],
  "mcp_servers": {
    "docs": "https://mcp.example.com/mcp"
  }
}
```

`csk install` запишет сервер `docs` для каждого выбранного writable агента. Этот пример не требует consent и работает без терминала. Эквивалентная объектная форма хранится в [`tests/fixtures/mcp/docs-example.Skillfile.json`](../tests/fixtures/mcp/docs-example.Skillfile.json):

```json
{
  "schema_version": 3,
  "agents": ["claude_code", "codex_cli", "cursor"],
  "skills": [],
  "mcp_servers": {
    "docs": {
      "server": { "transport": "http", "url": "https://mcp.example.com/mcp" },
      "install": { "auth": { "kind": "none" } }
    }
  }
}
```

URL shorthand равен объекту `server` с `transport: "http"` и тем же `url`. URL использует `https` и ASCII host. Userinfo, query и fragment запрещены. Не помещайте секреты в URL, включая путь. Ограничение длины составляет 2048 символов; пробелы, управляющие и format-символы, `$`, фигурные скобки и backslash запрещены. Проверка секретов отклоняет обнаруженный токен до записи.

#### Object form

Объект объявления содержит обязательный `server` и необязательный `install`. Другие поля на этом уровне запрещены. Имя сервера соответствует `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`; имена уникальны без учёта регистра, зарезервированные Windows-имена запрещены.

Следующий фрагмент задаёт значение поля `mcp_servers` для stdio и HTTP с env header:

```json
{
  "tools": {
    "server": {
      "transport": "stdio",
      "command": "uvx",
      "args": ["example-mcp==1.2.3"],
      "env_names": ["TOOLS_TOKEN"],
      "environments": ["claude_code"]
    },
    "install": {
      "auth": { "kind": "env", "help_url": "https://example.com/tokens" },
      "scope": "project",
      "git_policy": "local"
    }
  },
  "api": {
    "server": { "transport": "http", "url": "https://api.example.com/mcp" },
    "install": {
      "headers": { "Authorization": { "env": "API_TOKEN", "prefix": "bearer" } },
      "auth": { "kind": "env" }
    }
  }
}
```

Both declarations require consent. `tools` selects only Claude Code; `api` has no selector, so it selects all three writers independently of skill agents. To add a separately approved local Cursor destination for `tools`, run `csk mcp local-target add cursor --server tools` and then `csk install`.

Набор полей `server` закрыт: `transport`, `url`, `command`, `launcher`, `args`, `env_names`, `environments`. HTTP использует `url`; stdio использует ровно одно из `command` и `launcher`, а также массив `args`. `command` содержит имя executable без пути или shell-команды. Для `npx` и `uvx` нужна точная версия пакета, например `example-mcp==1.2.3` для `uvx`. Секреты в `args` запрещены. `env_names` перечисляет имена переменных, а не значения; управляющие переменные вроде `PATH` запрещены. Нативный клиент может передать серверу больше переменных, чем перечисляет `env_names`; csk не гарантирует изоляцию окружения.

`server.environments` accepts exactly `claude_code`, `codex_cli`, `opencode`, `pi`, `muse`. The first two have native writers; explicitly selected remaining ids produce `skipped: unsupported`, with path unavailable, without failing the operation by themselves. An absent selector selects Claude Code, Codex CLI and csk-local Cursor independently of skill `agents`; absence is preserved in normalized identity. To narrow this set, use `"environments": ["claude_code"]`. Empty, duplicate and unknown ids are `mcp_declaration_invalid`.

Cursor is not a canonical id. The 0.21.x private reader still accepts legacy Cursor input and warns once on stderr; normal Skillfile writers refuse with `skillfile_deprecated_form`. Migrate mixed selections with `csk mcp migrate`: only Cursor is removed, canonical members retain their exact bytes, and a local target is recorded atomically. Cursor-only selections require `csk mcp migrate --targets claude_code` (or another explicit canonical choice); they never become absent or empty. The migration requires separate local approval; installation reviews changed canonical selections. `ignore_mode`, if also present, is preserved for its own `csk ignore-mode set` migration. Either migration order is supported when both command slices are installed. Both forms must be removed before the Skillfile is portable; 0.22 stops accepting them. Clean shared syntax does not imply another manager supports csk's inline admission, launcher or install-option behavior.

`csk mcp local-target add|remove|list cursor [--server NAME] [--project PATH]` manages project-bound local configuration under the user config directory. Without `--server`, it applies to servers with explicit selectors. Every project/server target has its own approval; one server's grant cannot approve another, and removal revokes only that target. New local targets require a terminal and write nothing when approval is unavailable. Installation, ownership, no-start and rollback gates still apply. Status identifies additional destinations as `local: cursor`; the shared selector and canonical targets are unchanged. Local values other than Cursor refuse with `mcp_local_target_invalid`. `install.environments` is not added.

`install` задаёт локальные параметры установки: как устанавливающий менеджер пишет и аутентифицирует сервер в native configs. Набор полей `install` тоже закрыт. Поле `csk` не является alias и отклоняется с `mcp_declaration_invalid`. HTTP `headers` сопоставляет каждому имени объект `{ "env": "VARIABLE", "prefix": "none" }` либо объект только со значением `{ "value": "issues,merge_requests" }`. Формы взаимоисключающие. csk никогда не читает и не сохраняет значения env. Префиксы образуют закрытый набор: `none` (по умолчанию), `bearer`, `api-token` и `token`; native text соответственно пустой, `Bearer `, `Api-Token ` и `Token `. `bearer` допустим только для `Authorization` без учёта регистра; остальные префиксы принимают любое разрешённое имя header.

Claude Code раскрывает `${VARIABLE}`, а Cursor — `${env:VARIABLE}` в headers. Codex использует `env_http_headers` для `none`, `bearer_token_env_var` для `bearer` и `http_headers` для literals. Его native env fields не могут выразить `api-token` или `token`: выбор Codex с любым из этих префиксов отклоняет весь план с `mcp_header_prefix_unsupported` до любых записей. Для этих префиксов выбирайте только Claude Code и Cursor. csk не создаёт executable helper.

Literal values должны занимать не более 1024 UTF-8 bytes и не содержать `$`, управляющих, format или surrogate characters. Значение должно проходить существующий secret scanner. Literal names не могут быть `Authorization`, `Cookie`, `Proxy-Authorization` или содержать `token`, `key`, `secret` либо `auth` без учёта регистра; прежние запрещённые HTTP names также недопустимы. Один literal не требует consent. OAuth совместим с обеими формами; для HTTP consent нужен только при фактических env references.

`auth` содержит `kind` (`none`, `env` или `oauth`) и необязательный `help_url`. Без `auth` csk выбирает `env` при любой env reference, иначе `none`. `scope` поддерживает только `project`, `git_policy` только `local`. Значения `user` и `shared` отклоняются с `mcp_capability_unsupported`. Полный [справочник полей и кодов отказа](reference.md#project-mcp-reference) перечисляет ограничения.

`csk check` validates schema 3 and MCP declarations with the install parser, without writes or network access. Stdio `command` must be a bare executable. Known shells/wrappers and evaluator-capable commands such as `awk`, `python`, `osascript` and `open` are refused. Use direct `npx` with an exact `package@1.2.3` or `uvx` with `package==1.2.3`.

Recognized runner/evaluator words in `args` refuse with `mcp_command_invalid`.

Staged ignore checks preserve the live `core.excludesFile` source (including relative paths), `.git/info/exclude` and default global excludes.

Pin and no-eval rules cover recognized runners/evaluators. Known evaluator-capable
commands (including awk/sed, interpreters, editors, debuggers and command-launching
OS tools) refuse even with plain script arguments. Within each argument, csk splits
on characters outside `[A-Za-z0-9_.+-]`, ignores case, strips path prefixes and
`.exe`/`.cmd`/`.bat`/`.ps1`/`.com`, and refuses recognized names as words.
Thus `a=npx pkg`, `system("npx pkg")`, `--exec=npx` and data containing the word
`node` refuse; `--node-id` and `my-server --port 1` remain valid. This intentionally
fails closed on false positives. Direct supported pinned `npx`/`uvx` remain valid.
Base-system OS programs cannot be MCP servers, except for two exact Apple
recipes: command `xcrun` with args `["mcpbridge"]`, and command `safaridriver`
with args `["--mcp"]`. Both require the selected executable's kernel-resolved
inode to match a regular base-directory file of that exact command name.
Missing candidates, opaque copies, evaluator aliases, extra arguments and
alternate executable spellings refuse; the exception never skips host identity.
Every other normalized name in the committed base-system inventory refuses as
command regardless of PATH. That inventory combines the dated macOS snapshot
with documented Linux base and Windows System32/Git for Windows families.
This portable name layer is best-effort across OS versions; the current POSIX
host base-directory inode check remains authoritative for unlisted names.
Thus `jq` is refused even if another installation is first in PATH. The existing
evaluator/runner table remains in force. Exec-capable base names, including
`apply`, `lockf`, `xcrun`, `dtrace`, `lockstat`, `sc_usage`, `parl`, `jjs`,
`ssh-keygen` and `xcodebuild`, also refuse as argument words and interpreters.
`check` and install observe PATH without normalizing its components. On POSIX,
kernel stat/access select the first regular executable. Any selected inode also present
in `/usr/bin`, `/bin`, `/usr/sbin` or `/sbin` refuses, including symlink and
hardlink aliases. On Windows, each PATH directory tries the exact name if it
already has an extension, otherwise `.com` then `.exe`, without PATHEXT or
POSIX execute-mode checks. This is the shared PATH policy, not an exact model
of every agent's search: libuv also has implicit cwd search and extended-name
suffix fallback; Rust std uses `.exe` only for extensionless names and has
additional application/system searches. Those implicit searches and later
agent environments remain outside this PATH observation.
Batch launchers refuse with `mcp_windows_batch_unsupported` before host header
admission when a supported native write target is selected. The selected candidate's final kernel path is
compared against the resolved `%SystemRoot%`, ignoring case and normalizing
separators with a component boundary; SystemRoot is never traversed. An unset
or unresolvable SystemRoot refuses stdio commands, including pinned runners and
missing PATH candidates. Known
evaluator identities in other PATH/default directories also refuse. A searchable
PATH directory that cannot be enumerated fails closed rather than guessing
incomplete versioned or case-sensitive spellings. No command is executed.
Only the selected candidate's first 1024 bytes may be read. An unreadable
selected header refuses. Windows requires PE (`MZ`) magic and never interprets
a shebang; all other selected headers refuse. On POSIX, a readable header must
start with `#!` or recognized native magic: Mach-O, ELF or PE (`MZ`). Universal Mach-O headers require an
architecture count of 1 through 16 in the matching byte order; larger counts,
including usual Java class headers, refuse. This recognizes magic, not full
loader validity. Empty files and text without `#!` refuse. A script line must
end with newline within the first 256 bytes, including the newline. The
interpreter word uses strict UTF-8, splits at horizontal whitespace without shell quoting or stripping CR,
and refuses if it contains `#`. A separate `# comment` argument is permitted.
An invalid/non-executable interpreter refuses. For an otherwise opaque
interpreter, only its first two bytes are read: `#!` refuses as a nested script;
an unreadable interpreter header also refuses. No nested chain is followed.
These conservative refusals do not emulate every OS's script parsing.
Errors use `mcp_command_invalid` and name the bare candidate; selection never
continues to a later PATH entry. A missing non-base candidate retains the
portable result. Supported exact-pin runners retain their interpreter substrate
after the base-system and selected-header checks on POSIX; Windows runners
require PE magic like other Windows candidates.
This is a point-in-time host check: concurrent directory/executable replacement
or another agent's later PATH can differ, and filesystem identities are not
frozen. A same-user attacker who can write PATH directories already controls the
agent and is outside this boundary. A third-party evaluator outside base dirs
remains opaque unless its name or inode matches the evaluator inventory; a copy
outside those dirs need not share an inventoried inode. This is no sandbox or
proof of what an opaque program runs. On a CLT-only host, `xcrun` can fall back to PATH
when its developer directory lacks `mcpbridge`; that downstream tool selection
is not checked. The exception verifies the base `xcrun` identity and exact argv,
not the executable it may subsequently choose.

Any remaining bare program runs as is: csk cannot verify what it runs. Consent shows
its exact command and argument array and states this limit. This is no sandbox or
proof about arbitrary programs; non-default evaluators outside the recognized
inventory and hidden execution semantics remain the documented limit.


For MCP files csk creates or its ownership ledger records as created, install verifies the proposed ignore hierarchy with Git before publication. Root rules defeated by a later negation are appended effectively; a conflicting nested ignore rule causes `gitignore_required` before consent or publication. Existing user files and tracked files are not automatically ignored. Output-parent readiness is also checked before consent, while publication retains its concurrency checks and rollback.

#### Запись и consent

`csk install` пишет native project configs:

| Агент | Project-relative path |
| --- | --- |
| Claude Code (`claude_code`) | `.mcp.json` |
| Codex CLI (`codex_cli`) | `.codex/config.toml` |
| Cursor (`cursor`) | `.cursor/mcp.json` |

Для созданного MCP-файла с `git_policy: "local"` csk автоматически добавляет точный путь в `$GIT_COMMON_DIR/info/exclude` (или в `.gitignore` при режиме `gitignore`): `/.mcp.json`, `/.codex/config.toml` или `/.cursor/mcp.json`, и сообщает добавленные entries. Ignore публикуется до native config в одной транзакции. Обычный `csk init` + `csk install` не требует deprecated `--fix-gitignore`; dry-run только показывает план. В режиме `exclude` install также поддерживает обычные generated paths; в режиме `gitignore` для них используйте предварительный `csk init`. В режиме `gitignore` уже отслеживаемый git файл csk меняет на месте и не добавляет в ignore. В режиме `exclude` отслеживаемый собственный путь отказывает с `gitignore_required`; сначала уберите его из index, сохранив локальный файл. csk сохраняет байты вне изменяемых записей. JSON-файлы должны быть строгим UTF-8 JSON без BOM, comments и trailing commas.

CocoaSkill хранит отдельный блок для каждого проекта/worktree между `# CocoaSkill project <key>` и `# End CocoaSkill project <key>`. Непрозрачный ключ определяется метаданными worktree и путём к корню проекта относительно репозитория. При обновлении меняется только собственный блок; остальные блоки и пользовательские байты остаются без изменений. Все операции записи выполняются по очереди под OS lock в постоянном файле `$GIT_COMMON_DIR/csk-exclude.lock`. При завершении процесса ОС освобождает lock; replay и rollback транзакции объединяют с текущим файлом только записанный блок проекта. Не удаляйте lock-файл, пока csk работает. Старые exclude-блоки без ключа сохраняются как существующие правила; удаляйте устаревшие записи вручную.

Consent разрешает публикацию исполняемого кода или env reference. Он нужен для stdio и для любой ссылки на переменную в `env_names` или `install.headers`. HTTP без env reference, включая `auth.kind: "oauth"`, consent не требует. csk никогда не запускает сервер, package runner или команду аутентификации.

Для нового или изменённого объявления с такими полномочиями запустите `csk install` в терминале. csk покажет план и спросит `Approve N MCP server(s) for <project>? [y/N]`. Запрос появляется после неинтерактивных preflight-проверок. Ответ `y` или `yes` разрешает публикацию; consent сохраняется в приватном csk home в одной транзакции с конфигурациями. При отказе публикации новое согласие не остаётся сохранённым. Без терминала или при отказе csk возвращает `mcp_plan_unapproved`. Флаг, env variable, поле Skillfile или файл проекта не дают consent. Сохранённое согласие относится к проверенному объявлению этого проекта и набору writable агентов. Добавление writable агента требует нового согласия. Unsupported ids не входят в consent subject; объявление только для них согласия не требует.

Для просмотра плана запустите `csk install --dry-run`. Команда покажет строки `would:` и состояние consent каждого объявления: `required`, `approved` или `not-required`. Она ничего не пишет и не сохраняет consent. Если единственное препятствие состоит в consent, dry run завершается с кодом 0.

#### Ownership и drift

Ownership связывает запись с проектом в приватном ledger csk home. csk меняет только собственные записи. Чужая запись с тем же именем вызывает `mcp_entry_conflict`, даже если её содержимое совпадает. Выберите другое имя в Skillfile или вручную разрешите конфликт в native config. Автоматического takeover нет.

Удаление объявления из Skillfile или исключение агента удаляет неизменённую owned entry при следующем `csk install`. csk удаляет целый файл только тогда, когда сам создал его и файл стал пустым. Содержимое чужих записей остаётся на месте.

Drift означает, что пользователь изменил owned entry после установки. Действие зависит от объявления и текущих байтов:

| Состояние | Действие csk | Как управлять результатом |
| --- | --- | --- |
| Запись объявлена и изменена вручную | `skipped: drift`, предупреждение `mcp_entry_drift`; ownership сохраняется, установка продолжается | Сохраните ручную настройку или верните запись к установленному виду для дальнейших обновлений через Skillfile. |
| Запись объявлена и удалена вручную | `restored` под сохранённым consent | Чтобы убрать сервер окончательно, удалите объявление из Skillfile. Для отключения используйте native disable агента. |
| Объявление удалено, запись изменена вручную | `released`, предупреждение; csk забывает ownership и сохраняет запись | Дальше управляйте native entry вручную или удалите её самостоятельно. |
| Объявление и native entry удалены | csk молча забывает ownership | Дополнительные действия не нужны. |

#### Отчёт и аутентификация

Каждая пара server/agent получает ровно одну строку. Writable пары содержат точный project-relative path; unsupported пары показываются как `mcp NAME -> AGENT (skipped: unsupported)` без пути и отдельного предупреждения. Порядок стабилен: имя сервера по возрастанию, затем Claude Code, Codex CLI и Cursor, затем unsupported ids по алфавиту. Это правило действует и при отказе; dry-run добавляет `would:`. Успешная установка может показать такой отчёт:

```text
mcp docs -> claude_code .mcp.json (added)
mcp docs -> codex_cli .codex/config.toml (added)
mcp docs -> cursor .cursor/mcp.json (added)
MCP configuration installed. Authentication not checked.
```

Действия также включают `updated`, `unchanged`, `restored`, `removed`, `released`, `skipped: drift` и `skipped: unsupported`. После записи csk явно сообщает, что аутентификацию не проверял.

Отказ тоже перечисляет все затронутые пары до итоговой ошибки. Например, headless install HTTP-сервера `api` с env header из примера сообщает:

```text
mcp api -> claude_code .mcp.json (refused: mcp_plan_unapproved)
mcp api -> codex_cli .codex/config.toml (refused: mcp_plan_unapproved)
mcp api -> cursor .cursor/mcp.json (refused: mcp_plan_unapproved)
```

После этих строк следует отказ `mcp_plan_unapproved` с рекомендацией повторить установку в терминале. Чужая одноимённая запись получает действие `conflict: foreign entry`. Блокирующий отказ не публикует ни native entries, ни скиллы, ни ledger. Общая ошибка проекта показывается с её кодом либо как `blocked by project_preflight_failed`; сервер без потребности в consent при блокировке соседним сервером получает `blocked by mcp_plan_unapproved`.

csk печатает подсказки для агентов, получивших запись. В Claude Code выполните `/mcp`, чтобы проверить сервер и войти. Для Codex CLI с `auth.kind: "oauth"` выполните `codex mcp login <name>`. В Cursor откройте `Cursor Settings > MCP`. csk не выполняет эти действия и не проверяет доступность сервера.

#### Проверка MCP через status

После установки URL shorthand из примера выше запустите:

```bash
csk status
csk status --check
csk status --check --json
```

Для project Skillfile schema 3 `status` показывает каждую объявленную или ещё owned пару server/agent. Формат строки: `mcp <server> -> <agent> <project-relative path> (<state>)`. Порядок совпадает с install: имя сервера по возрастанию, затем `claude_code`, `codex_cli`, `cursor`, затем unsupported ids по алфавиту. Для них строка не содержит пути и имеет состояние `unsupported`.

```text
mcp docs -> claude_code .mcp.json (current)
mcp docs -> codex_cli .codex/config.toml (current)
mcp docs -> cursor .cursor/mcp.json (current)
MCP authentication and connectivity: not checked.
```

| State | Значение |
| --- | --- |
| `unsupported` | Известное окружение без поддержки записи; конфигурация не меняется. |
| `current` | Owned entry соответствует объявлению и последней записи csk. |
| `updated (informational)` | Содержимое launcher изменилось при прежнем consent subject, включая явно разрешённый unverified launcher; `--check` сохраняет успех. |
| `untrusted` | Проверка macOS signature/seal/notarization или Windows signature/publisher не пройдена либо для unverified launcher не задан явный override; `--check` возвращает 1. |
| `missing` | Объявленная native entry или её файл отсутствует. |
| `outdated` | Объявление изменилось, а owned entry сохранила установленное csk содержимое; следующий install обновит её. |
| `drift` | Пользователь изменил существующую owned entry после установки. |
| `conflict` | В native config есть чужая одноимённая запись, даже если её содержимое совпадает. |
| `unapproved` | Для текущего объявления требуется consent, которого ещё нет. |
| `unreadable` | Native config нельзя безопасно прочитать или разобрать, например из-за некорректного JSON/TOML или symlink. |
| `orphaned` | Ledger ещё содержит owned entry, но объявление удалено или агент исключён; следующий install обработает её удаление или release. |

Launcher signature failures take precedence as `untrusted`. For other entries, the state priority is: `unreadable` → `orphaned` → `conflict`/`drift` → `unapproved` → `outdated` → `missing` → `updated (informational)` → `current`. Поэтому повреждённый native config остаётся `unreadable` даже после удаления объявления, а чужая запись остаётся `conflict` при отсутствии consent.

Например, замените URL в Skillfile на `https://mcp.example.com/mcp-v2`, оставив native configs без изменений: строки станут `outdated`, а `csk status --check` вернёт 1. После `csk install` они снова станут `current`. Ручное изменение самой owned entry вместо Skillfile даст `drift`.

Обычный `csk status` возвращает 0 и при неактуальных строках: это отчёт, а не проверочный gate. С `--check` код 0 означает, что все MCP-строки `current`, `unsupported` или `updated (informational)` (или строк нет) и остальные проверки проекта тоже успешны. Любое другое MCP-состояние, ошибка проекта или неактуальный скилл дают 1. Ошибки конфигурации или использования команды дают 2.

Если приватный ledger или consent store нельзя проверить, csk выводит `ERROR mcp_ownership_unknown`, а не считает хранилище пустым. MCP-строк для такого проекта нет; `--check` возвращает 1. В JSON ошибка находится в `errors`, а `mcp` остаётся пустым массивом.

В `--json` результат проекта входит в массив проектов. Объект проекта содержит `mcp` — массив строк с полями `server`, `agent`, `path`, `state`; например, `{"server":"docs","agent":"codex_cli","path":".codex/config.toml","state":"current"}`. Для `unsupported` поле `path` равно `null`. Поле `clean` учитывает и MCP, и остальные проверки проекта. Текстовая строка про auth/connectivity в JSON не добавляется.

`status` только читает состояние: не меняет native configs, ledger или consent, не запрашивает согласие и не запускает серверы или auth-команды. `current` подтверждает состояние конфигурации; аутентификация и доступность сервера не проверены. Для project schema 1/2 и global status MCP-строки и JSON-поле `mcp` не добавляются.

<a id="project-mcp-recipes"></a>

#### Примеры для реальных серверов и текущие ограничения

Ниже фрагменты поля `mcp_servers` для распространённых серверов. Адреса условные, подставьте свои.

**GitLab**: OAuth выполняется в агенте. Этот recipe выбирает toolsets через несекретный literal header и не требует consent. Выбор toolsets требует GitLab 19.5 или новее с включённым MCP; см. [поддерживаемые toolsets](https://docs.gitlab.com/user/model_context_protocol/mcp_server/#select-tool-groups-toolsets). `work_items` и `merge_requests` поддерживаются; `issues` не является именем toolset.

```json
{
  "gitlab": {
    "server": { "transport": "http", "url": "https://gitlab.example.com/api/v4/mcp" },
    "install": {
      "headers": { "X-Gitlab-Enabled-Mcp-Server-Toolsets": { "value": "work_items,merge_requests" } },
      "auth": { "kind": "oauth", "help_url": "https://docs.gitlab.com/user/model_context_protocol/mcp_server/" }
    }
  }
}
```

Если инстанс не поддерживает автоматическую регистрацию OAuth-клиента, используйте fine-grained personal access token с доступом к MCP и env header: `"headers": { "Authorization": { "env": "GITLAB_MCP_TOKEN", "prefix": "bearer" } }` и `"auth": { "kind": "env" }`.

**Allure TestOps** (MCP endpoint `/api/mcp`): используйте `prefix: "api-token"` для `Authorization: Api-Token <token>`. В переменной храните только токен. Recipe явно выбирает Claude Code и Cursor, поскольку Codex не может выразить этот prefixed env reference нативно:

```json
{
  "allure": {
    "server": { "transport": "http", "url": "https://testops.example.com/api/mcp", "environments": ["claude_code"] },
    "install": {
      "headers": { "Authorization": { "env": "ALLURE_TESTOPS_TOKEN", "prefix": "api-token" } },
      "auth": { "kind": "env", "help_url": "https://docs.qameta.io/allure-testops/howto/mcp/" }
    }
  }
}
```

```bash
export ALLURE_TESTOPS_TOKEN="<token>"
```

Инструкция Qameta подключает тот же сервер через мост `npx mcp-remote` с токеном в аргументах. Закреплённый мост без токена csk примет (`"command": "npx"`, `"args": ["mcp-remote@<версия>", "https://testops.example.com/api/mcp"]`, с consent), но передать ему токен нельзя: подстановка `${VAR}` в `args` отклоняется с `mcp_declaration_invalid`, а токен прямо в `args` был бы секретом в конфиге. На Windows `npx` отклоняется с `mcp_windows_batch_unsupported`. Используйте прямое HTTP-подключение выше.

<a id="app-bundled-launchers-macos"></a>
#### App-bundled launchers (macOS, Windows и Linux)

Используйте `server.launcher` вместо `server.command` для подписанного application helper.
Следующий объект — значение `mcp_servers` для Proxyman:

```json
{
  "proxyman": {
    "server": {
      "transport": "stdio",
      "launcher": {
        "macos": {
          "bundle_id": "com.proxyman.NSProxy",
          "team_id": "3X57WP8E8V",
          "executable": "Contents/MacOS/mcp-server"
        },
        "windows": {
          "install_dir": "%LOCALAPPDATA%\\Programs\\Proxyman",
          "executable": "mcp-server.exe",
          "publisher": "TablePlus Inc",
          "trust": "allow-unverified"
        },
        "linux": {
          "path": "${XDG_CONFIG_HOME:-~/.config}/Proxyman/bin/mcp-server",
          "trust": "allow-unverified"
        }
      },
      "args": []
    }
  }
}
```

На стандартном Ubuntu/Debian с umask `002` новым каталогам приложения может потребоваться однократное исправление permissions. Используйте точный путь и mode из диагностики; для этого recipe один из возможных каталогов исправляется так:

```sh
chmod -- g-w "${XDG_CONFIG_HOME:-$HOME/.config}/Proxyman/bin"
```

Примените указанную команду ко всем остальным элементам с group write, включая раскрытый config root. Если указан world write, удалите и его через `o-w`.

Сначала установите приложение, затем выполните `csk check`, `csk install` в терминале и `csk status --check`. csk ищет bundle по объявленному ID в `/Applications`, `~/Applications`, `/Applications/Setapp` или `~/Applications/Setapp`. Несколько разных bundles с одним ID дают `mcp_launcher_ambiguous`; отсутствие совпадений — `mcp_launcher_not_found`. Executable должен быть относительным путём `Contents/...` без пустых компонентов, `.` или `..` и обратных слешей. Его realpath должен оставаться внутри bundle и указывать на исполняемый обычный файл; иначе csk отказывает с `mcp_launcher_path_invalid`.

csk использует системный macOS `/usr/bin/codesign` без Xcode CLT для strict, deep, all-architecture проверки bundle и проверки helper. Оба requirements закрепляют notarization, `anchor apple generic` и объявленный Team ID. Signing identifier bundle должен совпадать с `bundle_id`; signing identifier helper — с последним компонентом `executable` (здесь `mcp-server`). Этот компонент должен быть переносимым именем executable. Любой отказ signature, seal, notarization или requirement даёт `mcp_launcher_untrusted`; unsigned fallback отсутствует.

`check` проверяет без записи состояния менеджера или проекта. `install` проверяет до consent и пишет разрешённый абсолютный путь executable прямо в configs Claude Code, Codex CLI и Cursor, без shim. Consent plan явно называет проверенного publisher, path и trust level. Subject связывает эти идентичности, declaration, проект и набор writable agents. Неизменный subject повторно использует consent. Другие path или publisher либо trust downgrade требуют нового consent; upgrade может сохранить approval при прежних path/publisher. Изменение содержимого helper при том же subject информационное. Install повторяет проверку после consent до публикации. Source snapshot trees и records участвуют в общей транзакции со skills, native configs, consent, ledger и ignore entries; отказ при публикации targets откатывает их и удаляет новые пустые родительские каталоги, сохраняя прежние snapshots.

Recovery повторно проверяет записанную в journal launcher declaration и одобренные path, publisher и trust level перед replay любого forward target. Невалидная signature, изменение path/publisher или trust downgrade приостанавливают replay с `mcp_launcher_untrusted` или `mcp_launcher_changed`; прерванное состояние и journal сохраняются для повтора после восстановления записанного доверенного приложения. При таком отказе прежние native entries и consent не продвигаются. Обычные command/HTTP journals от 0.20.1 восстанавливаются без выдуманной launcher observation. Классификация использует запиненные в journal desired и previous ownership ledgers, включая launcher fingerprints, а не текущий Skillfile. Записанный launcher без observation или journal, чьи сохранённые targets не позволяют исключить участие launcher, останавливается с `mcp_recovery_observation_missing` и сохраняет прерванное состояние. Это относится и к ledger-only либо consent/ledger plans без изменения native command. Одного восстановления trust недостаточно для отсутствующей observation. Чтобы отказаться от такой незавершённой публикации, скопируйте transaction ID из диагностики и выполните:

```sh
csk recover --rollback TRANSACTION_ID
csk install
```

Используйте тот же manager home (`CSK_CONFIG`, если задан), что и при прерванной установке. Rollback восстанавливает записанные preimages под manager lock с прежними publication-boundary checks; он не одобряет и не запускает helper. Сохраняйте все journal sidecars до успеха. Перед повтором отклонённого rollback восстановите перемещённые publication boundaries. Следующий install рассматривает текущую declaration как новый plan. Этой командой нельзя откатить committed transaction, ожидающую cleanup; вместо этого повторите `csk install` для завершения cleanup. Rollback восстанавливает journaled file и symlink preimages, но не весь directory layout и прежние directory modes. Созданные при установке пустые каталоги могут остаться в проекте и manager home, включая adapter, source snapshot и state directories. Прежние каталоги, permissions которых сужены для protected state, могут остаться более строгими: consent directory после изменения с `0750` на `0700` остаётся `0700`. Rollback не расширяет directory permissions. Допустимые content-only updates и trust upgrades сохраняют approval; следующий обычный install plan записывает текущий helper fingerprint без нового prompt.

Если все targets committed, но cleanup завершился ошибкой, `transaction_cleanup_deferred` сообщает **Installation committed**, путь journal и записанное и наблюдаемое live state каждого target. Это не сообщение об отказе install. Незавершённый rollback аналогично сообщает `transaction_rollback_deferred` и сохранённое состояние. Сохраняйте journal и sidecars, восстановите перемещённые publication boundaries, затем повторите `csk install` для завершения recovery. Exit 1 в этих случаях означает необходимость operator attention/recovery; он не означает отсутствие записей.

`status` повторяет проверку без записи. Допустимое content-only изменение показывает `updated (informational)` и сохраняет успешный `--check`; install принимает новый content fingerprint без нового prompt. Изменённый consent subject показывает `unapproved`. Невалидная macOS или Windows signature показывает `untrusted`, а `--check` возвращает 1. Authentication и connectivity по-прежнему не проверяются. Проверка выполняется на конкретный момент: native agent запускает helper позже, и csk не замораживает приложение до этого запуска. Vendor checksum для Proxyman не предполагается.

Все platform objects имеют закрытый набор полей. Переносимая grammar проверяется на каждом хосте; разрешается только текущая платформа:

| Платформа | Поля и допустимые расположения | Trust |
| --- | --- | --- |
| `macos` | Обязательны `bundle_id`, `team_id` из десяти заглавных букв/цифр, относительный `executable` внутри `Contents/`. | Обязательны notarized signature и identity; `trust` override отсутствует. |
| `windows` | Обязательны `install_dir`, относительный `.exe` `executable`, непустой `publisher`. Install dir должен быть внутри `%LOCALAPPDATA%\`, `%ProgramFiles%\` или `%ProgramFiles(x86)%\` (регистр root не учитывается). Иные expansions, UNC/device paths, drive-relative paths, alternate data streams, traversal, reserved device names и недопустимые символы Windows paths запрещены. | Валидная совпадающая signature: `verified` внутри Program Files, `signed-user-writable` внутри LocalAppData. Unsigned: `unverified`, требуется `trust: "allow-unverified"`. Невалидная signature или другой publisher всегда дают отказ. |
| `linux` | Обязателен `path` внутри `/usr/bin/`, `/usr/local/bin/`, `/opt/`, `~/.local/bin/`, `~/.config/`, `${XDG_CONFIG_HOME:-~/.config}/` или `${XDG_DATA_HOME:-~/.local/share}/`. Иные expansions, пустые компоненты и traversal запрещены. | Root-owned файл, подтверждённый системным package manager: `verified`. Иначе `unverified`, требуется `trust: "allow-unverified"`. |

Нужна хотя бы одна platform entry. Отсутствие entry для текущей платформы даёт `mcp_launcher_platform_missing`. `trust` принимает только `allow-unverified` и только в Windows/Linux entries. Он допускает unsigned/unverified helper лишь вместе с local consent. Для unverified helper csk не может отличить update от подмены. `signed-user-writable` подтверждает Windows signer на момент проверки; owning user всё ещё может заменить helper до запуска native agent. Same-user attacker при Windows/Linux per-user установке находится вне trust boundary.

Windows разрешает каждый именованный root через `SHGetKnownFolderPath`, используя `FOLDERID_ProgramFiles`, `FOLDERID_ProgramFilesX86` и `FOLDERID_LocalAppData` текущего пользователя. Поддерживается OS relocation без предположений о диске. Значение env, не совпадающее с OS folder, отклоняется с `mcp_launcher_root_conflict`; при отсутствии env используется результат ОС. Ошибка API даёт `mcp_launcher_known_folder_unavailable` без environment fallback. Symlinks, junctions и reparse points разрешаются. Локальный executable должен оставаться внутри разрешённого install directory, а тот — внутри разрешённого OS root. Trust определяется фактическим расположением executable: при пересечении known folders приоритет у LocalAppData, поэтому Program Files placeholder не повышает per-user location до `verified`. Authenticode использует системный `WinVerifyTrust` API через ctypes, без PowerShell. `publisher` должен точно совпадать с **первым CN фактического Subject проверенного primary leaf certificate**, с учётом регистра и без нормализации; fallback на organization (O), display name, SAN или timestamp signer отсутствует. Отсутствие CN даёт отказ. Проверка не показывает UI, использует только cached retrieval, не запрашивает дополнительные revocation checks и отклоняет MD2/MD4. Это сохраняет offline и детерминированное планирование, но **не подтверждает актуальный online revocation status**. Ошибка OS trust/signature и malformed signed files никогда не получают unsigned override. Windows recipe — declaration template: подтвердите фактический Subject CN установленного helper; `allow-unverified` не обходит другого signer.

Linux раскрывает только документированные XDG fallback expressions и home locations. Незаданная или пустая XDG variable использует указанный fallback; относительное значение XDG отклоняется. Унаследованное написание `~/.config/` сохранено для совместимости с прежними declarations: оно обозначает тот же фиксированный fallback location без нового механизма expansion. Realpath должен оставаться внутри раскрытого root. Обычный executable и каждый родитель до root должны принадлежать root или текущему пользователю, без group/world write. Это относится и к root-owned элементам, и к declarations с `allow-unverified`. Отказ называет проблемный элемент, его octal mode и shell-quoted команду исправления, например `chmod -- g-w ~/.local/bin` (также `o-w` при world write).

Стандартный Ubuntu/Debian umask `002` делает созданные пользователем каталоги group-writable, поэтому перед допуском может потребоваться однократное исправление:

```sh
chmod -- g-w "$HOME/.local/bin"
```

Для приложения внутри XDG config root исправьте каждый указанный в отказе элемент, например `chmod -- g-w "${XDG_CONFIG_HOME:-$HOME/.config}/Proxyman/bin"`. При указанном world write удалите и его через `chmod -- g-w,o-w "PATH"` с указанным путём. csk только сообщает команды и никогда не меняет launcher permissions. Для непечатаемых имён csk показывает escaped bytes, octal mode и необходимые permission bits, но не предлагает shell command. Исключений по group membership или account database нет. Root-owned helpers со strict-mode chain проверяются доверенными root-owned системными `/usr/bin/dpkg -S -- PATH` или `/usr/bin/rpm -qf -- PATH`, с фиксированным argv, минимальным environment, timeout пять секунд и без shell. Ошибки probe никогда не устанавливают trust. Package ownership — связь в database, **не checksum/integrity check** установленных bytes. Остальным helpers нужны явный override и local consent.

Поиск и проверка приложения никогда не запускают приложение или helper. Native controls используют установленный Proxyman на macOS, Microsoft-signed binary и tampered copy на Windows, package-owned executable и fixtures с user-owned helper и writable parent на Linux. Этим controls нужны соответствующие native platforms. Linux control с umask `002` отклоняет group-writable `~/.local/bin`, затем принимает тот же unverified helper после удаления group write у каталога.

Текущие ограничения schema 3:

| Ограничение | Что из-за него нельзя |
| --- | --- |
| Unverified Windows/Linux helpers | csk не отличает update от подмены; нужны явный override и local consent. |
| Per-user signed Windows helper | Signature определяет publisher на момент проверки; owning user может заменить файл позже. |
| Offline Windows revocation / Linux package ownership | Нет гарантии актуального online revocation или package-file checksum. |
| Проверка приложения на конкретный момент | Нет гарантии для последующего запуска агентом после изменения приложения. |
| `${VAR}` запрещён в `args` | Передать токен stdio-мосту, например `mcp-remote --header`. |
| Codex поддерживает только env prefixes `none`/`bearer` | `api-token`/`token` отклоняются с `mcp_header_prefix_unsupported`; явно выберите Claude Code/Cursor. |
| Literals несекретны, не более 1024 UTF-8 bytes | Секретные имена, совпадения secret scanner, `$`, управляющие символы и смешанные объекты value/env/prefix отклоняются. |
| HTTP consent зависит от env references | OAuth с literals не требует consent; OAuth с env headers — требует. |
| OAuth только для HTTP | Пометить stdio-мост, который сам проходит OAuth. |

Используйте native HTTP support, где он доступен; csk никогда не встраивает секрет в Skillfile или аргумент команды.

#### Границы slice 1

Записанные project configs предназначены для native agent sessions. Managed sessions других environment managers пока не видят эти конфигурации. Codex CLI также требует доверия к проекту для загрузки project config. Другие environment managers без поддержки schema 3 отклоняют такой Skillfile с ошибкой обновления версии.

Slice 1 поддерживает inline declarations. References и declaration packages появятся позже, как и `git_policy: "shared"`, user scope и запись для других агентов. На Windows stdio требует native executable; batch launchers `.cmd`/`.bat` и известные batch-only команды `npx`, `npm`, `pnpm`, `yarn` вызывают `mcp_windows_batch_unsupported`. Переписывания через `cmd /c` нет.

### Схема v6

Схема v6 добавляет исключенные из контекста корни сборки и скомпилированные команды. Приведенный ниже манифест показывает смешанный пример: одну скомпилированную команду, одну скриптовую команду и одну системную команду оператора в совместимом расположении.

```json
{
  "schema_version": 6,
  "runtime_roots": ["scripts"],
  "build_roots": ["build"],
  "capabilities": {
    "network": "none",
    "filesystem": "repo",
    "exec": ["git"],
    "secrets": "none",
    "env_read": [],
    "prompt_scope": "Inspect a repository and produce local reports."
  },
  "commands": {
    "format-report": {
      "type": "script",
      "unix_path": "scripts/format-report",
      "win_path": "scripts/format-report.cmd"
    },
    "repo-report": {
      "type": "build",
      "driver": "go-v1",
      "source_dir": "build/cmd/repo-report"
    },
    "git": {
      "type": "system",
      "command": "git",
      "hint": "Install Git through project bootstrap tooling"
    }
  },
  "dependencies": {
    "commands": {},
    "mcp_servers": {},
    "skills": {}
  }
}
```

Репозиторий содержит как минимум следующие файлы:

```text
SKILL.md
agent-skill.json
scripts/format-report
scripts/format-report.cmd
build/go.mod
build/cmd/repo-report/main.go
build/vendor/                    checked-in modules when non-standard packages are imported
```

Команда `repo-report` представляет собой один `package main`. Файл `build/go.mod` служит прямым и ближайшим файлом модуля для каталога исходного кода. Исходный код сборки остается в проверенном снапшоте исходников, но не копируется в контекст промпта и в хранилище runtime скриптов. Скомпилированный артефакт команды не хранится в репозитории скилла; csk собирает и активирует артефакт из защищенного состояния менеджера. Пример специально демонстрирует совместимую форму `type: system` в секции `commands`. csk проверяет наличие команды, но не создает для нее лаунчер. Новые скиллы размещают используемые системные команды в `dependencies.commands`.

### Схема v7: внешние репозитории сборки

Схема v7 добавляет `build_repositories`: команду собирают из отдельно
запиненного внешнего Git-репозитория, а не из корня сборки внутри скилла.
Пакет скилла объявляет каноничный сетевой источник, точный объект и
(опционально) точный тег; выбор кредов, тулчейна, вывода и подписи остаётся
за оператором и менеджером.

```json
{
  "schema_version": 7,
  "capabilities": {
    "network": ["gitlab.example.com"],
    "filesystem": "repo",
    "exec": "none",
    "secrets": "none",
    "env_read": [],
    "prompt_scope": "Read-only documentation lookups."
  },
  "build_repositories": {
    "tool-cli": {
      "git": "git@gitlab.example.com:group/tool-cli.git",
      "locked_commit": {
        "object_format": "sha1",
        "hex": "0123456789abcdef0123456789abcdef01234567"
      },
      "tag": "v1.2.0"
    }
  },
  "commands": {
    "tool": {
      "type": "build",
      "driver": "go-repository-v1",
      "repository": "tool-cli",
      "target": "tool"
    }
  }
}
```

Внешний репозиторий обязан содержать в корне закрытый дескриптор
`skill-build.json`:

```json
{
  "schema_version": 1,
  "targets": {
    "tool": {
      "driver": "go-repository-v1",
      "build_root": ".",
      "source_dir": "cmd/tool"
    }
  }
}
```

`build_root` содержит `go.mod`; модули вендорятся (`go mod vendor`), сборка
идёт без сети. Скомпилированный артефакт никогда не коммитится ни в скилл,
ни во внешний репозиторий. Поле `build_repositories.*.git` принимает обе формы
адреса: `git@host:path.git` и `https://host/path.git`. Приватный репозиторий
требует явного выбора учётных данных оператором через скоупы `build_ssh` для SSH
и `build_https` для HTTPS. Выбор транспорта не определяет доступность установки:
SSH требует SSH-ключ, HTTPS требует сохранённые учётные данные Git или токен.
Адрес HTTPS в манифесте обязан содержать суффикс `.git`. Без суффикса `.git`
сервис GitLab отвечает 301, fetch выполняется с `http.followRedirects=false`, и
установка завершается ошибкой `build_repository_source_unavailable`. Полный
контракт работы с внешними репозиториями описан в
`docs/external-build-repositories.md`. `go-repository-v1` поддерживается только
на macOS и Windows.

### Схема v8: корни first-party модулей и политика выполнения script-команд

Схема v8 добавляет два необязательных поля.

Локальная команда `go-v1` объявляет `modules`: список каталогов first-party
Go-модулей того же пакета, которые корень сборки заменяет через `replace`.
Отсутствующий или пустой список сохраняет смысл схем v6 и v7, то есть корень
сборки из одного модуля.

```json
{
  "schema_version": 8,
  "capabilities": {},
  "build_roots": ["tools/cli"],
  "commands": {
    "cli": {
      "type": "build",
      "driver": "go-v1",
      "source_dir": "tools/cli/cmd/cli",
      "modules": ["pkg/board", "pkg/remoteconfig"]
    }
  }
}
```

Пакет заявляет, менеджер проверяет. Каждый объявленный каталог обязан быть
портируемым относительным путём, отличным от `.`, реальным каталогом без
ссылок внутри снапшота, содержать `go.mod` прямо внутри и не пересекаться ни с
другим объявленным модулем, ни с корнем сборки, ни с runtime-корнем. Проверка
идёт до фиксированного `go list`.

Эффективный набор `replace` менеджер читает только из
`<корень сборки>/vendor/modules.txt` после `go list` и до `go build`.
Допускается лишь форма "каталог": по одному токену с каждой стороны.
Версия слева, версия справа и redirect с модуля на модуль отклоняются.
Объявленные каталоги и эффективные директивы обязаны находиться во взаимно
однозначном соответствии: директива без объявления отклоняется как
`build_module_root_directive_undeclared`, объявление без директивы как
`build_module_root_declaration_unused`.

Объявленные каталоги входят в поверхность сканирования вместе со своими
vendor-копиями, а послабление для аудированного стороннего vendor-кода на
заменённые модули не распространяется.

Script-команда может выбрать закрытую политику выполнения парой
`execution_policy` + `interpreter`:

```json
{
  "type": "script",
  "unix_path": "scripts/tool",
  "execution_policy": "script-worker-v1",
  "interpreter": "python3-v1"
}
```

Поля обязательны друг для друга; единственное допустимое значение
`execution_policy` это `script-worker-v1`, допустимые интерпретаторы это
`python3-v1` и `node-v1`. csk не реализует `script-worker-v1`, поэтому
отклоняет такую команду с `script_execution_policy_unsupported` на установке и
в `csk check`. Команда не понижается до declared-only shim: опубликованный
лаунчер выполнял бы код пакета, который манифест объявляет изолированным.

Установка схемы v8 записывает install marker schema 4. Форма и семантика
записей сборки те же, что у marker v3.


## 4. Runtime-корни

Секция `runtime_roots` перечисляет каталоги только для runtime. CocoaSkills копирует их в глобальное хранилище runtime и исключает из установленного контекста промпта.

Правила:

- Поле `runtime_roots` необязательно. Значение по умолчанию: `[]`.
- Каждый корень представляет собой относительный путь POSIX внутри репозитория скилла.
- Запрещен начальный `/`.
- Запрещен элемент `..`.
- Запрещены пустые компоненты пути.
- Корень должен существовать.
- Корень должен быть каталогом.
- Корни должны оставаться уникальными после удаления замыкающих слэшей.
- Корни не должны пересекаться: список `["scripts", "scripts/lib"]` некорректен.
- Сравнение выполняется с учетом регистра.

Корректный пример:

```json
{
  "schema_version": 2,
  "runtime_roots": ["scripts"]
}
```

Некорректные примеры (три независимых фрагмента):

```text
{"runtime_roots": ["/scripts"]}
{"runtime_roots": ["../scripts"]}
{"runtime_roots": ["scripts", "scripts/lib"]}
```

## 5. Корни сборки и скомпилированные команды

Подробное руководство по поставке компилируемых и скриптовых CLI-утилит смотрите в документе [Поставка CLI-утилит](authoring-cli-commands.md).

Скиллы схемы 7 могут также выбирать зафиксированный внешний источник Git с закрытой формой команд `go-repository-v1`. Пакет объявляет идентичность и точный commit; пакет не может передавать вспомогательные скрипты Git, учетные данные, хуки, аргументы сборки, переменные окружения, пути вывода или настройки подписи. Внешний репозиторий содержит отдельный дескриптор `skill-build.json`. Смотрите [Внешние репозитории сборки](external-build-repositories.md) для полных примеров и правил жизненного цикла. Этот режим с поддержкой исходного кода работает на macOS и Windows; Linux не входит в квалифицированный набор платформ.

Поле `build_roots` задает каталоги только с исходным кодом. csk проверяет и хэширует их, но не копирует в установленный контекст промпта или хранилище runtime скриптов.

Правила корней сборки:

- Поле `build_roots` требует схему 6. Значение по умолчанию: `[]`.
- Каждый корень представляет собой относительный путь POSIX без точечных элементов к реальному каталогу без ссылок в снапшоте скилла.
- Корни уникальны, не пересекаются и не могут содержать записи из `runtime_roots` или содержаться в них.
- Каждый корень используется хотя бы одной активной командой `build`.
- Каталог `source_dir` команды сборки расположен внутри ровно одного корня сборки и не содержит ссылок. Каталог может совпадать с корнем.
- Содержащий корень сборки содержит `go.mod` напрямую. Никакой промежуточный каталог между корнем и `source_dir` не может содержать другой `go.mod`.

Объект команды закрыт:

```json
{
  "type": "build",
  "driver": "go-v1",
  "source_dir": "build/cmd/repo-report"
}
```

Спецификация разрешает только поля `type`, `driver` и `source_dir`. Авторы не могут настраивать программу, произвольные аргументы, переменные окружения, теги или флаги сборки, тулчейн, целевую платформу, путь вывода, скрипты, хуки, плагины, генераторы, действия после сборки или аргументы команды. Драйвер `go-v1` остается единственным допустимым драйвером. Любой другой драйвер или лишнее поле вызывают отказ вместо отката к локальной сборке. Этот отказ предотвращает выбор нереализованного контракта выполнения или подмену артефактов под другими семантиками кэша. Единственный путь артефакта вычисляется из имени экспортируемой команды: `bin/<command>` на Unix и `bin/<command>.exe` на Windows.

### Требования к исходному коду Go

Протокол разрешает драйвер Go версии 1.23 или новее и требует от менеджера проверять семейство тулчейна по векторам `go-v1`. `csk` квалифицирует семейства 1.26 (от 1.26.10) и 1.27 (от 1.27.3); семейства 1.25 и старше отклоняются с `unsupported_go_family`. По решению `csk` более новые, ещё не квалифицированные семейства принимаются с предупреждением `untested_go_family` при сохранении полного lockdown `go-v1`; это отклонение от текущего требования протокола, и предложение об изменении внесено в [curator-spec issue #101](https://github.com/relux-works/curator-spec/issues/101) и описано в [`UNRESOLVED_QUESTIONS.md`](../UNRESOLVED_QUESTIONS.md). Оператор может отключить оптимистичное принятие через `builds.go_future_families: "refuse"` в `~/.cocoaskills/config.json` или переменную `CSK_GO_FUTURE_FAMILIES=refuse`.

Релизы 1.26 ниже 1.26.10 и 1.27 ниже 1.27.3, а также prerelease и другие неподтверждённые версии этих семейств отклоняются с `insecure_go_release` до сборки и чтения кэша. Сообщение называет версию, путь к Go и порог семейства. Строка `remediation:` предлагает Go 1.27.3 или новее первым в `PATH`: например, `go = "1.27.3"` в проектном `.mise.toml` либо `brew upgrade go`. Порог нужен для исправленной проверки TLS-сертификатов: стандартная библиотека Go встраивается в CLI. Смена версии меняет cache key, и следующая `csk install` или `csk upgrade` пересобирает CLI.

Shim Go из goenv, asdf или mise-shim разрешается ровно одним вызовом `env GOROOT` в приватной bootstrap-среде. Затем csk проверяет структурные признаки GOROOT и fingerprint-ит `<GOROOT>/bin/go`; некорректный ответ вызывает `toolchain_shim_unresolved`. На Windows `.cmd` и `.bat` shim отклоняются, поскольку direct-process probe runner не запускает batch-файлы безопасно.

Драйвер `go-v1` собирает один нативный исполняемый файл для хостовых `GOOS`/`GOARCH`. Объявленный каталог исходного кода содержит ровно один нетестовый `package main`. Нестандартные импорты разрешаются из закоммиченных вендорных файлов; csk выполняет фиксированные команды `-mod=vendor` с отключенным сетевым доступом для зависимостей. Входные данные компилятора из стандартной библиотеки поступают только из проверенного `GOROOT`, а остальные активные входные данные находятся строго внутри корня сборки.

Драйвер отключает телеметрию Go в приватном состоянии и фиксирует приватные конфигурации, кэш, временные каталоги и каталоги вывода. Драйвер фиксирует `GOTOOLCHAIN=local`, `GOENV=off`, `GOWORK=off`, `CGO_ENABLED=0`, `GO_EXTLINK_ENABLED=0`, компилятор gc, отключение PGO, нативную целевую платформу и внутреннюю линковку без libgcc. Следующие возможности пакетов запрещены:

- режим workspace или переключение тулчейна;
- кросс-компиляция, cgo, PGO, внешняя линковка или откат к libgcc;
- генераторы, тесты, плагины или оверлеи;
- выбираемые пакетом объектные файлы хоста;
- шелл, команды VCS, скачивание модулей, дополнительные программы, хуки или действия после сборки.

Комментарии генераторов и пути PGO остаются инертными байтами. Они не дают csk права запускать процессы.

Четыре узких исключения для вендоринга позволяют собирать проверенные реальные зависимости без расширения границ безопасности (решение `curator-spec` 0005):

- Пакеты `GOROOT/src/vendor` с признаками `Standard`, `Goroot`, пустым `Root` и префиксом импорта `vendor/` остаются доверенными, так как их каталог зафиксирован под проверенным `GOROOT`.
- Ассемблер Go (`SFiles`) допускается только для вендорных пакетов без cgo, C, C++, Objective-C, Fortran, SWIG или объектных файлов хоста, где каждая запись `SFiles` является обычным файлом внутри корня сборки, хэшируемым `curator-build-source-v1` (например, маски `coder/websocket`).
- Директива `//go:cgo_import_dynamic` отвергается во всех нестандартных записях `GoFiles`, кроме проверенного модуля `golang.org/x/sys` и его подпакетов (трамплины системных вызовов `zsyscall`).
- Директива `//go:generate` инертна в вендорных записях `GoFiles`. Ни `go list -mod=vendor`, ни `go build -mod=vendor` не запускают генераторы, вендор уже материализован, а csk не вызывает `go generate`, поэтому комментарий не вызовет ошибку предварительной проверки. За пределами `vendor/` директива отвергается как активный генератор.

Режим `go-v1` с поддержкой исходного кода работает на macOS и Windows. На любой другой хост-системе режим завершается ошибкой до запуска воркера или процессов Go. Поддержка Linux вынесена в отдельные задачи `TASK-260728-1skseh` и `TASK-260728-1e6811`; это не меняет поддержку Linux для скриптовых и системных скиллов.

### Выполнение под управлением менеджера

Протокольная идентичность `manager-worker-v1` зафиксирована и не является полем манифеста, настройкой оператора или опциональным песочным окружением. Идентичность служит нормативным входом для кэша, квитка, актуальности маркера и утверждений. Дерево процессов содержит только следующие элементы:

```text
manager parent
  -> identity-verified manager-owned worker
       -> fingerprinted <GOROOT>/bin/go
            -> fingerprinted regular children below <GOROOT>/pkg/tool/
```

Менеджер проверяет воркер перед запуском; воркер доказывает подлинность с помощью свежего nonce. Сессия выполняет один фиксированный вызов `go list`, приостанавливается на время проверки родительским процессом полного графа пакетов, получает один аутентифицированный пропуск на сборку и выполняет один фиксированный вызов `go build`. Повторные попытки, лишние сообщения, исполняемые файлы, скачивания, генераторы, тесты или запросы на запуск уничтожают домен воркера. Снапшот исходного кода, воркер и тулчейн Go повторно проверяются после выполнения; весь домен воркера завершается и присоединяется перед публикацией.

Менеджер ограничивает одну операцию лимитами: 120 секунд времени, 8 МиБ суммарного вывода, 128 МиБ на артефакт, 512 МиБ на файл, 1 ГиБ приватного хранилища сборки, 2 ГиБ памяти и 64 активных процесса. Средства ограничения файлов, памяти и процессов зависят от платформы:

| Элемент управления `rc5-native-control-inventory-v1` | macOS | Windows |
|---|---|---|
| `descendant-domain-termination` | доступно: завершение группы процессов/сессии | доступно: завершение Job Object при закрытии |
| `active-process-count-limit` | недоступно | доступно: лимит Job Object |
| `aggregate-memory-limit` | недоступно | доступно: лимиты памяти процесса/job в Job Object |
| `per-file-size-limit` | доступно: `RLIMIT_FSIZE` | недоступно |
| `inherited-handle-restriction` | доступно: close-on-exec плюс освобождение дескрипторов | доступно: явный список наследования хэндлов |

Каждая операция выполнения возвращает один закрытый документ `capability-evidence-v1` с ровно одной записью на каждый элемент inventory control, проверенный перед запуском воркера. Недоступные элементы управления фиксируются как недоступные и не отменяют сборку. Если обязательный портативный элемент управления невозможно применить, csk возвращает ошибку `build_execution_control_unavailable` до старта воркера или Go и ничего не публикует.

Документ capability evidence содержит только результаты: он не является ключом кэша, квитком, маркером, входом актуальности или входом для утверждений. Портативная политика также не обеспечивает и не декларирует `total-network-denial`, `read-only-source-and-toolchain`, `private-build-root-only-writes`, `hard-aggregate-descendant-resource-bounds`, `exact-executable-allowlisting` или `fail-closed-capability-preflight`. Это отложенные усиленные гарантии. Например, фиксированное офлайн-поведение Go не является сетевой изоляцией ядра, а ограничения менеджера не являются жесткими агрегированными лимитами потомков.

Менеджер проверяет, но никогда не выполняет только что скомпилированный артефакт во время проверки, установки, dry-run, status, repair, rollback или GC. Артефакт становится исполняемым для пользователя или агента только через активированный лаунчер команд.

### Жизненный цикл оператора для тестирования авторами

Команда dry-run проверяет и хэширует зафиксированный исходный код сборки, определяет доверенный тулчейн и нативную целевую платформу, а также проверяет состояние кэша без вызова `go list`, `go build`, компилятора или линкера. Операция не создает постоянный кэш, снапшот, блокировки изменений или журнал транзакций. Результаты по командам включают `cache-hit`, `would-preflight-and-build`, `would-rebuild-untrusted-cache`, `corrupt` и `unsupported`.

Реальная установка в проект, глобальная установка или гибридная установка выполняют проверку и аудит перед сборкой. Промахи кэша компилируются в приватном временном каталоге операции; публикация в защищенном кэше и транзакция контекста/runtime/shim/адаптера/маркера происходят под блокировкой home менеджера. Сбой откатывает предыдущую установку. Безопасно опубликованная запись без ссылок может остаться для GC под блокировкой. GC удаляет проверенные записи старше 24 часов и сохраняет неопределенные состояния.

Команды `csk status --json --check` и `csk global status --json --check` проверяют дескриптор, идентичность исходников, целевую платформу, тулчейн, политику выполнения, ключ, защищенный квиток/артефакт, маркер и shim без их изменения. Флаг `--check` завершается с кодом 1 при неактуальной сборке. Восстановление выполняется обычной переустановкой: на поддерживаемой платформе `csk install` или `csk global install` пересобирает отсутствующие, поврежденные, несоответствующие, устаревшие или недоверенные кандидаты в новое защищенное состояние без применения их байтов. Неподдерживаемая платформа сохраняет состояние сбоя.

Не приравнивайте установленный `content_sha256` к `curator-build-source-v1`: только последний охватывает полный проверенный исходный код, включая файлы только для сборки. Не считайте согласованный квиток защищенной provenance; повторное использование требует также независимо проверенного менеджером владения, разрешений/DACL, изоляции, типа файла и границ ссылок. Физические пути `<csk-home>/builds/go-v1/...` специфичны для csk; портативными являются только логические входы, канонические байты квитка, путь относительно артефакта и байты/хэш/размер артефакта.

## 6. Скриптовые команды

Скриптовые команды предоставляют исполняемые файлы скилла через проектный каталог `<project>/.agents/bin`.

Пример:

```json
{
  "schema_version": 2,
  "runtime_roots": ["scripts"],
  "commands": {
    "mr": {
      "type": "script",
      "unix_path": "scripts/mr",
      "win_path": "scripts/gmr.cmd"
    }
  }
}
```

Правила для скриптов схемы v2 и новее:

- Разрешенные поля: `type`, `unix_path`, `win_path`.
- Требуется минимум один путь для платформы.
- Пути должны быть относительными путями POSIX.
- Пути не должны содержать `..`.
- При непустом `runtime_roots` каждый путь скрипта должен находиться внутри одного из этих корней.
- Путь должен существовать и указывать на файл.

### Разрешение команд для агента

Каталоги `runtime_roots` намеренно отсутствуют в установленном контексте промпта. Файл `SKILL.md` и ссылки в промпте не должны предполагать существование путей манифеста (таких как `scripts/tool`) рядом с установленным скиллом. Тот же резолвер применяется к командам сборки схемы 6: их артефакт остается в защищенном кэше и не присутствует рядом с `SKILL.md`.

Определите плейсхолдеры (например, `<tool-command>`) и разрешайте каждую экспортируемую команду один раз перед первым вызовом:

1. При наличии `.csk-install.json` рядом с `SKILL.md` ищите вверх от текущего рабочего каталога, затем от физического пути `SKILL.md`, ближайший файл `<ancestor>/.agents/bin/<command>` (`.cmd` на Windows).
2. При отсутствии проектного shim используйте `<csk-home>/global/bin/<command>` (`<command>.cmd` на Windows), где `<csk-home>` по умолчанию является родителем `CSK_CONFIG` или `~/.cocoaskills`.
3. Используйте имя команды без пути только как конечный запасной вариант после подтверждения существования через `command -v` или `Get-Command`.
4. При отсутствии `.csk-install.json` считайте скилл рабочей копией исходников. Для скриптовой команды прочитайте точку входа платформы из `agent-skill.json` и разрешите путь относительно физического каталога скилла. Команда сборки не имеет исполняемого файла в дереве исходников: требуйте установленного csk shim или отдельно задокументированной команды только для человека (без выполнения собственного исходного каталога).
5. Если ни одна объявленная команда не найдена, сообщите о неполной установке и остановите работу. Не угадывайте путь runtime и не выполняйте файлы относительно текущего рабочего каталога.

Поиск из рабочего каталога и из физического пути скилла важен: адаптеры агентов могут быть симлинками или копиями, а проектные команды должны перекрывать глобальные. Не вычисляйте команду как фиксированное число компонентов `..` от пути `SKILL.md`, видимого адаптеру.

Применяйте это же правило к скиллам-workflow, использующим команды из `dependencies.skills` или устаревших записей `dependencies.commands`. Их собственный `runtime_roots` может быть пустым, но пути к исходникам провайдеров недоступны после установки.

Активация шелла не является пререквизитом для выполнения агентом. Авторы должны сохранять явный проектный или глобальный резолвер, даже если интерактивный шелл уже предоставляет команду через `PATH`. `csk skill check` выдает предупреждение, когда управляемой команде не хватает этого независимого от оболочки контракта.

Шаблон резолва команд в `SKILL.md` (гасит предупреждение `skill.command_resolution_contract_missing`):

```markdown
Резолвь экспортированное имя `<tool>` один раз до первого вызова:

1. Вверх от текущей директории, затем от физического пути `SKILL.md`: ближайший `<tool>` в `.agents/bin` (управляемые шимы на Windows несут суффикс `.cmd`: `<tool>.cmd`).
2. Иначе `<tool>` в `global/bin` под домом CocoaSkills (родитель `CSK_CONFIG`, по умолчанию `~/.cocoaskills`), с тем же `.cmd` на Windows.
3. Иначе голое имя `<tool>`: только после валидации `command -v <tool>` в POSIX-шеллах, `Get-Command <tool>` в PowerShell.

Если лаунчер не найден, установка неполна: сообщи об этом и остановись; не выкачивай и не собирай CLI ад-хок.
```

На Unix установленный скомпилированный лаунчер напрямую вызывает `exec` для абсолютного пути к защищенному артефакту и передает аргументы. На Windows лаунчер `<command>.cmd` вызывает абсолютный `.exe`, передает аргументы и сохраняет код выхода. Авторам не следует добавлять второй платформенный оберточный скрипт вокруг этого лаунчера.

`csk skill check` выдает предупреждение, когда доступный промпту Markdown ссылается на корень только для runtime или угадывает исходный runtime провайдера. Команды разработки для человека следует размещать в `README.md`, который не копируется в контекст промпта.

Точки входа команд должны определять собственный каталог перед загрузкой соседних файлов. Для скриптов POSIX shell:

```bash
#!/usr/bin/env bash
set -euo pipefail

source_path="${BASH_SOURCE[0]}"
while [[ -L "$source_path" ]]; do
  target_path="$(readlink "$source_path")"
  if [[ "$target_path" == /* ]]; then
    source_path="$target_path"
  else
    source_path="$(cd -P -- "$(dirname -- "$source_path")" && pwd)/$target_path"
  fi
done

script_dir="$(cd -P -- "$(dirname -- "$source_path")" && pwd)"
exec python3 "$script_dir/main.py" "$@"
```

Этот шаблон работает, когда `.agents/bin/<command>` проекта является симлинком на хранилище runtime.

## 7. Зависимости

Секция `commands` предназначена только для команд, экспортируемых текущим скиллом. Зависимости размещаются в `dependencies.commands`.

Не объявляйте команду в `commands` только потому, что скилл вызывает ее. Это превращает имя команды в экспортируемую команду CocoaSkills и может вызвать конфликт со скиллом, предоставляющим ее.

### Зависимости от системных команд

Зависимости от системных команд представляют собой команды, необходимые скиллу, но не принадлежащие ему и устанавливаемые в систему или проект инструментами подготовки проекта.

Пример:

```json
{
  "schema_version": 2,
  "dependencies": {
    "commands": {
      "review-cli": {
        "type": "system",
        "command": "review-cli",
        "hint": "Install the review CLI through project bootstrap tooling"
      }
    }
  }
}
```

Правила:

- Разрешенные поля: `type`, `command`, `hint`.
- Поле `command` обязательно.
- Поле `hint` необязательно.
- CocoaSkills проверяет наличие с помощью `shutil.which(command)`.
- CocoaSkills никогда не устанавливает системные зависимости.
- CocoaSkills никогда не выполняет проверки из манифеста.

Запрещенные поля:

- `install`
- `check`
- `post_install`
- `script`
- `command_args`

При отсутствии системной зависимости `csk install` завершается с ошибкой до записи файлов runtime, контекста проекта или shims для этого скилла.

Инструменты подготовки проекта управляют системными зависимостями. В demo-ios это Mise, Make или скрипт подготовки проекта, но не менеджер скиллов.

### Зависимости от команд скиллов

Зависимости от команд скиллов представляют собой команды, экспортируемые другим скиллом в том же `Skillfile.json`.

Пример:

```json
{
  "schema_version": 2,
  "dependencies": {
    "commands": {
      "wk": {
        "type": "skill",
        "skill": "skill-docs",
        "command": "wk",
        "hint": "Add skill-docs to Skillfile.json before this skill."
      }
    }
  }
}
```

Правила:

- Разрешенные поля: `type`, `skill`, `command`, `hint`.
- Поле `skill` задает имя скилла-провайдера из `Skillfile.json`. Потребители должны объявлять провайдера под этим каноническим именем.
- Поле `command` задает скриптовую команду, экспортируемую скиллом-провайдером.
- Ключ словаря `dependencies.commands` служит локальным идентификатором зависимости для маркеров и диагностики. Для зависимостей от команд скиллов сохраняйте его равным `command`, если зависимости не требуется отдельное локальное имя.
- Поле `hint` необязательно.
- Скилл-провайдер должен находиться в том же плане установки.
- Провайдер должен экспортировать запрашиваемую команду как команду `script`.
- Зависимости от команд скиллов не устанавливаются как новые shims вызывающим скиллом и не участвуют в обнаружении конфликтов команд.

Корректный пример:

```json
{
  "schema_version": 2,
  "commands": {},
  "dependencies": {
    "commands": {
      "wk": {
        "type": "skill",
        "skill": "skill-docs",
        "command": "wk"
      }
    }
  }
}
```

Некорректный пример:

```json
{
  "schema_version": 2,
  "commands": {
    "review-cli": {
      "type": "system",
      "command": "review-cli",
      "hint": "This is a dependency, not an export."
    }
  }
}
```

Тип `type: system` в секции `commands` остается допустимой формой совместимости (включая схему 6), но не является экспортируемым лаунчером. csk только проверяет предоставленную оператором команду через `shutil.which`. Новые скиллы размещают используемый системный инструмент в секции `dependencies.commands`.

## 8. Контракт локализации

Локализация необязательна. Если скилл не содержит `locales/metadata.json` и `.skill_triggers/`, установка выполняется одинаково независимо от настройки `locale` проекта.

При наличии одного из указанных элементов локализация считается согласованной только при присутствии в обоих местах:

- `locales/metadata.json` с объектом `locales.<locale>` (поле `description` заменяет описание из frontmatter `SKILL.md`);
- `.skill_triggers/<locale>.md` с каталогом триггеров для этой локали.

При наличии локализации требуется минимум одна согласованная локаль. Если выбранная локаль отсутствует, но другая локаль согласована, CocoaSkills устанавливает исходный `SKILL.md` с предупреждением вместо ошибки.

## 9. Проверка скилла

Используйте `csk skill check` перед созданием тега скилла:

```bash
csk skill check .
csk skill check . --locale ru
csk skill check . --json
```

Команда проверяет внутренние требования к скиллу в рабочей копии: `SKILL.md`, `agent-skill.json`, корни runtime и сборки, форму команд, корректность Go-модуля и согласованность каталогов локализаций. Команда также предупреждает, если доступный промпту Markdown указывает на каталог только для runtime или сборки, который будет отсутствовать после установки. Команда не требует `~/.cocoaskills/config.json`, `Skillfile.json`, настройки проекта или тулчейн Go и не выполняет компиляцию.

Команда `csk skill check` читает рабочую копию как есть. `csk install` проверяет закоммиченный снапшот git из `Skillfile.json` проекта, поэтому незакоммиченные файлы могут вызывать различия между командами.

Наличие системных команд зависит от окружения и проверяется при установке. Команда `csk skill check` проверяет правильность объявления записей в `dependencies.commands`, но не требует наличия системных команд на машине автора.

Каталоги локалей корректны, когда минимум одна локаль присутствует и в `locales/metadata.json`, и в `.skill_triggers/<locale>.md`. Если выбранная локаль отсутствует, но другая локаль согласована, CocoaSkills устанавливает исходный `SKILL.md` с предупреждением вместо сбоя.

Не добавляйте `dependencies.json`. CocoaSkills больше не копирует этот файл. Зависимости указываются в `agent-skill.json` в секции `dependencies.commands`.

## 10. Контракт контекста промпта

Размещайте предназначенные для агента файлы в корнях контекста промпта:

- `SKILL.md`
- `agents/`
- `references/`
- `.skill_triggers/`
- `assets/`
- `templates/`
- `examples/`
- `data/`

Размещайте код только для runtime в `runtime_roots` (обычно `scripts/`). Скомпилированные исходники размещайте в `build_roots` схемы 6; эти каталоги отсутствуют в контексте промпта и не должны пересекаться с корнями runtime.

Одно исключение для совместимости: скилл без объявления команд копирует каталог `scripts/` в контекст промпта, так как ничто не помечает эти файлы как относящиеся только к runtime. Объявляйте команды (или `runtime_roots` в схеме v2) для исключения скриптов из контекста агента.

Не рассчитывайте на копирование в контекст промпта следующих файлов:

- `README*`
- `CHANGELOG*`
- `LICENSE*`
- `tests/`
- `.github/`
- `.gitlab-ci.yml`
- `.venv/`
- `node_modules/`
- `pyproject.toml`
- `requirements*.txt`
- `Makefile`

Помещайте оперативную информацию для агента в `SKILL.md` или `references/`, а не в `README.md`.

## 11. Примеры манифестов скиллов

### skill-tracker

```json
{
  "schema_version": 2,
  "runtime_roots": ["scripts"],
  "commands": {
    "trk": {
      "type": "script",
      "unix_path": "scripts/trk"
    },
    "trkx": {
      "type": "script",
      "unix_path": "scripts/trkx"
    }
  }
}
```

### skill-review

```json
{
  "schema_version": 2,
  "runtime_roots": ["scripts"],
  "commands": {
    "mr": {
      "type": "script",
      "unix_path": "scripts/mr"
    }
  },
  "dependencies": {
    "commands": {
      "review-cli": {
        "type": "system",
        "command": "review-cli",
        "hint": "Install the review CLI through project bootstrap tooling"
      }
    }
  }
}
```

### skill-monitor

```json
{
  "schema_version": 2,
  "runtime_roots": ["scripts"],
  "commands": {
    "monitor-api": {
      "type": "script",
      "unix_path": "scripts/monitor-api"
    },
    "monitor-cli-auth": {
      "type": "script",
      "unix_path": "scripts/monitor-cli-auth"
    }
  },
  "dependencies": {
    "commands": {
      "monitor-cli": {
        "type": "system",
        "command": "monitor-cli",
        "hint": "Install the monitor CLI through project bootstrap tooling"
      }
    }
  }
}
```

## 12. Глобальная и проектная установка

Авторам скиллов не требуется отдельный манифест для глобального использования. Те же файлы `SKILL.md` и `agent-skill.json` действуют при установке:

- в проект через `csk install`;
- глобально через `csk global install`.

Глобальная установка меняет только целевой каталог:

```text
project scope: <project>/.agents/skills/<skill>/
global scope:  ~/.cocoaskills/global/skills/<skill>/
runtime:       ~/.cocoaskills/runtime/<skill>/<commit>/
compiled:      <csk-home>/builds/go-v1/<64-hex-key>/
```

Хранилища runtime и скомпилированных артефактов являются общими. Проект может выбирать другой commit того же скилла. Проектные команды и адаптеры агентов перекрывают глобальные внутри рабочей копии. Скомпилированные артефакты остаются в защищенном кэше и не копируются ни в одну область видимости.

Не создавайте зависимость скилла от глобальной установки. Объявления в проектном `Skillfile.json` остаются единственным источником правды для поведения проекта.

## 13. Чек-лист перед релизом

Перед созданием тега релиза скилла:

1. Проверьте рабочую копию командой `csk skill check .` (и `csk skill check . --locale <locale>`): `csk skill check` завершается кодом 0 и при предупреждениях, поэтому проверяйте вывод на отсутствие ошибок и предупреждений, а не код возврата.
2. Проверьте `agent-skill.json`, запустив `csk install` в реальном или тестовом проекте.
3. Для схемы 6 убедитесь, что каждый корень сборки используется, не пересекается с корнями runtime, содержит ближайший прямой `go.mod` и отсутствует в `.agents/skills/<skill>/`.
4. Убедитесь в отсутствии корней runtime в `.agents/skills/<skill>/`.
5. Убедитесь в наличии файлов runtime в `~/.cocoaskills/runtime/<skill>/<commit>/`.
6. Убедитесь в доступности команд проекта через `.agents/bin`.
7. Для скомпилированных команд проверьте `csk install --dry-run`, выполните реальную установку с доверенным семейством Go на поддерживаемом хосте и убедитесь, что `csk status --json --check` сообщает об актуальности сборки.
8. Убедитесь в работоспособности разрешения команд, когда `.agents/bin` отсутствует в `PATH`, включая режим скопированного адаптера.
9. Убедитесь, что `SKILL.md` и доступные промпту ссылки не содержат путей к исполняемым файлам в корнях runtime или сборки.
10. Убедитесь, что отсутствие системных зависимостей вызывает сбой с понятной подсказкой.
11. Убедитесь, что `SKILL.md` и `references/` содержат все инструкции для агента.
12. Создайте тег в репозитории скилла.
13. Обновите `Skillfile.json` в проекте-потребителе до нового тега.
14. Выполните `csk install` и `csk status --check`.

## 14. Заметки по миграции

Для существующих скиллов с `csk-skill.json`:

1. Переименуйте файл в `agent-skill.json` без изменения значений JSON.
2. Обновите документацию и автоматизацию для использования только `agent-skill.json`.
3. Если при поэтапном внедрении временно требуются оба файла, сохраняйте равенство декодированных значений JSON. CocoaSkills отвергает расхождение ошибкой `conflicting_skill_manifests`.

Устаревшее имя файла поддерживается во всем протоколе 1.x, поэтому потребителям не требуется одновременный переход.

Для существующих скиллов с `agents/runtime.json`:

1. Добавьте `agent-skill.json` схемы v2.
2. Сохраните `agents/runtime.json` во время первого внедрения.
3. Выпустите новый тег и подключите его в реальном проекте.
4. После проверки удалите устаревший файл `agents/runtime.json`.

Файл `agents/runtime.json` читается только при отсутствии `agent-skill.json` и устаревшего `csk-skill.json`.


### Local ignore policy and legacy Skillfiles

Ignore placement is local policy, not a portable Skillfile member. In 0.21.x,
a legacy top-level `ignore_mode` field still determines the effective mode while
it is present. Once absent, a managed CocoaSkill block in the project `.gitignore`
selects `gitignore`; otherwise `$GIT_COMMON_DIR/info/exclude` is used. Inspect the
effective mode and its source with `csk ignore-mode show`: deprecated field,
managed block, or the default when neither block exists. Use `csk ignore-mode set exclude` or
`csk ignore-mode set gitignore` to switch explicitly. These commands preserve
user bytes outside the managed blocks and remove only the legacy top-level
`ignore_mode` member, in the same recoverable transaction. No schema version
change or stored replacement setting is needed. Symlinked policy files, tracked
owned paths, ineffective rules, duplicate keys and unsafe edits refuse unchanged.

Top-level `ignore_mode` and Cursor in `server.environments` are input-only
compatibility forms in 0.21.x. Ordinary Skillfile writers refuse either with
`skillfile_deprecated_form`, naming `csk ignore-mode set exclude` (or `gitignore`)
and/or `csk mcp migrate`. Each migration removes only its own form and preserves
the other byte-for-byte, so either order works. Every load warns on stderr,
once per form per command; JSON stdout is unaffected, and a warning alone is not
status drift. A Skillfile is portable only after migration of both forms;
0.22 stops reading them. This does not promise cross-manager installation of
every declaration or implementation-local option.
