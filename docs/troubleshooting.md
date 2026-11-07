# Диагностика установки

## `git_trust_anchor_distrusted` / `git_trust_anchor_invalid`

`git_trust_anchor_distrusted` — предупреждение: csk удалил перечисленные CA
из выбранного bundle и продолжает работу с оставшимися PEM-сертификатами.
Оно появляется один раз за процесс, указывает имена удалённых CA и исходный путь.
`git_trust_anchor_invalid` останавливает операцию до запуска git и передачи
токена: файл нечитаем, повреждён, содержит мусор вне PEM или не содержит
сертификатов после фильтрации. Лишние байты внутри DER также недопустимы.
На macOS установите или обновите пакет `ca-certificates` через Homebrew:

```bash
brew install ca-certificates
```

На Linux обновите пакет CA-сертификатов вашего дистрибутива. Для отдельного
актуального PEM-файла задайте абсолютный путь через `CSK_GIT_CA_BUNDLE`
или `git.https_ca_bundle` в конфигурации csk. Файл должен быть читаемым,
обычным, содержать корректные сертификаты и занимать не более 16 MiB.
Если первый найденный системный bundle повреждён, csk откажет, даже когда
следующий существует. После обновления повторите исходную команду.
Не отключайте проверку TLS. Настройки `git config --global http.sslCAInfo`
недостаточно для изолированного транспорта csk; [порядок выбора](reference.md#https-токены-внешних-репозиториев-сборки)
описан в справочнике. Проверка выполняется также перед SSH и локальными
командами Git, поэтому непригодный override нужно исправить и для них.

Здесь описаны ошибки MCP-конфигурации проекта, обновления с ранних версий csk (<=0.12) и работы с компилируемыми командами.

## MCP first run: check, ignore и consent

Начните с `csk init`, затем `csk check` и `csk install`. Schema 3 проходит тот же MCP-валидатор, что install; `check` ничего не записывает и не проверяет auth/connectivity. Install сам добавляет точные ignore entries для создаваемых им MCP configs с локальной git policy. Deprecated `--fix-gitignore` для них не нужен; в режиме `exclude` обычные generated paths также добавляются автоматически. Для режима `gitignore` используйте `csk init`.

`gitignore_required` в MCP-строке указывает на общий ignore preflight, а `blocked by project_preflight_failed` — на общую ошибку без отдельного кода: смотрите следующую диагностику проекта. Это не ошибка MCP declaration. Если согласия требует соседний сервер, сервер без такой потребности получает `blocked by mcp_plan_unapproved`.

`mcp_command_invalid` also refuses any runner/evaluator token inside `args`, including unknown wrappers such as `find . -exec npx pkg ;`. Matching ignores path, case and executable suffixes. Even an intended data argument such as `--tool node` refuses; the diagnostic names the token. Use a direct bare executable with ordinary arguments, or a direct supported exact-pin `npx`/`uvx` recipe. Direct evaluator-capable commands remain refused.

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
hardlink aliases. On Windows, admission checks every distinct first PATH
candidate selected by the modeled libuv and Rust rules. In each directory,
libuv tries an extended name exactly, then appends `.com` and `.exe`; Rust
tries a dotted name exactly or appends only `.exe` to an extensionless name.
PATHEXT and POSIX execute-mode checks do not apply. Each dispatcher's quoted
PATH parsing is observed without normalizing directory components; quoted
SystemRoot is resolved before the containment comparison. A refusal from either
choice blocks admission. This does not reproduce implicit cwd, application or
system searches, or later agent environments.
Batch launchers refuse with `mcp_windows_batch_unsupported` before host header
admission when a supported native write target is selected. The selected candidate's final kernel path is
compared against the resolved `%SystemRoot%`, ignoring case and normalizing
separators with a component boundary; SystemRoot is never traversed. An unset
or unresolvable SystemRoot refuses stdio commands, including pinned runners and
missing PATH candidates. Known
evaluator identities in other PATH/default directories also refuse. A searchable
PATH directory that cannot be enumerated fails closed rather than guessing
incomplete versioned or case-sensitive spellings. No command is executed.
Only each selected candidate's first 1024 bytes may be read. An unreadable
selected header refuses. Windows requires `MZ` magic and never interprets a
shebang; this does not validate a full PE image. On macOS, native recognition
requires a complete thin Mach-O fixed header with executable file type and
consistent nonzero load-command counts, or a big-endian universal header with
1 through 16 complete, nonempty architecture records and aligned offsets.
Swapped universal headers refuse. On Linux, ELF class, byte order, version,
executable/shared-object type and fixed-header size must be consistent.
Other-platform formats refuse. These bounded observations do not validate
architecture support, load commands, segments or a complete kernel-loadable
image; they do not prove that PATH continuation is impossible. Empty files and
text without `#!` refuse. A script line must end with newline within the first
256 bytes, including the newline. The interpreter word uses strict UTF-8,
splits at horizontal whitespace without shell quoting or stripping CR,
and refuses if it contains `#`. A separate `# comment` argument is permitted.
An invalid/non-executable interpreter refuses. For an otherwise opaque
interpreter, only its first two bytes are read: a current-platform native prefix
is required, and `#!`, short, text, foreign and unreadable prefixes refuse.
Two bytes cannot distinguish all files sharing a prefix (including Java and
universal Mach-O), establish loader validity, or inspect a nested chain.
These conservative refusals do not emulate every OS's script parsing.
Errors use `mcp_command_invalid` and name the bare candidate; admission never
falls through a refused choice to a later PATH entry. A missing non-base candidate retains the
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


`gitignore_required` может означать, что вложенное правило отменяет предлагаемое автоматическое исправление MCP ignore. Исправьте конфликтующее правило и повторите; отказ сохраняет native configs и live ignore file без изменений (`info/exclude` по умолчанию, `.gitignore` в opt-in или legacy mode). Git проверяет предлагаемую hierarchy до consent. `source_output_overlap` может указывать на output parent без права записи; восстановите write/search access и повторите. Эти readiness checks выполняются до prompt и не заменяют проверки при публикации.

## mcp_entry_conflict

`csk install` отказывает до публикации скиллов и MCP entries. В native config уже есть чужая запись с тем же именем; совпадение содержимого ownership не даёт.

Выберите другое имя в `mcp_servers` project Skillfile. Если существующая запись больше не нужна, удалите её вручную после проверки и повторите `csk install`. csk не выполняет takeover. Отчёт отмечает конфликтующую пару как `conflict: foreign entry`.

## mcp_entry_drift

Отчёт показывает `skipped: drift` и предупреждение, но установка продолжается. Пользователь изменил запись, которой владеет csk; csk сохраняет ручную правку и ownership.

Сохраните настройку агента или верните запись к установленному виду, чтобы csk снова обновлял её из Skillfile. Удаление native entry при сохранённом объявлении приводит к `restored` на следующей установке. Чтобы убрать сервер окончательно, удалите объявление из Skillfile. Если объявление уже удалено, а запись изменена, csk сообщает `released`: сохраняет запись и забывает ownership. Дальше управляйте записью вручную.

## mcp_plan_unapproved

Headless `csk install` отказывает с `mcp_plan_unapproved`. Объявление stdio или env reference требует consent. Интерактивный отказ от согласия даёт тот же результат.

Запустите `csk install` в терминале с TTY для stdin и stdout. Проверьте план и ответьте `y` или `yes` на запрос `Approve N MCP server(s) for <project>? [y/N]`. Флаги, env variables и файлы проекта не подтверждают consent. Для просмотра без записи используйте `csk install --dry-run`; эта команда не сохраняет согласие. Запрос согласия появляется после неинтерактивных preflight-проверок, а новое согласие сохраняется в одной транзакции с конфигурациями. После ошибки публикации повторная установка снова запросит согласие. HTTP URL без env reference consent не требует.

## mcp_native_config_unsupported_syntax

`csk install` отказывает до публикации. `.mcp.json` или `.cursor/mcp.json` содержит неподдерживаемый JSON: comments, trailing commas, BOM, duplicate keys или невалидный UTF-8. Для `.codex/config.toml` причиной может быть невалидный TOML или owned entry вне стандартной группы таблиц `[mcp_servers.NAME]`.

Исправьте native config вручную, сохранив остальные настройки. Для JSON используйте строгий UTF-8 JSON без BOM. В TOML приведите изменяемую owned entry к стандартным таблицам. Повторите `csk install`; csk не удаляет комментарии и не перезаписывает файл целиком для исправления синтаксиса.

## mcp_url_invalid

`csk install` отклоняет URL shorthand или `server.url` до публикации. URL использует другую схему, не-ASCII host, userinfo, query, fragment или запрещённые символы. Длина свыше 2048 символов тоже вызывает отказ.

Укажите HTTPS endpoint без этих компонентов и без секретов, например `https://mcp.example.com/mcp`. Для секрета используйте env reference в `install.headers`; такое объявление требует consent. Не переносите токен из query в путь URL. [Правила объявления](skill-authoring.md#project-mcp-schema-3) описывают обе формы.

## MCP environments: `skipped: unsupported`

Известный environment id без поддержки записи получает `mcp NAME -> AGENT (skipped: unsupported)` без пути; это не отказ и не отдельное предупреждение. В `status` состояние `unsupported` не влияет на `--check`. Без `server.environments` csk использует resolved agents проекта. Явно выбранный writable id записывается даже вне agents проекта. Неизвестный id, пустой список или дубликат вызывает `mcp_agent_selection_invalid`.

Локальные параметры записи и аутентификации находятся в `install`, например `install.headers`. Старое поле `csk` отклоняется с `mcp_declaration_invalid`; замените его на `install`. Unsupported ids не входят в consent subject; добавление writable агента требует нового согласия.

## Moved upstream tag: `moved_tag` / `would clobber existing tag`

An upstream history rewrite can move an existing release tag to a new commit,
even when its tree is unchanged. Current csk fetches the new tag, then compares
it with the installed marker. `csk upgrade app --strict-tags` (or
`csk global upgrade --strict-tags`) refuses with `moved_tag` and lists every
affected skill, tag, old commit and new commit. Installed skills stay unchanged;
retrying still refuses, even after `csk update` has refreshed the cache.

Check the upstream rewrite and the listed commits before accepting it. To accept
the new commits, run `csk upgrade app` or `csk global upgrade` without
`--strict-tags`; the command warns and updates the installation. To keep the old
commit, pin its revision in the Skillfile. `csk update` and `csk global update`
only refresh repository refs; they do not install skills or accept moved tags.
On older csk versions that fail with `would clobber existing tag`, upgrade csk
first; deleting the cache or installed markers is unnecessary.

For caches with several remotes, csk fetches only `origin`. Without `origin`,
the sole remote is used; multiple remotes without `origin` produce an explicit
ambiguity error before fetching. Configure `origin` to the intended upstream.
Repositories without any remote remain local. Network or authentication failures
are reported as fetch failures, not as moved tags.

## invalid install marker ... installed_at is not a UTC second timestamp

Маркер установки записан версией csk до 0.12.0 включительно в микросекундном формате; начиная с 0.12.1 csk пишет метку с точностью до секунды. Запустите `csk install` для проекта или `csk global install` для глобальных скиллов. Инсталлятор переустановит скилл и перепишет маркер в формате UTC-секунд без ручного удаления файлов.

## runtime_tree_modified

A skill wrote into its installed runtime or another csk-owned published skill
tree. Older versions could report `unsafe transaction tree entry` for a `.venv`
symlink; regular extra files and directories are also refused now.

Inspect the skill/member and offending path in the diagnostic. Run only the
exact quoted removal command it prints, which names the foreign entry or its
foreign enclosing directory, and then rerun the same install or upgrade. Do not
delete the entire runtime tree or use a wildcard over other members. csk does
not remove foreign entries automatically.

Update the skill so it stores environments, caches and mutable state outside
installed trees. On POSIX, successful publication removes all runtime write
bits while preserving execute bits; Windows permissions and ACLs are unchanged.
See the [runtime authoring contract](skill-authoring.md#installed-runtime-is-immutable).
For Python skills, use the [external venv recipe](skill-authoring.md#python-virtual-environments-outside-the-runtime).

## Cannot resolve system command outside csk shims

When resolution observes a candidate crossing an excluded tree, the diagnostic
names that candidate. A command may be visible to your shell yet refused while
csk creates a skill's launcher. csk excludes whole `.agents/bin` and csk `global/bin` trees, as well as
directories containing `.csk-managed.json` and their descendants. This includes
unrelated executables beside csk forwarders in a shared user bin directory.
Aliases and intermediate symlinks through these directories are also refused.

Install the real tool in a separate directory outside those trees and add its
absolute directory to `PATH` before running csk. A symlink is acceptable only if
every directory and file link in its chain stays outside the excluded trees;
adding an external alias that points back into a mixed shim directory does not
help. Use the same `PATH` in project setup and the agent's launch environment.
For an absolute command declaration, also change the declaration to the new
absolute location or a bare name on PATH. Changing PATH alone cannot repair an
absolute declaration. Missing commands instead receive installation guidance;
non-executable regular files receive permission/reinstallation guidance; invalid
relative declarations must become bare names or absolute paths.
Keep `.csk-managed.json` intact: deleting csk ownership records is not a remedy.

## go-v1 toolchain_shim_unresolved

В `PATH` может стоять shim менеджера версий Go (goenv, asdf или mise). csk один раз запрашивает через него `GOROOT`, затем использует найденный `<GOROOT>/bin/go` для проверок и fingerprint. Если ответ отсутствует, не является абсолютным существующим каталогом или содержит неподходящий бинарник, csk откажется с `toolchain_shim_unresolved`.

Поставьте реальный Go первым в `PATH` либо используйте `mise activate`, чтобы добавить настоящий бинарник вместо shim:

```bash
export PATH="$(go env GOROOT)/bin:$PATH"
csk install
```

На Windows `.cmd` и `.bat` shim не запускаются direct-process probe runner; укажите каталог с `go.exe`.

## go-v1 insecure_go_release

Квалифицированы семейства Go 1.26 от 1.26.10 и 1.27 от 1.27.3. Более ранние релизы, prerelease и другие неподтверждённые версии этих семейств отклоняются до сборки и чтения кэша. Сообщение называет обнаруженную версию, путь к Go и порог семейства. Строка `remediation:` предлагает поставить Go 1.27.3 или новее первым в `PATH`, например `go = "1.27.3"` в проектном `.mise.toml` либо `brew upgrade go`. Проверьте выбранный бинарник командой `go version`.

Также проверяется согласованность `go version` и `GOROOT/VERSION`: отсутствие файла, другая версия или посторонние строки вызывают `insecure_go_release` до сборки и чтения кэша. Обычная строка `time …` из официального дистрибутива допустима. При таком отказе переустановите полный дистрибутив Go; не исправляйте только файл `VERSION`.

Порог нужен для исправленной проверки TLS-сертификатов: собранные CLI включают стандартную библиотеку выбранного Go. Обновите Go и выполните `csk install` или `csk upgrade`: новая версия меняет cache key и требует пересборки CLI.

## После обновления Go

Обновите Go до квалифицированного релиза и проверьте выбранную версию командой
`go version`. Затем проверьте все проекты, пересоберите команды и повторите проверку:

```bash
csk status --all --check
csk install --all
csk global install
csk status --all --check
```

До пересборки статус `build-input-drift` и ненулевой exit code ожидаемы: установленный
CLI собран другим тулчейном. Для проектов, где установка обёрнута в mise task,
используйте `mise run setup-agents`, затем снова проверьте статус.

## go-v1 unsupported_go_family

Семейства 1.25 и старше не квалифицированы. Сообщение называет версию, путь к Go и поддерживаемые семейства и содержит ту же строку `remediation:` с Go 1.27.3 или новее. Семейства новее 1.27 принимаются с предупреждением по умолчанию; для строгого отказа задайте `builds.go_future_families: "refuse"` в `~/.cocoaskills/config.json` либо `CSK_GO_FUTURE_FAMILIES=refuse`.

## build_repository_credential_policy_invalid

Скоуп аутентификации `build_https` совпал в конфигурации, но выбранный источник токена не вернул данные. Текст ошибки называет конкретный случай из трёх возможных:

1. Правило `token_env` указывает незаданную переменную окружения (`the environment variable configured for build_https scope '<scope>' is not set`; имя переменной в ошибке намеренно не выводится, его показывает `csk config build-https list`). Экспортируйте значение переменной из хранилища перед запуском:

   ```bash
   export CI_TOKEN="$(pass show ci/gitlab)"
   csk install
   ```

2. Правило `token: keyring` не находит токен в хранилище ключей (`build_https scope '<scope>' selects a stored token, but none is saved`). Сохраните токен командой входа:

   ```bash
   csk config build-https login <scope>
   ```

3. Правило `token: git-credentials` не находит запись у helper Git для хоста (`build_https scope '<scope>' selects your Git credentials, but no helper holds one for '<host>'`). Склонируйте целевой репозиторий по HTTPS один раз через системный Git или сохраните токен через `login`:

   ```bash
   csk config build-https login <scope>
   ```

На платформе Windows команда `git credential approve` рапортует об успехе даже при недоступности службы Windows Credential Manager (например, в неинтерактивной сессии без графического входа). Команда `csk config build-https login` перечитывает токен после записи и при отказе сохранения выводит ошибку `your Git credential helper did not persist the token`. Для решения запустите команду в интерактивной сессии или настройте хранилище Git:

```bash
git config --global credential.credentialStore dpapi
```

## build_repository_source_unavailable / fatal: ... The requested URL returned error: 301

If Git reports an HTTP 3xx response, the server returned a redirect. csk pins `http.followRedirects=false` and refuses redirects for security. Legacy cloning reports `git clone failed: HTTP redirects are refused`; external build acquisition can report `build_repository_source_unavailable`, but that code alone does not establish a redirect. A redirect alone does not identify a missing suffix, invalid credentials or a missing repository: its destination may be a canonical endpoint, a login page or a browser page.

Verify the provider's canonical **Git clone URL** in its Clone UI or API, then correct the declaration that failed: `skills[*].git` in a legacy Skillfile, `sources.<alias>.git` in schema 2/3, or `build_repositories.*.git` in the skill manifest. For example, if the provider reports this clone URL:

```json
{
  "build_repositories": {
    "core": {
      "git": "https://gitlab.example.com/portals/infra.git"
    }
  }
}
```

Use that exact URL. The `.git` suffix is provider-specific; a suffixless URL serving Git smart HTTP directly is also valid. Do not append `.git` automatically, copy an arbitrary redirect destination, or enable redirects/disable TLS verification to bypass the refusal. If the provider's clone URL itself redirects, ask its server/proxy administrator to check routing and authentication. Static `csk check` validates declarations offline; it does not prove that an endpoint serves Git without redirects.

## Cannot resolve tag '...' ... Needed a single revision

Локальный клон репозитория в `skills_root` не содержит указанного тега. Команда `csk install` работает по локальным refs и не выполняет сетевой fetch.

Скачайте новые refs и переустановите замыкание зависимостей:

```bash
csk upgrade
```

Для глобальных скиллов используйте `csk global upgrade`. Команды `csk upgrade` и `csk global upgrade` скачивают новые теги из удалённого репозитория и запускают установку.

## commands are installed in .../.agents/bin, which is not on PATH

Установка завершилась успешно. Агентские скиллы вызывают шимы напрямую по абсолютным путям, поэтому добавление `.agents/bin` в `PATH` опционально.

Для прямого вызова команд из интерактивного шелла настройте окружение:

```bash
csk shell-init --install
```

Команда запишет хук в директорию CocoaSkills и выведет команду для добавления в профиль шелла.

### Локальный ignore без изменений .gitignore

По умолчанию csk пишет `$GIT_COMMON_DIR/info/exclude`; linked worktrees делят этот файл. User lines вне блока сохраняются побайтово. Symlink, каталог или недоступный для записи exclude дают `git_exclude_invalid` до записи. Исправьте тип/права файла; csk не заменяет небезопасный файл. `gitignore_required` означает, что Git всё ещё видит собственный путь как tracked или не ignored: проверьте `git ls-files` и конфликтующие вложенные `.gitignore`. Проверка staged policy сохраняется в обоих режимах.

CocoaSkill хранит отдельный блок для каждого проекта/worktree между `# CocoaSkill project <key>` и `# End CocoaSkill project <key>`. Непрозрачный ключ определяется метаданными worktree и путём к корню проекта относительно репозитория. При обновлении меняется только собственный блок; остальные блоки и пользовательские байты остаются без изменений. Все операции записи выполняются по очереди под OS lock в постоянном файле `$GIT_COMMON_DIR/csk-exclude.lock`. При завершении процесса ОС освобождает lock; replay и rollback транзакции объединяют с текущим файлом только записанный блок проекта. Не удаляйте lock-файл, пока csk работает. Старые exclude-блоки без ключа сохраняются как существующие правила; удаляйте устаревшие записи вручную.

In 0.21.x an existing top-level `ignore_mode` field still selects the effective mode until migration. Run `csk ignore-mode show` to see the mode and its source. Use `csk ignore-mode set gitignore` to select a managed project block, or `csk ignore-mode set exclude` for local Git excludes. The switch preserves user rules and removes only the deprecated field. Without that field, block location selects the mode and exclude is the default. Global/hybrid modes are unchanged.


### Локальная проверка SSH-учётных данных

При локальных проблемах SSH запустите `csk config build-ssh doctor <host/path>`
(при необходимости с `--all-os`). Doctor не подключается к сети и не меняет
ssh-agent. Срок ключей, ответственность оператора, закрепление публичного
ключа и ограничения аппаратных вариантов описаны в
[справочнике doctor](cli.md#csk-config-build-ssh-doctor). Предпочитайте
приватный путь даже с ssh-agent: он подходит для загруженного ключа и
пригодного файла. Ключ с passphrase всё ещё требует совместимый ssh-agent
с загруженным ключом, пока SSH-запросы отключены.

<a id="ssh-build-repository-failures"></a>

## Ошибки SSH-репозиториев сборки

Настройте учётные данные до первой установки по [руководству по SSH](ssh-key-setup.md).
Сначала создайте глобальный config через `csk bootstrap`: без него команды
`config build-ssh add` и `list` дают `Global config not found`.
csk изолирует SSH fetch от `~/.ssh/config` и обычной конфигурации Git.
Успешный `git clone` не доказывает, что csk выбрал те же ключ, ssh-agent и клиент.

### build_repository_ssh_credential_missing

Для репозитория нет выбора оператора, отсутствует файл проверенных ключей
хоста либо POSIX `--agent auto` не находит `SSH_AUTH_SOCK`. Последний случай
даёт `SSH agent was requested but SSH_AUTH_SOCK is not set`.

Загрузите нужный ключ в свой ssh-agent, передайте csk его сокет и сохраните
scope через `csk config build-ssh add`. Для загруженного ключа:

```sh
csk config build-ssh add git.example.com/team --agent auto --identity ~/.ssh/build_key.pub
```

Если `csk install` работает с интерактивными stdin и stdout, для репозитория
без выбора появляется меню найденных кандидатов. Enter выбирает первый
вариант; следующий Enter принимает scope по умолчанию. Без интерактивного
терминала csk отказывает и печатает команды настройки. Это меню выбора
учётных данных, а не запрос SSH passphrase или PIN: ключ загружает и
разблокирует оператор, SSH-запросы остаются отключёнными.

При `SSH build repositories pin StrictHostKeyChecking and require operator host keys`
укажите проверенный файл ключей хоста через `--known-hosts` в команде `add`.
Команда сохраняет выбор, но не загружает ключ. В файловом режиме выберите
незашифрованный приватный файл через `--identity` без `--agent`.
Проверьте `csk config build-ssh list`, границы scope и переопределения
`--build-ssh-*` или `CSK_BUILD_SSH_*`, затем повторите команду.

### build_repository_ssh_credential_unusable

Предварительная проверка отклоняет выбор, только когда локальные метаданные
доказывают непригодность для работы без SSH-запросов. В старых версиях этой
проверки нет. Проверьте выбранные учётные данные:

```sh
csk config build-ssh doctor git.example.com/team/tools
```

Доказанно зашифрованный файл отклоняется до создания wrapper и сетевого
обращения, если ssh-agent не выбран либо подтверждено отсутствие ключа.
Недоступный или неоднозначный ответ ssh-agent оставляет состояние `unknown`
и не доказывает отсутствие ключа.

Ошибка содержит `cannot fetch`, идентичность репозитория, `reason:` и `next:`.
Следуйте действию для указанной причины:

| Причина | Следующий шаг |
| --- | --- |
| `key has a passphrase and no matching agent key is available; SSH prompts are disabled` | Загрузите ключ в совместимый операторский ssh-agent со сроком задачи через `ssh-add -t`, затем выберите этот ssh-agent и идентичность. |
| `a public key alone cannot sign and no matching agent key is available` | Загрузите соответствующий приватный ключ в выбранный ssh-agent либо выберите пригодный приватный файл. |
| `SSH has no configured security-key provider in this lane` | Выберите первым в `PATH` OpenSSH со встроенной поддержкой FIDO, даже если ssh-agent перечисляет ключ; флага выбора provider в csk нет. |
| `private key permissions are too open; SSH will ignore this file` | Исправьте права приватного файла по своей политике либо выберите совместимый ssh-agent с соответствующим ключом. csk никогда не меняет права ключа. |
| `selected .pub file cannot sign directly; public-key pins need a matching agent; select a usable private identity` | Загрузите ключ в выбранный ssh-agent либо выберите пригодный приватный файл. Публичный файл не подписывает; сообщение не требует менять его права. |

Отказ по правам основан на фактах POSIX `stat`: обычный файл принадлежит
текущему пользователю и имеет биты доступа группы или остальных пользователей.
Текст ошибки SSH сам по себе этого не доказывает. При загруженном ключе права
файла не доказывают неспособность подписывать. Неизвестное наличие ключа
также не доказывает, что остаётся только файловый режим. Имя сокета не
устанавливает реализацию ssh-agent или отказ подписи.

Независимая проверка загрузки с пустой passphrase охватывает SK-контейнеры и
старые PEM файлы даже без fingerprint. Доказанное отсутствие provider
отклоняет SK-идентичность даже при соответствующем ключе в ssh-agent.
Неоднозначный вывод provider остаётся `unknown`; это не обещает успеха подписи.
На настоящем аппаратном токене не проверен ни один вариант. Отказ получает
класс `auth-unavailable` для явно объявленной политики запасного транспорта
и не разрешает автоматическое переключение на HTTPS.

Doctor работает без пригодного config и с закрытыми stdin или stderr.
Уведомление о config означает использование явных параметров и окружения
без исправления config. Проверьте терминал, число ключей и наличие выбранного
ключа в ssh-agent, а также локальную запись ключа хоста.
`Terminal: not attached` само по себе не делает ключ непригодным.
Найденная запись подтверждает только локальное наличие: fingerprint
сверяется независимо. Добавьте проверенную запись хоста перед повторным fetch.

<a id="ssh-diagnostic-actions"></a>

### Действия по SSH-диагностике

Транспортные ошибки сохраняют классы `attempt 1=...`. Прямой отказ допуска
Git может печатать `failure class:`. Каждый класс получает действие `next:`.
Действия и подсказки транспорта содержат номер попытки: `next: attempt 2:`
и `hint: attempt 2:`. Распознанные подсказки сохраняются при timeout.
Ошибка сборки называет репозиторий, команду и скилл. Старые версии могут
показывать только общий отказ приватной операции Git.

| Класс отказа | Следующий шаг |
| --- | --- |
| `dns` | Проверьте DNS хоста репозитория и повторите команду. |
| `connection-refused` | Проверьте хост, порт и сетевой доступ. |
| `timeout` | Проверьте сеть и ожидание passphrase, PIN, касания или подтверждения ssh-agent оператора. Совет не включает запрос passphrase в csk. |
| `endpoint-unavailable` | Проверьте указанную точку входа репозитория и повторите команду. |
| `auth-unavailable` | Выберите пригодные SSH или явно выбранные HTTPS учётные данные. |
| `ssh-auth-rejected` | Проверьте локальную пригодность ключа и права чтения на сервере; ключ с passphrase может быть не загружен в совместимый ssh-agent. Просмотрите `csk config build-ssh list`. |
| `auth-rejected` | Проверьте права чтения репозитория выбранной учётной записью. |
| `host-key` | Независимо сверьте fingerprint хоста и исправьте выбранный `known_hosts`. |
| `unclassified` | Просмотрите `csk config build-ssh list`, затем проверьте выбранную команду Git/SSH локально с подключённым stderr. Для безопасной цепочки причин используйте `csk --verbose`. |

Следующие точные строки подсказок уточняют действие. Транспорт вставляет
`attempt N:` после `hint:`. Подсказки не повторяют исходный stderr SSH:

| Точная строка подсказки | Следующий шаг |
| --- | --- |
| `hint: SSH reported failure loading a public-key pin; load the matching key into a capable agent or select the matching private identity path with csk config build-ssh add` | Загрузите ключ в выбранный ssh-agent либо выберите соответствующий пригодный приватный файл. |
| `hint: SSH reported agent refusal; check whether the agent supports this key; for a PIN-required key use the file route with a usable provider rather than relying on a client terminal` | Проверьте поддержку ключа и подтверждения ssh-agent. Терминал клиента не передаёт PIN в ssh-agent; ограничение файлового режима описано ниже. |
| `hint: SSH reported a missing device; connect the security key and retry` | Подключите токен и проверьте поддержку клиентом перед повтором. |
| `hint: SSH reported invalid signing format; touch the security key when required and check that a usable security-key provider is selected` | Проверьте касание и поддержку provider. Один текст не доказывает конкретную причину. |
| `hint: SSH reported an unavailable PIN; use a PIN-capable attended file route or an agent configured with askpass` | Используйте операторский ssh-agent с получением PIN через askpass либо другие пригодные учётные данные. Режим файла с участием оператора в текущем csk недоступен. |
| `hint: SSH reported an inaccessible identity; correct the selected identity path with csk config build-ssh add` | Исправьте путь и доступность выбранного файла, затем повторите команду. |
| `hint: SSH reported a signing failure; check the selected key, agent and any required PIN or touch` | Проверьте поддержку и подтверждения ssh-agent, затем доступность токена. |
| `hint: SSH reported a key-loading failure; check the selected key format and unlock method` | Проверьте файл и формат; зашифрованный ключ загрузите в совместимый ssh-agent. |

Советы о файловом режиме в подсказках PIN и отказа ssh-agent требуют
возможностей, которых текущий csk не предоставляет. Поддерживаемого режима
с участием оператора и флага выбора provider нет. Используйте совместимый
операторский ssh-agent с нужным подтверждением и получением PIN либо другие
пригодные учётные данные. На настоящем аппаратном токене не проверен ни один вариант.

Подсказка подписи может сопровождать `unclassified`, но не меняет класс.
Диагностика не добавляет исходный вывод SSH даже при `--verbose`.
Если подробности попытки нельзя безопасно показать, используется причина
`transport refusal recorded without attempt detail`. Проверьте безопасную
цепочку причин и выбранные учётные данные перед локальным повтором команды.

### build_repository_identity_invalid

Прочитайте подробность ошибки. `SSH identity is unavailable` означает, что
путь идентичности не удалось разрешить. `SSH agent socket is unavailable`
означает то же для сокета. `SSH identity path must be absolute` и
`SSH agent socket path must be absolute` требуют полного пути.
`SSH agent socket is not an admitted socket` требует настоящего поддерживаемого
POSIX-сокета. На Windows выбирайте стандартный pipe через `--agent auto`;
произвольные pipes не допускаются.

Исправьте путь и доступность. Если ssh-agent перезапущен, выберите его текущий
сокет. Не заменяйте сокет обычным файлом и не ослабляйте права ради допуска.

`operator known hosts file is unreadable` или отсутствие файла ключей хоста
требует доступного файла с независимо проверенными ключами сервера.
Укажите его через `--known-hosts` в `csk config build-ssh add`, сохранив
тот же выбор ssh-agent и идентичности.

### `repository_endpoint_unavailable` / `Permission denied (publickey)`

Общая ошибка точки входа не доказывает непригодность ключа.
`Permission denied (publickey)` означает отказ SSH-аутентификации.
Проверьте scope, загруженный fingerprint, права учётной записи и путь
репозитория. Причиной также может быть зашифрованный файл, который не может
запросить passphrase. Загрузите ключ в свой ssh-agent через `ssh-add -t`
со сроком задачи и выберите ssh-agent с соответствующим `.pub`.

`Too many authentication failures` может означать, что ssh-agent предложил
слишком много ключей. Закрепите нужный публичный ключ через `--identity`
вместе с `--agent`. Не передавайте приватный ключ провайдеру.

### `Host key verification failed`

Неизвестный или изменившийся ключ хоста даёт отказ. Сверьте fingerprint с
провайдером или администратором и исправьте выбранный `known_hosts` оператора.
Не отключайте проверку и не принимайте замену вслепую. Повторите команду
с проверенным файлом.

<a id="ssh-hardware-and-agent-failures"></a>

### Отказы аппаратных ключей и ssh-agent

`signing failed ... from agent: agent refused operation` означает, что
выбранный ssh-agent не завершил подпись. Проверьте разблокировку, подтверждения
и поддержку типа ключа. `ssh-add -l` доказывает перечисление, а не способность
подписывать. Проверки системного ssh-agent macOS с программным provider
воспроизвели этот отказ для SK-ключей. Выберите совместимые клиент и ssh-agent;
повторная загрузка в тот же несовместимый ssh-agent не исправляет проблему.

Если несовместимая копия в ssh-agent перекрывает пригодный файл, оператор
может удалить только эту копию через `ssh-add -d ~/.ssh/build_key_sk.pub`.
Перед повтором настройте нужный вариант. Текущий csk не может использовать
зашифрованный файл, запросив его passphrase.

`Load key "...pub": bad permissions` или `WARNING: UNPROTECTED PRIVATE KEY FILE!`
могут появиться, когда SSH пытается использовать публичный файл без
соответствующего приватного ключа в ssh-agent. Загрузите приватный ключ
в выбранный ssh-agent, сохранив `.pub`. Изменение прав публичного файла
не позволяет ему подписывать.

При `signing failed ... device not found` проверьте подключение токена и
поддержку клиентом. У `signing failed ... invalid format` несколько возможных
причин, включая provider и незавершённое касание.
`signing failed ... incorrect passphrase supplied` может означать недоступный
PIN аппаратного ключа. Один текст не доказывает неверную passphrase;
не перебирайте PIN повторно.

Apple SSH может игнорировать SK-идентичности без provider. Изолированный fetch
csk не импортирует provider из `~/.ssh/config` или `SSH_SK_PROVIDER`.
Выберите первым в `PATH` клиент со встроенной поддержкой FIDO.
Поведение настоящих токенов и точные аппаратные сообщения не проверены:
исследование использовало программный provider.

### build_repository_transport_unclassified / build_repository_source_unavailable

Коды сами по себе не устанавливают причину аутентификации. Для отчёта
сохраните полную очищенную диагностику, идентичность репозитория и имя
команды. Проверьте клиент, учётные данные и clone URL провайдера.
Wrapper, перенаправляющий stderr SSH мимо Git, может убрать данные для
классификации. Исключите его из обычного локального повтора; не добавляйте
его в изолированный fetch csk.

При `timeout` проверьте доступность сети и ожидание подтверждения оператора
выбранным ssh-agent или токеном. Повторите после исправления причины.
Увеличение timeout не включает запрос passphrase.

<a id="ssh-build-platform-limit"></a>

### Ограничение платформы SSH-сборок

`go-repository-v1 is supported only on macOS and Windows; Linux qualification is deferred`
означает, что публикация внешней сборки на выбранной платформе недоступна.
Настройка учётных данных не снимает ограничение. Используйте поддерживаемую
платформу либо дождитесь поддержки Linux.

<a id="ssh-windows-agent-limit"></a>

### Ограничения ssh-agent Windows

В текущем main приватные SSH-репозитории поддерживаются со стандартным
pipe службы OpenSSH Windows. `--agent auto` выбирает `\\.\pipe\openssh-ssh-agent`
без `SSH_AUTH_SOCK`. Старые версии ещё могут выдавать ограничения POSIX-сокетов;
используйте версию с поддержкой Windows pipe.

`SSH agent pipe is unavailable` означает, что доступность стандартного pipe
не установлена. Проверьте, что служебный ssh-agent оператора запущен и содержит
нужный ключ. csk не запускает и не наполняет его.
`SSH agent socket is not the admitted Windows pipe` означает выбор другого
pipe. Используйте стандартный через `--agent auto`; обычные файлы и произвольные
pipes не подходят.

Текущий wrapper пока не поддерживает пути с пробелами. Используйте пути без
пробелов до выпуска исправления. Проверки Windows через loopback охватывают
программные ключи и стандартный служебный ssh-agent, но не аппаратные токены
и 1Password. Поведение настоящих токенов не проверено. Doctor использует тот
же выбор Windows `auto`, что и допуск, игнорируя `SSH_AUTH_SOCK` окружения.
