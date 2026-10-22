# Справочник CLI CocoaSkills

Утилита командной строки `csk` управляет установкой, обновлением и аудитом пакетов скиллов для агентов искусственного интеллекта. Документ содержит полное описание командных групп, subcommands, флагов и позиционных аргументов.

## Группа: Проект

Команды группы увязывают конфигурацию скиллов с конкретным git-репозиторием и файлом `Skillfile.json`.

Манифест `Skillfile.json` поддерживается в двух вариантах схемы.

Схема 2 является основным форматом проектного Skillfile. Она объявляет источники и селекторы скиллов. Команды `csk check`, `csk install`, `csk upgrade` и `csk status` поддерживают её по умолчанию.

Схема 1 остаётся поддерживаемым legacy-форматом и содержит список скиллов в секции `skills`.

### csk

Точка входа утилиты командной строки CocoaSkills. Выводит версию утилиты или справку по доступным командам.

**Синопсис:**

```bash
usage: csk [-h] [--version]
           {bootstrap,init,skill,install,update,upgrade,global,audit,status,gc,add,remove,hybrid,list,project,config,shell-init} ...
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.
* `--version`: выводит версию csk и завершает работу.

**Пример использования:**

```bash
csk --version
```

Команда выводит установленную версию утилиты CocoaSkills.

### csk init

Создает манифест `Skillfile.json` и записывает управляемый блок директорий в `.gitignore`.

**Синопсис:**

```bash
usage: csk init [-h] [--alias ALIAS] [--agents AGENTS] [--no-interactive]
                [path]
```

**Аргументы и флаги:**

* `path`: путь к директории проекта. По умолчанию используется текущий каталог.
* `-h`, `--help`: выводит краткое справочное сообщение.
* `--alias ALIAS`: определяет имя алиаса проекта в файле `config.json`.
* `--agents AGENTS`: список целевых агентов через запятую (`codex_cli`, `claude_code`, `cursor`, `gemini`, `opencode`, `windsurf`).
* `--no-interactive`: отключает интерактивные запросы при выполнении в скриптах.

**Пример использования:**

```bash
csk init --alias demo-app --agents codex_cli,claude_code
```

Команда создает `Skillfile.json` с указанными настройками и обновляет `.gitignore`.

### csk add

Добавляет новую запись скилла или обновляет существующую конфигурацию в манифесте `Skillfile.json`.

**Синопсис:**

```bash
usage: csk add [-h] [--git GIT] [--source SOURCE] (--tag TAG |
               --branch BRANCH | --revision REVISION) [--project PROJECT]
               name
```

**Аргументы и флаги:**

* `name`: имя добавляемого скилла.
* `-h`, `--help`: выводит краткое справочное сообщение.
* `--git GIT`: URL git-репозитория источника.
* `--source SOURCE`: имя локальной директории источника в каталоге `skills_root`.
* `--tag TAG`: тег git-репозитория.
* `--branch BRANCH`: ветка git-репозитория.
* `--revision REVISION`: хеш коммита git-репозитория.
* `--project PROJECT`: алиас проекта, `.` или путь к проекту. По умолчанию выбирает текущий проект.

**Пример использования:**

```bash
csk add --git https://github.com/example/skill-tracker --tag v1.0.0 skill-tracker
```

Команда добавляет скилл `skill-tracker` с привязкой к тегу `v1.0.0` в текущий `Skillfile.json`.

### csk remove

Удаляет объявление скилла из манифеста `Skillfile.json`.

**Синопсис:**

```bash
usage: csk remove [-h] [--project PROJECT] name
```

**Аргументы и флаги:**

* `name`: имя удаляемого скилла.
* `-h`, `--help`: выводит краткое справочное сообщение.
* `--project PROJECT`: алиас проекта, `.` или путь к проекту. По умолчанию выбирает текущий проект.

**Пример использования:**

```bash
csk remove skill-tracker
```

Команда удаляет запись `skill-tracker` из секции `skills` манифеста `Skillfile.json`.

### csk status

Сравнивает объявленный манифест с фактически установленным состоянием файлов скиллов и project MCP configs schema 3.

**Синопсис:**

```bash
usage: csk status [-h] [--all] [--check] [--attest] [--json] [target]
```

**Аргументы и флаги:**

* `target`: алиас проекта, `.` или путь к проекту.
* `-h`, `--help`: выводит краткое справочное сообщение.
* `--all`: показывает статус для всех зарегистрированных проектов.
* `--check`: завершает выполнение с кодом 1 при неактуальном скилле, ошибке проекта или MCP-состоянии кроме `current` и `unsupported`.
* `--attest`: проверяет установленные скиллы через доверенные реестры аудита.
* `--json`: выводит данные в формате JSON вместо таблицы.

**Пример использования:**

```bash
csk status --check
```

Команда возвращает нулевой код, если все установленные файлы соответствуют манифесту.

Для Skillfile schema 2 команда показывает актуальность относительно лока `Skillfile.lock.json` и ничего не записывает.

**MCP в project Skillfile schema 3:**

Строки имеют формат `mcp <server> -> <agent> <project-relative path> (<state>)`. csk перечисляет объявленные и ещё owned пары server/agent: сначала по имени сервера, затем `claude_code`, `codex_cli`, `cursor` и unsupported ids по алфавиту. Строки `mcp NAME -> AGENT (unsupported)` не содержат пути. Например:

```text
mcp docs -> claude_code .mcp.json (current)
mcp docs -> codex_cli .codex/config.toml (current)
mcp docs -> cursor .cursor/mcp.json (current)
MCP authentication and connectivity: not checked.
```

| State | Значение |
| --- | --- |
| `unsupported` | Известное окружение, для которого запись не поддерживается; не влияет на `--check`. |
| `current` | Owned entry соответствует объявлению и установленному csk содержимому. |
| `missing` | Объявленная entry или native config отсутствует. |
| `outdated` | Объявление изменилось; owned entry ещё содержит прежний результат установки и будет обновлена install. |
| `drift` | Существующая owned entry изменена вручную. |
| `conflict` | Чужая запись с тем же именем, включая совпадающее содержимое. |
| `unapproved` | Для объявления нет требуемого consent. |
| `unreadable` | Native config нельзя безопасно прочитать или разобрать. |
| `orphaned` | Owned entry больше не объявлена для этой пары server/agent. |

Приоритет при совпадении условий: `unreadable` → `orphaned` → `conflict`/`drift` → `unapproved` → `outdated` → `missing` → `current`. Непроверяемый приватный ledger или consent store даёт `ERROR mcp_ownership_unknown` без MCP-строк; `--check` в этом случае возвращает 1.

Обычный `status` возвращает 0 даже при неактуальном MCP-состоянии или ошибке в отчёте проекта. `status --check` возвращает 0, только если все MCP-строки `current` или `unsupported` (либо их нет) и остальные проверки проекта успешны; иначе 1. Ошибки конфигурации или использования команды дают 2. `status` не исправляет конфигурации, не сохраняет consent, не запускает серверы и не проверяет auth/connectivity.

Для `csk status --json` (также с `--check`) каждый объект в массиве проектов получает массив `mcp`. У каждой строки поля `server`, `agent`, `path`, `state`; например, `{"server":"docs","agent":"codex_cli","path":".codex/config.toml","state":"current"}`. Для `unsupported` поле `path` равно `null`. `clean` учитывает MCP и остальные проверки; `unsupported` не делает проект неактуальным. При `mcp_ownership_unknown` поле `mcp` пустое, а ошибка находится в `errors`. Сообщение про auth/connectivity в JSON не выводится. Project schema 1/2 и global status сохраняют прежний формат без MCP-строк и поля `mcp`.

Полный Skillfile и пример перехода `outdated` → `current` приведены в [руководстве по project MCP](skill-authoring.md#project-mcp-schema-3).

### csk check

Проверяет Skillfile без установки. Команда принимает schema 2 без дополнительной настройки.

**Синопсис:**

```bash
usage: csk check [-h] [--all] [target]
```

**Аргументы и флаги:**

* `target`: алиас проекта, `.` или путь к проекту.
* `-h`, `--help`: выводит краткое справочное сообщение.
* `--all`: проверяет все зарегистрированные проекты.

**Пример использования:**

```bash
csk check
```

Schema 3 также проверяется: MCP declarations проходят тот же валидатор и коды отказа, что install; вывод содержит реальный номер schema.

Команда проверяет структуру Skillfile schema 2, строит планы сетевых источников по файлу `source-policy.json` без сетевых обращений и сверяет лок при его наличии. Код завершения 0 означает корректность. Код 1 означает сбой проверки объявлений, политики, плана или лока. Код 2 означает ошибку конфигурации или использования. Сюда входит Skillfile, который не разбирается, пока команда определяет проект по текущему каталогу или по пути (`csk check`, `csk check .`). Для зарегистрированного проекта, указанного алиасом, и для `--all` тот же сбой разбора даёт код 1: например, `source_name_conflict` завершает `csk check` без аргумента кодом 2, а `csk check` с алиасом проекта кодом 1. Вердикт `valid` подтверждает корректность объявлений и планов. Команда не загружает источники, поэтому `csk install` может отклонить Skillfile, прошедший проверку: например, установка не принимает унаследованные записи и корень источника `path`. Набор conformance-проверок закреплён на draft vectors curator-spec до выхода следующей редакции спецификации.

### csk list

Выводит список зарегистрированных проектов и объявленных скиллов.

**Синопсис:**

```bash
usage: csk list [-h] [--paths]
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.
* `--paths`: выводит пути к проектам, чекаутам и манифестам.

**Пример использования:**

```bash
csk list --paths
```

Команда печатает список всех проектов из `config.json` с путями на диске.

### csk project add

Регистрирует путь к проекту в конфигурации `config.json` и создает `Skillfile.json`, если файл отсутствует.

**Синопсис:**

```bash
usage: csk project add [-h] alias path
```

**Аргументы и флаги:**

* `alias`: имя алиаса проекта.
* `path`: путь к директории проекта.
* `-h`, `--help`: выводит краткое справочное сообщение.

**Пример использования:**

```bash
csk project add demo-app /path/to/project
```

Команда связывает алиас `demo-app` с файловым путем проекта.

### csk project resolve

Выводит информацию о выбранном `Skillfile.json`, алиасе проекта и целевых директориях без выполнения установки.

**Синопсис:**

```bash
usage: csk project resolve [-h] [target]
```

**Аргументы и флаги:**

* `target`: алиас проекта, `.` или путь к проекту.
* `-h`, `--help`: выводит краткое справочное сообщение.

**Пример использования:**

```bash
csk project resolve .
```

Команда показывает пути файловых адаптеров и директории генерации шимов.

## Группа: Скиллы и зависимости

Команды группы скачивают источники скиллов, проверяют структуру и генерируют файлы адаптеров.

### csk install

Выполняет установку объявленных скиллов из `Skillfile.json` по локальным git-ссылкам и скачивает отсутствующие URL-источники.

Если при установке закрытого SSH-репозитория сборки не заданы ни флаги, ни переменные окружения, ни скоупы в конфигурации: на интерактивном терминале (TTY) открывается меню обнаруженных кандидатов (агент с числом загруженных ключей и публичные ключи `*.pub` из `~/.ssh`) ; выбор сохраняется в конфиг только после явного выбора скоупа. В неинтерактивном окружении (без TTY) установка прерывается с ошибкой `build_repository_ssh_credential_missing` и выводит готовые команды `csk config build-ssh add`, сформированные из найденных кандидатов.

**Синопсис:**

```bash
usage: csk install [-h] [--all] [--dry-run] [--verbose] [--fix-gitignore]
                   [--strict-tags] [--audit [{advisory,strict}]]
                   [--build-ssh-identity PATH] [--build-ssh-agent [SOCKET]]
                   [--build-ssh-known-hosts PATH]
                   [target]
```

**Аргументы и флаги:**

* `target`: алиас проекта, `.` или путь к проекту.
* `-h`, `--help`: выводит краткое справочное сообщение.
* `--all`: выполняет установку для всех зарегистрированных проектов.
* `--dry-run`: рассчитывает план действий без изменения файлов на диске.
* `--verbose`: выводит подробный ход выполнения.
* `--fix-gitignore`: устаревший флаг; рекомендуем использовать `csk init`.
* `--strict-tags`: завершает работу ошибкой, если тег локально указывает на другой коммит.
* `--audit [{advisory,strict}]`: запускает проверку аудита перед установкой (по умолчанию: `advisory`).
* `--build-ssh-identity PATH`: файл идентичности SSH для приватных репозиториев сборки; приватный ключ или соответствующий публичный ключ в сочетании с `--build-ssh-agent` (переменная: `CSK_BUILD_SSH_IDENTITY`).
* `--build-ssh-agent [SOCKET]`: сокет SSH-агента для приватных репозиториев сборки; голый флаг или `auto` берёт `SSH_AUTH_SOCK` (переменная: `CSK_BUILD_SSH_AGENT`).
* `--build-ssh-known-hosts PATH`: путь к файлу known_hosts для проверки ключей хоста (по умолчанию: `~/.ssh/known_hosts`; переменная: `CSK_BUILD_SSH_KNOWN_HOSTS`).

**Пример использования:**

```bash
csk install --verbose
```

Команда проверяет гейт аудита и раскладывает файлы скиллов в `.agents/skills`.

Для Skillfile schema 2 команда выполняет начальную установку с разрешением источников при отсутствии лока и установку по локу при его наличии. Команда читает источники `path` с диска, а источники `git` и `repository` получает через ограниченный транспорт по файлу `source-policy.json`. Если сохранённого снимка нет, команда восстанавливает его из источника: для `path` читает текущие байты, для `git` и `repository` получает точный commit из лока. Совпадение идентичности и `content_sha256` продолжает установку. Несовпадение вызывает `source_snapshot_changed`. Недоступный источник вызывает `source_snapshot_unavailable` и указывается в сообщении. Восстановление не меняет лок и не разрешает теги или ветки. Руководство находится в `docs/skillfile-sources.md`.

Для schema 3 `csk check` также проверяет MCP declarations тем же валидатором и с теми же кодами, что install, и печатает `schema_version 3 valid` только после успешной проверки. Команда ничего не записывает и не обращается к сети.

`csk init` настраивает ignore для обычных generated paths. При установке MCP с `git_policy: "local"` install автоматически добавляет и сообщает точные ignore entries для создаваемых им MCP configs; deprecated `--fix-gitignore` не нужен. Dry-run показывает эти изменения без записи. Consent запрашивается после неинтерактивных preflight-проверок и сохраняется только вместе с успешной публикацией. Отказ проекта передаёт свой код в MCP-строки; если собственного кода нет, строки показывают `blocked by project_preflight_failed`.

Before consent, install checks existing output-parent readiness and asks Git to evaluate the proposed ignore hierarchy. A root negation is repaired effectively; a conflicting nested rule refuses with `gitignore_required` and unchanged native/ignore state. Runner/evaluator names in any `args` position refuse with `mcp_command_invalid`, including path/case/suffix aliases and tokens intended as data such as `--tool node`.

Staged ignore checks preserve the live `core.excludesFile` source (including relative paths), `.git/info/exclude` and default global excludes.

Pin and no-eval rules cover recognized runners/evaluators. Known evaluator-capable
commands (including awk/sed, interpreters, editors, debuggers and command-launching
OS tools) refuse even with plain script arguments. Within each argument, csk splits
on characters outside `[A-Za-z0-9_.+-]`, ignores case, strips path prefixes and
`.exe`/`.cmd`/`.bat`/`.ps1`/`.com`, and refuses recognized names as words.
Thus `a=npx pkg`, `system("npx pkg")`, `--exec=npx` and data containing the word
`node` refuse; `--node-id` and `my-server --port 1` remain valid. This intentionally
fails closed on false positives. Direct supported pinned `npx`/`uvx` remain valid.
Any other bare program runs as is: csk cannot verify what it runs. Consent shows
its exact command and argument array and states this limit. This is no sandbox or
proof about arbitrary programs; non-default evaluators outside the recognized
inventory and hidden execution semantics remain the documented limit.


Для schema 3 объект MCP содержит `server` и необязательный `install`: локальные параметры записи и аутентификации устанавливающего менеджера. Без `server.environments` используются resolved agents проекта; явный writable id пишется даже вне этого набора. Известные unsupported ids дают `mcp NAME -> AGENT (skipped: unsupported)` без пути и отдельного warning, включая отчёт при отказе. Dry-run добавляет `would:`. Неизвестный id, дубликат или пустой список вызывает `mcp_agent_selection_invalid`. Добавление writable агента требует нового consent; unsupported ids в consent subject не входят. [Правила MCP](skill-authoring.md#project-mcp-schema-3) перечисляют ids и поля `install`.

### csk update

Выполняет подтягивание git-источников для каждого репозитория скиллов в каталоге `skills_root`.

**Синопсис:**

```bash
usage: csk update [-h]
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.

**Пример использования:**

```bash
csk update
```

Команда обновляет локальные git-репозитории в каталоге источников без изменения проектов.

### csk upgrade

Скачивает свежие коммиты источников для указанного проекта и сразу запускает процесс установки.

**Синопсис:**

```bash
usage: csk upgrade [-h] [--all] [--dry-run] [--verbose] [--fix-gitignore]
                   [--strict-tags] [--audit [{advisory,strict}]]
                   [--build-ssh-identity PATH] [--build-ssh-agent [SOCKET]]
                   [--build-ssh-known-hosts PATH]
                   [target]
```

**Аргументы и флаги:**

* `target`: алиас проекта, `.` или путь к проекту.
* `-h`, `--help`: выводит краткое справочное сообщение.
* `--all`: подтягивает обновления и обновляет все зарегистрированные проекты.
* `--dry-run`: рассчитывает план действий без изменения файлов на диске.
* `--verbose`: выводит подробный ход выполнения.
* `--fix-gitignore`: устаревший флаг; рекомендуем использовать `csk init`.
* `--strict-tags`: завершает работу ошибкой, если тег локально указывает на другой коммит.
* `--audit [{advisory,strict}]`: запускает проверку аудита перед установкой (по умолчанию: `advisory`).
* `--build-ssh-identity PATH`: файл идентичности SSH для приватных репозиториев сборки; приватный ключ или соответствующий публичный ключ в сочетании с `--build-ssh-agent` (переменная: `CSK_BUILD_SSH_IDENTITY`).
* `--build-ssh-agent [SOCKET]`: сокет SSH-агента для приватных репозиториев сборки; голый флаг или `auto` берёт `SSH_AUTH_SOCK` (переменная: `CSK_BUILD_SSH_AGENT`).
* `--build-ssh-known-hosts PATH`: путь к файлу known_hosts для проверки ключей хоста (по умолчанию: `~/.ssh/known_hosts`; переменная: `CSK_BUILD_SSH_KNOWN_HOSTS`).

**Пример использования:**

```bash
csk upgrade --all
```

Команда скачивает новые коммиты для всех проектов и пересобирает адаптеры.

Для Skillfile schema 2 команда выполняет явное обновление: единственную операцию, которая заменяет зафиксированные ссылки, байты и состав лока. Обновление охватывает источники `path`, `git` и `repository`; ссылки `tag` и `branch` команда разрешает заново.

### csk skill check

Проверяет структуру скилла, корректность манифеста и формат файла `SKILL.md` в указанном каталоге.

**Синопсис:**

```bash
usage: csk skill check [-h] [--locale LOCALE] [--json] path
```

**Аргументы и флаги:**

* `path`: путь к директории скилла.
* `-h`, `--help`: выводит краткое справочное сообщение.
* `--locale LOCALE`: выбранная локаль для проверки правил.
* `--json`: выводит список найденных ошибок в формате JSON.

**Пример использования:**

```bash
csk skill check /path/to/skill
```

Команда проверяет валидность файлов скилла и выводит список ошибок или предупреждений.

Подробные требования к манифестам, компилируемым утилитам и скрипт-командам описаны в документах [Поставка CLI-утилит](authoring-cli-commands.md) и [Руководство по созданию скиллов](skill-authoring.md).

## Группа: Global и Hybrid

Команды группы управляют скиллами пользовательского уровня и правилами гибридного связывания.

### csk global init

Создает манифест глобальных скиллов `~/.cocoaskills/global/Skillfile.json`.

**Синопсис:**

```bash
usage: csk global init [-h]
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.

**Пример использования:**

```bash
csk global init
```

Команда инициализирует структуру глобальной конфигурации пользователя.

### csk global add

Добавляет объявление глобального скилла в манифест `~/.cocoaskills/global/Skillfile.json`.

**Синопсис:**

```bash
usage: csk global add [-h] [--git GIT] [--source SOURCE] (--tag TAG |
                      --branch BRANCH | --revision REVISION)
                      name
```

**Аргументы и флаги:**

* `name`: имя скилла.
* `-h`, `--help`: выводит краткое справочное сообщение.
* `--git GIT`: URL git-репозитория источника.
* `--source SOURCE`: имя локальной директории в `skills_root`.
* `--tag TAG`: тег git-репозитория.
* `--branch BRANCH`: ветка git-репозитория.
* `--revision REVISION`: хеш коммита.

**Пример использования:**

```bash
csk global add --git https://github.com/example/skill-metrics --tag v1.0.0 skill-metrics
```

Команда записывает скилл `skill-metrics` в глобальный манифест.

### csk global remove

Удаляет объявление скилла из файла `~/.cocoaskills/global/Skillfile.json`.

**Синопсис:**

```bash
usage: csk global remove [-h] name
```

**Аргументы и флаги:**

* `name`: имя удаляемого глобального скилла.
* `-h`, `--help`: выводит краткое справочное сообщение.

**Пример использования:**

```bash
csk global remove skill-metrics
```

Команда удаляет запись скилла из пользовательского манифеста.

### csk global list

Выводит список объявленных глобальных скиллов.

**Синопсис:**

```bash
usage: csk global list [-h]
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.

**Пример использования:**

```bash
csk global list
```

Команда выводит имена и источники всех глобальных скиллов пользователя.

### csk global status

Показывает статус установки глобальных скиллов и состояние сборок.

**Синопсис:**

```bash
usage: csk global status [-h] [--check] [--json]
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.
* `--check`: завершает выполнение с ненулевым кодом при наличии неактуальных скиллов.
* `--json`: выводит данные в формате JSON.

**Пример использования:**

```bash
csk global status --check
```

Команда проверяет актуальность файлов в пользовательских каталогах агентов.

### csk global install

Устанавливает глобальные скиллы из `~/.cocoaskills/global/Skillfile.json` в пользовательский профиль.

Для закрытых SSH-репозиториев сборок команда аналогично выполняет предварительную проверку кредов: на интерактивном терминале (TTY) открывает меню кандидатов (выбор сохраняется в конфиг только после явного выбора скоупа), а без TTY завершается ошибкой `build_repository_ssh_credential_missing`.

**Синопсис:**

```bash
usage: csk global install [-h] [--dry-run] [--verbose] [--strict-tags]
                          [--audit [{advisory,strict}]] [--only NAME]
                          [--build-ssh-identity PATH]
                          [--build-ssh-agent [SOCKET]]
                          [--build-ssh-known-hosts PATH]
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.
* `--dry-run`: рассчитывает план действий без изменения файлов на диске.
* `--verbose`: выводит подробный ход выполнения.
* `--strict-tags`: завершает работу ошибкой, если тег локально указывает на другой коммит.
* `--audit [{advisory,strict}]`: запускает проверку аудита перед установкой (по умолчанию: `advisory`).
* `--only NAME`: ограничивает операцию указанным глобальным скиллом (повторяемый флаг). Выбранный скилл затягивает свои обязательные зависимости в замыкание исполнения. Невыбранные декларации на диске остаются без изменений.
* `--build-ssh-identity PATH`: файл идентичности SSH для приватных репозиториев сборки; приватный ключ или соответствующий публичный ключ в сочетании с `--build-ssh-agent` (переменная: `CSK_BUILD_SSH_IDENTITY`).
* `--build-ssh-agent [SOCKET]`: сокет SSH-агента для приватных репозиториев сборки; голый флаг или `auto` берёт `SSH_AUTH_SOCK` (переменная: `CSK_BUILD_SSH_AGENT`).
* `--build-ssh-known-hosts PATH`: путь к файлу known_hosts для проверки ключей хоста (по умолчанию: `~/.ssh/known_hosts`; переменная: `CSK_BUILD_SSH_KNOWN_HOSTS`).

**Пример использования:**

```bash
csk global install --only skill-metrics
```

Команда разворачивает файлы скилла `skill-metrics` в пользовательские директории.

### csk global update

Выполняет подтягивание git-источников для глобальных скиллов.

**Синопсис:**

```bash
usage: csk global update [-h] [--only NAME]
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.
* `--only NAME`: ограничивает операцию указанным скиллом (повторяемый флаг).

**Пример использования:**

```bash
csk global update
```

Команда обновляет данные git-репозиториев глобальных скиллов в `skills_root`.

### csk global upgrade

Подтягивает источники из remote-репозиториев и выполняет установку глобальных скиллов.

**Синопсис:**

```bash
usage: csk global upgrade [-h] [--dry-run] [--verbose] [--strict-tags]
                          [--audit [{advisory,strict}]] [--only NAME]
                          [--build-ssh-identity PATH]
                          [--build-ssh-agent [SOCKET]]
                          [--build-ssh-known-hosts PATH]
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.
* `--dry-run`: рассчитывает план действий без изменения файлов на диске.
* `--verbose`: выводит подробный ход выполнения.
* `--strict-tags`: завершает работу ошибкой, если тег локально указывает на другой коммит.
* `--audit [{advisory,strict}]`: запускает проверку аудита перед установкой (по умолчанию: `advisory`).
* `--only NAME`: обновляет только один указанный скилл (повторяемый флаг).
* `--build-ssh-identity PATH`: файл идентичности SSH для приватных репозиториев сборки; приватный ключ или соответствующий публичный ключ в сочетании с `--build-ssh-agent` (переменная: `CSK_BUILD_SSH_IDENTITY`).
* `--build-ssh-agent [SOCKET]`: сокет SSH-агента для приватных репозиториев сборки; голый флаг или `auto` берёт `SSH_AUTH_SOCK` (переменная: `CSK_BUILD_SSH_AGENT`).
* `--build-ssh-known-hosts PATH`: путь к файлу known_hosts для проверки ключей хоста (по умолчанию: `~/.ssh/known_hosts`; переменная: `CSK_BUILD_SSH_KNOWN_HOSTS`).

**Пример использования:**

```bash
csk global upgrade
```

Команда запрашивает изменения из репозиториев и пересобирает файлы глобального контекста.

### csk hybrid add

Добавляет объявление гибридного скилла с привязкой к целевым проектам по алиасу, пути или глоб-шаблону.

**Синопсис:**

```bash
usage: csk hybrid add [-h] [--git GIT] (--tag TAG | --branch BRANCH |
                      --revision REVISION) --target TARGET
                      name
```

**Аргументы и флаги:**

* `name`: имя скилла.
* `-h`, `--help`: выводит краткое справочное сообщение.
* `--git GIT`: URL git-репозитория источника.
* `--tag TAG`: тег git-репозитория.
* `--branch BRANCH`: ветка git-репозитория.
* `--revision REVISION`: хеш коммита.
* `--target TARGET`: алиас проекта, абсолютный путь или глоб-шаблон (повторяемый флаг).

**Пример использования:**

```bash
csk hybrid add --git https://github.com/example/workflow-lint --tag v1.2.0 --target demo-ios workflow-lint
```

Команда привязывает скилл `workflow-lint` к проекту `demo-ios`.

### csk hybrid remove

Удаляет объявление гибридного скилла из манифеста `~/.cocoaskills/hybrid/Skillfile.json`.

**Синопсис:**

```bash
usage: csk hybrid remove [-h] name
```

**Аргументы и флаги:**

* `name`: имя удаляемого скилла.
* `-h`, `--help`: выводит краткое справочное сообщение.

**Пример использования:**

```bash
csk hybrid remove workflow-lint
```

Команда удаляет конфигурацию гибридного скилла из пользовательского файла.

### csk hybrid list

Выводит список гибридных скиллов и правил привязки к проектам.

**Синопсис:**

```bash
usage: csk hybrid list [-h]
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.

**Пример использования:**

```bash
csk hybrid list
```

Команда выводит имена скиллов, источники и их целевые шаблоны.

### csk hybrid status

Выводит текущее состояние объявлений гибридных скиллов и локального хранилища.

**Синопсис:**

```bash
usage: csk hybrid status [-h]
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.

**Пример использования:**

```bash
csk hybrid status
```

Команда показывает активность гибридных связей для текущих проектов.

## Группа: Сборки и аудит

Команды группы выполняют статический аудит безопасности и очищают временные файлы хранилища.

### csk audit

Запускает детерминированный статический аудит безопасности для объявленных скиллов.

**Синопсис:**

```bash
usage: csk audit [-h] [--all] [--global] [--json] [--allow CONTENT_SHA256]
                 [--reason REASON] [--publish RECORD] [--registry REGISTRY]
                 [--token-file PATH]
                 [target]
```

**Аргументы и флаги:**

* `target`: алиас проекта, `.` или путь к проекту.
* `-h`, `--help`: выводит краткое справочное сообщение.
* `--all`: выполняет аудит всех зарегистрированных проектов и глобальных скиллов.
* `--global`: выполняет аудит только для глобальных скиллов.
* `--json`: выводит результат в формате JSON.
* `--allow CONTENT_SHA256`: разрешает устаревший хеш содержимого для проверок схем v1/v2.
* `--reason REASON`: обязательное текстовое обоснование применения флага `--allow`.
* `--publish RECORD`: отправляет подписанный JSON-файл отчета в реестр аудита.
* `--registry REGISTRY`: URL реестра аудита для публикации отчета.
* `--token-file PATH`: обычный UTF-8 файл с токеном реестра размером не более 64 KiB; размер проверяется по открытому descriptor до чтения. На POSIX чтение для группы и остальных пользователей запрещено (например, права `0600`; `0620` также допустимы, поскольку запись для группы не даёт права чтения). Токен содержит только `A-Za-z0-9-._~+/=`; допускается один завершающий LF или CRLF. Пробелы и другие символы отклоняются, а не удаляются. Явно указанный файл имеет приоритет перед `CSK_REGISTRY_TOKEN`; непригодный или пустой файл приводит к отказу без перехода к переменной окружения.
* `CSK_REGISTRY_TOKEN`: источник токена реестра, когда `--token-file` не указан.
* `--token`: отклоняется с подсказкой использовать `CSK_REGISTRY_TOKEN` или `--token-file`; значение не выводится. Подробнее о том, что показывают ошибки usage, см. раздел «Коды возврата».

**Пример использования:**

```bash
csk audit --all --json
```

Команда сканирует декларации и выводит отчет безопасности в формате JSON.

### csk gc

Удаляет неиспользуемые runtime-директории, snapshot-записи, просроченный кэш сборок (старше 24 часов) и устаревшие записи реестра клиентов.

**Синопсис:**

```bash
usage: csk gc [-h]
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.

**Пример использования:**

```bash
csk gc
```

Команда удаляет неиспользуемые файлы под блокировкой менеджера.

## Группа: Сервисные

Команды группы управляют конфигурацией `csk` на уровне машины и настройкой оболочки shell.

### csk bootstrap

Создает файл конфигурации `~/.cocoaskills/config.json` в интерактивном режиме или по флагам. В интерактивном режиме утилита запрашивает базовые настройки (`skills_root`, `preferred_locale`, `default_agents`), а также предлагает опциональный вопрос по настройке параметров SSH-кредов сборки (`build_ssh`).

**Синопсис:**

```bash
usage: csk bootstrap [-h] [--skills-root SKILLS_ROOT]
                     [--preferred-locale PREFERRED_LOCALE]
                     [--default-agents DEFAULT_AGENTS] [--non-interactive]
                     [--force | --if-missing]
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.
* `--skills-root SKILLS_ROOT`: каталог для хранения git-репозиториев скиллов.
* `--preferred-locale PREFERRED_LOCALE`: предпочитаемая локаль установки (например, `ru`).
* `--default-agents DEFAULT_AGENTS`: список агентов по умолчанию через запятую (`codex_cli`, `claude_code`, `cursor`, `gemini`, `opencode`, `windsurf`).
* `--non-interactive`: отключает интерактивные вопросы и требует указания параметров флагами.
* `--force`: перезаписывает действующий файл конфигурации без подтверждения.
* `--if-missing`: создает файл только при его отсутствии.

**Пример использования:**

```bash
csk bootstrap --non-interactive --if-missing
```

Команда генерирует базовый файл `config.json`.

### csk config show

Выводит путь к файлу конфигурации и его текущее JSON-содержимое.

**Синопсис:**

```bash
usage: csk config show [-h]
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.

**Пример использования:**

```bash
csk config show
```

Команда выводит путь к `config.json` и структурированное дерево настроек.

### csk config build-ssh

Управляет скоупами SSH-кредов для внешних репозиториев сборки (`go-repository-v1`).

Скоуп является префиксом канонической идентичности репозитория (`host` или `host/namespace`). При выборе кредов побеждает самый длинный совпавший скоуп, причем сопоставление выполняется строго по границе сегмента `/`. Флаги `--build-ssh-*` и переменные окружения `CSK_BUILD_SSH_*` имеют приоритет над любой записью в конфигурационном файле.

**Синопсис:**

```bash
usage: csk config build-ssh [-h] {add,list,remove} ...
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.
* `add`: добавляет или перезаписывает скоуп SSH-кредов.
* `list`: выводит список настроенных скоупов SSH-кредов.
* `remove`: удаляет скоуп SSH-кредов.

Записи конфигурации сохраняются в файле `~/.cocoaskills/config.json` в секции `build_ssh`. Для добавления записи требуется указать минимум один из флагов `--agent` или `--identity`.

**Примеры использования:**

```bash
# Скоуп неймспейса: ssh-agent с фиксацией публичного ключа
csk config build-ssh add gitlab.example.com/portals/infra \
    --agent auto --identity ~/.ssh/work.pub

# Скоуп хоста: приватный ключ с диска без агента
csk config build-ssh add gitlab.example.com --identity ~/.ssh/personal

# Просмотр настроенных скоупов
csk config build-ssh list

# Удаление скоупа
csk config build-ssh remove gitlab.example.com/portals/infra
```

Команда управляет правилами аутентификации SSH в файле `~/.cocoaskills/config.json`.

### csk config build-ssh add

Добавляет или обновляет настройки SSH-аутентификации для указанного скоупа.

**Синопсис:**

```bash
usage: csk config build-ssh add [-h] [--agent [SOCKET]] [--identity PATH]
                                [--known-hosts PATH]
                                scope
```

**Аргументы и флаги:**

* `scope`: префикс канонической идентичности, например `gitlab.example.com/group`.
* `-h`, `--help`: выводит краткое справочное сообщение.
* `--agent [SOCKET]`: использовать сокет SSH-агента оператора. Голый флаг или значение `auto` подключают `SSH_AUTH_SOCK`.
* `--identity PATH`: путь к файлу идентичности. Указание файла `.pub` при заданном `--agent` фиксирует (пинит) предъявляемый ключ агента; без `--agent` определяет путь к приватному ключу на диске.
* `--known-hosts PATH`: переопределение файла known_hosts для выбранного скоупа.

### csk config build-ssh list

Выводит список всех зарегистрированных скоупов SSH-кредов.

**Синопсис:**

```bash
usage: csk config build-ssh list [-h]
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.

### csk config build-ssh remove

Удаляет сохраненный скоуп SSH-кредов из конфигурации.

**Синопсис:**

```bash
usage: csk config build-ssh remove [-h] scope
```

**Аргументы и флаги:**

* `scope`: префикс канонической идентичности удаляемого скоупа.
* `-h`, `--help`: выводит краткое справочное сообщение.

### csk config build-https

Управляет скоупами HTTPS-токенов для внешних репозиториев сборки (`go-repository-v1`).

Скоупы используют ту же грамматику, что и `build-ssh`: префикс канонической идентичности, сопоставление по границам сегментов `/`, побеждает самый длинный совпавший скоуп. Конфигурация хранит источник токена, а не сам токен. Переменная окружения `CSK_BUILD_HTTPS_TOKEN` перекрывает любой скоуп на один запуск; переменная `CSK_BUILD_HTTPS_HOST` ограничивает это перекрытие одним хостом.

**Синопсис:**

```bash
usage: csk config build-https [-h] {add,login,list,remove} ...
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.
* `add`: добавляет или перезаписывает скоуп источника токена.
* `login`: сохраняет токен через Git credential helper оператора и выбирает его для скоупа.
* `list`: выводит список настроенных скоупов.
* `remove`: удаляет скоуп и связанную запись keyring.

**Примеры использования:**

```bash
# Использовать существующие HTTPS-креды Git для хоста
csk config build-https add gitlab.example.com/portals/infra --token-source git-credentials

# Сохранить personal access token через credential helper (скрытый ввод)
csk config build-https login gitlab.example.com/vendor

# Читать токен из переменной окружения (CI и headless)
csk config build-https add ci.example.com --token-env MY_CI_TOKEN

# Просмотр и удаление
csk config build-https list
csk config build-https remove gitlab.example.com/vendor
```

Команда управляет правилами в секции `build_https` файла `~/.cocoaskills/config.json`.

### csk config build-https add

Добавляет или обновляет источник токена для указанного скоупа.

**Синопсис:**

```bash
usage: csk config build-https add [-h] [--token-source {git-credentials,keyring}]
                                  [--token-env NAME] [--username NAME]
                                  scope
```

**Аргументы и флаги:**

* `scope`: префикс канонической идентичности, например `gitlab.example.com/group`.
* `-h`, `--help`: выводит краткое справочное сообщение.
* `--token-source {git-credentials,keyring}`: источник токена. `git-credentials` читает существующую HTTPS-запись оператора для хоста; `keyring` читает токен, сохраненный командой `login`.
* `--token-env NAME`: имя переменной окружения (`[A-Za-z_][A-Za-z0-9_]{0,127}`, полное совпадение), из которой токен читается на входе в процесс. Иное значение отклоняется без вывода самого значения. Ошибки установки называют только скоуп (`the environment variable configured for build_https scope '<scope>' is not set`), а не имя переменной; настроенное NAME показывает `csk config build-https list`.
* `--username NAME`: имя пользователя HTTPS (по умолчанию `token`).

Требуется ровно один из флагов `--token-source` или `--token-env`. Литеральный токен не принимается ни одним флагом. Старый `--token` отклоняется без вывода его значения.

### csk config build-https login

Сохраняет personal access token через Git credential helper оператора под неймспейснутым именем пользователя `csk-build-https:<scope>` и выбирает источник `keyring` для скоупа. Ввод скрытый; при отсутствии TTY токен читается из stdin. Запись проверяется обратным чтением: helper, который молча ничего не сохранил, приводит к ошибке с платформенной подсказкой.

**Синопсис:**

```bash
usage: csk config build-https login [-h] [--username NAME] scope
```

**Аргументы и флаги:**

* `scope`: префикс канонической идентичности.
* `-h`, `--help`: выводит краткое справочное сообщение.
* `--username NAME`: имя пользователя HTTPS, предъявляемое при fetch (по умолчанию `token`).

### csk config build-https list

Выводит список всех настроенных скоупов; для источника `keyring` дополнительно показывает, сохранен ли токен (`stored=yes` или `stored=NO`).

**Синопсис:**

```bash
usage: csk config build-https list [-h]
```

**Аргументы и флаги:**

* `-h`, `--help`: выводит краткое справочное сообщение.

### csk config build-https remove

Удаляет скоуп из конфигурации и связанную запись keyring, если она существует.

**Синопсис:**

```bash
usage: csk config build-https remove [-h] scope
```

**Аргументы и флаги:**

* `scope`: префикс канонической идентичности удаляемого скоупа.
* `-h`, `--help`: выводит краткое справочное сообщение.

### csk shell-init

Генерирует код интеграции с оболочкой или устанавливает хук автоматического добавления `.agents/bin` в PATH.

Проектные `env.sh` и `env.ps1` добавляют `.agents/bin` в конец `PATH`, поэтому уже доступные команды имеют приоритет. Для обновления ранее созданных файлов окружения выполните `csk install`.

**Синопсис:**

```bash
usage: csk shell-init [-h] [--no-global] [--install]
                      [{auto,zsh,bash,powershell}]
```

**Аргументы и флаги:**

* `{auto,zsh,bash,powershell}`: выбор синтаксиса оболочки (по умолчанию: `auto`).
* `-h`, `--help`: выводит краткое справочное сообщение.
* `--no-global`: отключает регистрацию глобального каталога бинарников CocoaSkills.
* `--install`: сохраняет хук в домашний каталог и выводит инструкцию подключения в профиль.

**Пример использования:**

```bash
csk shell-init --install zsh
```

Команда сохраняет скрипт активации и выводит строки для добавления в `~/.zshrc`.

## Коды возврата

Утилита `csk` использует следующие коды завершения процесса:

* `0`: операция выполнена успешно.
* `1`: произошла ошибка при обработке скилла или проекта.
* `2`: ошибка конфигурации или невалидный синтаксис аргументов.
* `3`: блокировка файлов хранилища занята другим процессом.

### Ошибки usage и значения из argv

Каждый парсер и подпарсер `csk` печатает ошибку usage по закрытому шаблону. Текст собирается из частей, которые задаёт сам парсер: строк опций, dest, metavar и объявленных вариантов `choices`. Любая часть, пришедшая из argv, заменяется на `<value not shown>`: неизвестные аргументы, значение вне `choices`, неприводимое типизированное значение, неоднозначный префикс опции. Например:

```text
csk audit: error: argument -h/--help: ignored explicit argument <value not shown>
csk: error: unrecognized arguments: <value not shown>
```

Сообщение, не подошедшее ни под один шаблон argparse, заменяется на `invalid usage (details not shown); run 'csk <command> --help'`. Код возврата остаётся `2`. Списка «опасных» написаний `--token*` нет: опечатки, похожие символы, сокращения и склеенные формы обрабатываются тем же механизмом, что и любой другой ввод.

Значения опций с учётными данными (`--token-file` PATH, `--token-env` NAME, `--token-source`) не выводятся ни в одной ошибке или диагностике. Успешный вывод `csk config build-https list` может показывать настроенное NAME.

**Граница:** позиционные аргументы являются данными команды. Значение, переданное позиционно, в том числе после `--`, может появиться в собственных ошибках этой команды (например, `error: Unknown project alias: …`). Ошибки usage значений argv не показывают.

#### Compatibility bounds for credential NAMEs and command data

`csk config build-https add --token-env NAME` accepts exactly an ASCII letter
or underscore followed by zero to 127 ASCII letters, digits or underscores
(full-string match; maximum 128 characters). Lower-case and mixed-case NAMEs
are accepted. An identifier-shaped secret such as `ghp_abcdef123456` also
matches; this grammar cannot distinguish a NAME from a pasted token.

Compared with the existing configuration loader's `str.isidentifier()` grammar
(maximum 4096 characters), the CLI now refuses non-ASCII identifiers such as
`ÜBER_TOKEN`, and otherwise valid identifiers of 129–4096 characters. Existing
configuration entries in those classes still load and work; this command cannot
recreate them. Empty strings, digit-first names, punctuation, whitespace,
newlines and leading dashes are also refused. A leading-dash separate value is
parsed as an option (missing NAME); `--token-env=VALUE` applies the NAME grammar
to the full attached value. Neither refusal echoes the supplied value.

The command-data bound includes **all positional arguments and all values of
non-credential options**, including values supplied after `--`, lookalike-dash
or leading-space spellings that argparse treats as positionals, and values
reused both as a credential and as command data. These values may appear in a
command's execution errors: for example an unknown project alias, a build-HTTPS
scope, or the audit record path supplied to `audit --publish PATH`. This bound
does not cover usage errors: those never show argv values. Credential-only
values (`--token-file`, `--token-env`, `--token-source`) remain absent from error
and diagnostic output; successful `config build-https list` may show NAME.
