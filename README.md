# CocoaSkills

[![PyPI](https://img.shields.io/pypi/v/cocoaskills.svg)](https://pypi.org/project/cocoaskills/)
[![Python versions](https://img.shields.io/pypi/pyversions/cocoaskills.svg)](https://pypi.org/project/cocoaskills/)
[![License](https://img.shields.io/pypi/l/cocoaskills.svg)](https://github.com/ivanopcode/cocoaskills/blob/main/LICENSE)
[![CI](https://github.com/ivanopcode/cocoaskills/actions/workflows/ci.yml/badge.svg)](https://github.com/ivanopcode/cocoaskills/actions/workflows/ci.yml)

Установщик `csk` управляет локальными пакетами скиллов для AI-агентов. Инструмент загружает скиллы из git-репозиториев и подготавливает файлы для шести сред: Claude Code, Codex CLI, Cursor, Gemini, OpenCode и Windsurf.

## Зачем

Ручное управление скиллами в нескольких проектах создаёт проблемы при командной разработке. Содержимое файлов на компьютерах разработчиков расходится со временем. Без фиксации версий по тегам и коммитам обновления ломают рабочие окружения. Вспомогательные файлы, такие как README, тесты и файлы сборки, попадают в контекст агента и расходуют лимиты токенов. При удалении скилла из конфигурации неиспользуемые файлы остаются на диске.

Установщик `csk` решает эти проблемы декларативным описанием скиллов в `Skillfile.json`. Инструмент фиксирует версии git-репозиториев, копирует в контекст агента только `SKILL.md` и объявленные каталоги (`references/`, `assets/`, `agents/`, `data/`), исключая `tests`, `README`, файлы сборки и метаданные git, и удаляет устаревшие файлы при обновлении состава скиллов.

## Рынок и позиция CocoaSkills

Публичные инструменты закрывают отдельные этапы работы с агентскими навыками. Корпоративный контур требует единой системы управления локальным окружением и цепочкой поставок.

| Решение | Основной фокус | Сильная сторона | Ограничения для задач компании |
| --- | --- | --- | --- |
| Vercel (skills.sh) | Каталог и установка публичных навыков | Масштаб экосистемы и поддержка множества агентов | Ориентация на публичное распространение без защиты корпоративной цепочки поставки |
| SkillKit | Универсальное управление для разных агентов | Разнообразие интеграций, поиск и сканирование безопасности | Уклон в индивидуальное использование без детерминированной воспроизводимости графа зависимостей |
| Tessl | Корпоративное управление и реестр | Приватные навыки, правила аудита и оценка качества | Зависимость от централизованной платформы вместо локального менеджера пакетов |
| NVIDIA Skills / SkillSpector | Безопасность, подлинность и проверка качества | Цифровые подписи, сканирование угроз и бенчмарки | Проверка и публикация без управления полным жизненным циклом локального окружения |
| Agent Plugins | Открытый формат пакета | Объединение навыков и инструментов MCP в единый стандарт | Спецификация формата без функций установки, разрешения зависимостей и контроля правил |

Установщик `csk` работает на уровне локальной инфраструктуры проекта. Инструмент воспроизводимо и безопасно управляет агентскими навыками со следующими свойствами:

- Детерминированный граф зависимостей выявляет конфликты версий до начала установки.
- Поддержка закрытых источников позволяет ограничить загрузку разрешёнными git-репозиториями.
- Трёхуровневая изоляция разделяет контекст модели, исполняемую среду и артефакты сборки.
- Транзакционная установка гарантирует откат к исходному состоянию при ошибках.
- Каноническое состояние проекта не зависит от выбранного агента или среды разработки.
- Локальная проверка правил безопасности работает без обязательной связи с внешними сервисами.

Существующие инструменты решают задачи каталогизации и проверки навыков; `csk` закрывает соседний уровень: детерминированную и воспроизводимую установку скиллов внутри закрытого корпоративного контура.

## Почему CocoaSkills, а не альтернативы

**Ручное копирование папок или создание symlink:**
- Где ломается: требует повторного копирования во всех проектах при обновлениях и переносит лишние файлы репозитория в контекст модели.
- Что делает `csk`: автоматизирует загрузку, фильтрует содержимое до разрешённых каталогов и обновляет файлы одной командой.

**Подключение через git submodules или git subtree:**
- Где ломается: сохраняет полную историю репозиториев, требует команд git при переключении веток и не формирует адаптеры для агентов.
- Что делает `csk`: скачивает репозитории в локальный кэш, извлекает файлы скиллов и строит конфигурации для каждого агента.

**Встроенные маркетплейсы плагинов внутри конкретных агентов:**
- Где ломается: привязывает скиллы к одному агенту и не позволяет использовать единый манифест в командах с разными инструментами.
- Что делает `csk`: хранит единый `Skillfile.json` и раскладывает скиллы по адаптерам Claude Code, Codex CLI, Cursor и Gemini; OpenCode и Windsurf читают `.agents/skills/` напрямую.

**Общая директория скиллов с синхронизацией кастомными скриптами:**
- Где ломается: требует поддержки собственных скриптов, не проверяет контрольные суммы и не управляет транзитивными зависимостями.
- Что делает `csk`: вычисляет граф зависимостей скиллов, фиксирует точные хеши содержимого и изолирует сгенерированные файлы.

Инструмент CocoaSkills доставляет скиллы и записывает объявления MCP в native project configs. csk не запускает MCP-серверы и не выполняет аутентификацию. Публичный реестр пакетов и runtime-среда агента остаются вне его задач.

## Быстрый старт

1. Установите CocoaSkills одним из способов. На macOS предпочтителен Homebrew:

   <details open>
   <summary>Homebrew (рекомендуется на macOS; также Linux)</summary>

   ```bash
   brew tap ivanopcode/csk
   brew install cocoaskills
   ```

   </details>

   <details>
   <summary>uv tool</summary>

   ```bash
   UV_SYSTEM_CERTS=1 uv tool install cocoaskills
   ```

   </details>

   <details>
   <summary>pipx</summary>

   ```bash
   pipx install cocoaskills
   ```

   </details>

   <details>
   <summary>mise</summary>

   ```bash
   mise use -g pipx:cocoaskills@latest
   ```

   </details>

   <details>
   <summary>pip</summary>

   ```bash
   python -m pip install --user cocoaskills
   ```

   </details>

   Для установки из Python-экосистемы нужны uv **0.13.4+** или pip **26.2.2+**, включая pip в shared venv pipx. Обновление: `uv self update`, `python3 -m pip install --upgrade pip`, `pipx upgrade-shared`.

   One-liner на macOS загружайте актуальным Homebrew curl (при необходимости `brew install curl`):

   ```bash
   "$(brew --prefix)/opt/curl/bin/curl" -fsSL https://cocoaskills.org/install.sh | sh
   ```

   Скрипт проверяет версии и выбирает uv, затем pipx, затем pip; uv запускается с `UV_SYSTEM_CERTS=1`. `install.sh` предназначен только для POSIX shell на macOS и Linux, не для установки на Windows. На Linux используйте актуальный `curl`. Пока macOS не обновит `/etc/ssl/cert.pem`, не используйте `/usr/bin/curl` для этой загрузки, а Apple git настройте на обновлённый bundle Homebrew:

   ```bash
   git config --global http.sslCAInfo "$(brew --prefix)/etc/ca-certificates/cert.pem"
   ```

   На Linux обновите пакет `ca-certificates` средствами дистрибутива: системные curl, git и Python читают его хранилище. Go-бинарники без встроенного x509 blocklist при проверке доверия полагаются только на этот системный bundle.

   На Windows Git for Windows по умолчанию использует OpenSSL и собственный bundle. Предпочтителен Schannel, чтобы применялось уже обновлённое хранилище Windows:

   ```bash
   git config --global http.sslBackend schannel
   ```

   Python с python.org использует хранилище Windows; минимумы uv **0.13.4+** и pip **26.2.2+** остаются теми же. Устанавливайте через Python-инструменты выше, без `install.sh`.

   Для приватных HTTPS-репозиториев сборки csk передаёт токен оператора в git на время fetch. На macOS перед использованием HTTPS build token убедитесь, что git доверяет обновлённому bundle; настройка обычных вызовов git показана выше (`http.sslCAInfo`). Изолированный fetch csk не читает пользовательскую и системную Git-конфигурацию; csk выбирает bundle самостоятельно, удаляет CA из встроенного списка отказа и закрепляет приватную PEM-копию. Для явного выбора используйте `CSK_GIT_CA_BUNDLE` или `git.https_ca_bundle`. Доступ к сборкам по SSH это не затрагивает.

   Подробности проверки клиентов и доверия хоста — в [справочнике](docs/reference.md#доверие-хоста).

   Результат: команда `csk --version` выводит версию CocoaSkills.

2. Перейдите в каталог проекта и инициализируйте конфигурацию:

   ```bash
   cd /path/to/project
   csk init
   ```

   Результат: команда создаёт манифест `Skillfile.json` с начальной конфигурацией проекта и записывает блок директорий `.agents/`, `.claude/skills/`, `.codex/skills/`, `.cursor/rules/`, `.gemini/skills/` и файла `Skillfile.dev.json` в `.gitignore`.

3. Добавьте объявление скилла в проект:

   ```bash
   csk add skill-tracker --git git@gitlab.example.com:skills/skill-tracker.git --tag v1.0.0
   ```

   Результат: команда записывает объект скилла с именем `skill-tracker`, указанным репозиторием и тегом `v1.0.0` в массив `skills` файла `Skillfile.json`.

4. Выполните установку объявленных скиллов:

   ```bash
   csk install
   ```

   Результат: установщик скачивает репозиторий, извлекает файлы скилла в `.agents/skills/skill-tracker/`, генерирует адаптеры для указанных агентов и создаёт исполняемые шимы в `.agents/bin/` для скиллов с командами.

5. Проверьте доступность скилла в подключённом агенте:

   ```bash
   claude
   ```

   Результат: агент считывает инструкции из адаптера `.claude/skills/` и применяет правила скилла `skill-tracker` в текущей сессии.

### Приватные репозитории сборки: за один Enter

Скомпилированные команды скилла могут собираться из отдельно запиненного git-репозитория. Если репозиторий приватный, при первой установке `csk` показывает обнаруженные варианты (ssh-agent, ваши `*.pub`): вы выбираете вариант на терминале (по умолчанию предлагается вариант "агент + пин ключа"), указываете скоуп, и выбор сохраняется в конфиг. В CI то же самое задаётся заранее:

```bash
csk config build-ssh add gitlab.example.com/portals/infra \
    --agent auto --identity ~/.ssh/work.pub
```

Приватный репозиторий сборки работает и по SSH, и по HTTPS. Для HTTPS ничего заводить не нужно, если вы уже клонируете по HTTPS: `csk` предложит переиспользовать ваши собственные креды Git при первой установке за один Enter. Для неинтерактивного случая выполните команду:

```bash
csk config build-https add gitlab.example.com/portals/infra --token-source git-credentials
```

В CI задайте переменную `CSK_BUILD_HTTPS_TOKEN` (и `CSK_BUILD_HTTPS_HOST`, если репозитории сборки живут на разных хостах).

Пакет скилла выбрать креды не может: выбор делает только оператор и только явно. Подробности устройства брокеров кредов и скоупов описаны в [`docs/external-build-repositories.md`](docs/external-build-repositories.md).

## Режимы установки скиллов

Установщик поддерживает три режима установки скиллов в зависимости от зоны ответственности и способа распространения файлов.

### Проектный режим

Проектный режим фиксирует скиллы в файле `Skillfile.json` в корне репозитория. Разработчики коммитят этот файл в систему контроля версий. Вызов `csk install` на любой машине разворачивает одинаковый набор скиллов для всех участников команды.

Проектный `Skillfile.json` поддерживает schema 1, 2 и 3.

Схема 2 является основным форматом проектного Skillfile. Она объявляет источники и выбирает скиллы через селекторы. Команды `csk check`, `csk install`, `csk upgrade` и `csk status` поддерживают её по умолчанию.

Пример `Skillfile.json` схемы 2:

```json
{
  "schema_version": 2,
  "project": { "alias": "demo-ios" },
  "agents": ["claude_code", "codex_cli", "cursor"],
  "sources": {
    "local": { "path": "." }
  },
  "skills": [
    {
      "name": "skill-tracker",
      "from": "local",
      "directory": "skills/skill-tracker"
    }
  ]
}
```

При работе со схемой 2 команда `csk install` выполняет начальную установку или устанавливает по файлу `Skillfile.lock.json`, команда `csk upgrade` явно обновляет лок, а `csk status` показывает актуальность манифеста и лока без записи. Команда `csk check` проверяет Skillfile без установки. Сетевые источники подчиняются файлу `source-policy.json`, который находится в `~/.cocoaskills/source-policy.json` (путь переопределяется через `CSK_SOURCE_POLICY`). Подробное описание работы со схемой 2 приведено в [`docs/skillfile-sources.md`](docs/skillfile-sources.md).

Коммитьте `Skillfile.lock.json` вместе со `Skillfile.json`. На новой машине `csk install` восстанавливает отсутствующий снимок. Для `path` команда читает текущие байты. Для `git` и `repository` она получает точный commit из лока. Установка продолжится при совпадении идентичности и `content_sha256`. Расхождение вызывает `source_snapshot_changed`. Недоступный источник вызывает `source_snapshot_unavailable` и указывается в сообщении. Восстановление не меняет лок и не разрешает теги или ветки. `csk upgrade` явно обновляет лок по текущим источникам.

Схема 1 остаётся поддерживаемым legacy-форматом. Она напрямую объявляет список скиллов в секции `skills`:

```json
{
  "schema_version": 1,
  "project": { "alias": "demo-ios" },
  "agents": ["claude_code", "codex_cli", "cursor"],
  "skills": [
    {
      "name": "skill-tracker",
      "git": "git@gitlab.example.com:skills/skill-tracker.git",
      "tag": "v1.0.0"
    }
  ]
}
```

Schema 3 добавляет `mcp_servers` к schema 2. Для URL-only сервера задайте project Skillfile:

```json
{
  "schema_version": 3,
  "agents": ["claude_code", "codex_cli", "cursor"],
  "skills": [],
  "mcp_servers": { "docs": "https://mcp.example.com/mcp" }
}
```

`csk check` проверяет schema 3 и MCP declarations без записи и сети. После `csk init` обычный `csk install` запишет native configs трёх агентов и покажет каждую запись; точные ignore entries для создаваемых MCP configs добавляются автоматически, без deprecated флага. Consent запрашивается после preflight и сохраняется вместе с успешной публикацией. URL без env reference не требует consent. Объектная форма содержит `server` и необязательный `install` с локальными параметрами записи и аутентификации устанавливающего менеджера. Без `server.environments` используются resolved agents проекта; явный writable id записывается даже вне этого набора. Известные unsupported ids дают строку `skipped: unsupported` без пути, а в status — `unsupported`, не влияющий на `--check`. Неизвестные ids, пустые списки и дубликаты запрещены. Добавление writable агента требует нового consent; unsupported ids в consent subject не входят. Managed sessions других environment managers пока не видят эти configs. Подробности: [MCP в project Skillfile](docs/skill-authoring.md#project-mcp-schema-3).

MCP install verifies effective Git ignore rules and output-parent readiness before consent. Conflicting nested ignore rules refuse without publication. Runner/evaluator tokens in `args` also refuse, including aliases and data such as `--tool node`; see the linked MCP declaration rules.

Staged ignore checks preserve the live `core.excludesFile` source (including relative paths), `.git/info/exclude` and default global excludes.

Pin and no-eval rules cover recognized runners/evaluators. Known evaluator-capable
commands (including awk/sed, interpreters, editors, debuggers and command-launching
OS tools) refuse even with plain script arguments. Within each argument, csk splits
on characters outside `[A-Za-z0-9_.+-]`, ignores case, strips path prefixes and
`.exe`/`.cmd`/`.bat`/`.ps1`/`.com`, and refuses recognized names as words.
Thus `a=npx pkg`, `system("npx pkg")`, `--exec=npx` and data containing the word
`node` refuse; `--node-id` and `my-server --port 1` remain valid. This intentionally
fails closed on false positives. Direct supported pinned `npx`/`uvx` remain valid.
The shared inventory includes system and package aliases such as `view`,
`vimdiff`, `rview` and `editor`, including words inside arguments. In addition,
`check` and install inspect the current host PATH relative to the project.
Kernel `stat` and executable-access checks select the first regular executable,
keeping symlinks, case aliases, separators and parent components intact. Its
filesystem identity is compared with recognized evaluator names found in PATH
and the default system directories. Matching symlinks and hard links refuse
with `mcp_command_invalid`. Only the selected candidate's first 256 bytes may
be read, through that candidate path, to recognize a shebang interpreter by name
or identity; interpreter bodies are not read. No command is executed. Missing,
unreadable or otherwise unrecognized targets retain the portable table result.
Supported runner recipes retain their expected interpreter substrate.
This is a point-in-time host check: concurrent directory/executable replacement
or another agent's later PATH can differ, and filesystem identities are not
frozen. A same-user attacker who can write PATH directories already controls the
agent and is outside this boundary. It does not inspect arbitrary binaries or
prove what an opaque program runs.

Any remaining bare program runs as is: csk cannot verify what it runs. Consent shows
its exact command and argument array and states this limit. This is no sandbox or
proof about arbitrary programs; non-default evaluators outside the recognized
inventory and hidden execution semantics remain the documented limit.


### Глобальный режим

Глобальный режим устанавливает скиллы один раз на компьютере пользователя в каталог `~/.cocoaskills/global/`. Глобальные скиллы работают во всех каталогах вне зависимости от наличия git-репозитория и файла `Skillfile.json`.

Команда добавляет глобальный скилл в конфигурацию пользователя:

```bash
csk global add skill-metrics --git git@gitlab.example.com:skills/skill-metrics.git --tag v2.1.0
```

Команда `csk global install` скачивает репозиторий и записывает адаптеры в пользовательские директории агентов в домашнем каталоге.

### Гибридный режим

Гибридный режим объявляет скиллы один раз на машине в файле `~/.cocoaskills/hybrid/Skillfile.json` и активирует их для целевых проектов по алиасу, пути или глоб-шаблону. Установщик создаёт адаптеры в директориях агентов проекта (`.claude/skills/`, `.codex/skills/`) и шимы команд в `.agents/bin/`, но не требует коммитов в git-репозиторий. Платформенные команды используют гибридный режим для раскатки процессных правил и рабочих процессов на выбранные репозитории.

Команда связывает гибридный скилл с проектом по его алиасу:

```bash
csk hybrid add workflow-lint --git git@gitlab.example.com:skills/workflow-lint.git --tag v1.2.0 --target "demo-ios"
```

При запуске `csk install` внутри проекта `demo-ios` установщик проверяет правила в `~/.cocoaskills/hybrid/Skillfile.json`, находит совпадение по алиасу `demo-ios` и подключает скилл `workflow-lint` в локальный контекст агентов.

### Порядок перекрытия скиллов

При совпадении имён скиллов в разных режимах установщик применяет следующий порядок приоритета: проектный режим перекрывает гибридный режим, а гибридный режим перекрывает глобальный режим (`проектный > гибридный > глобальный`).

## Команды

Раздел содержит обзор основных команд `csk` по пяти функциональным группам.

<details>
<summary>Проект</summary>

```bash
csk init [path]                     # Инициализирует Skillfile.json и .gitignore в проекте
csk add <name>                      # Добавляет или обновляет объявление скилла в Skillfile.json
csk remove <name>                   # Удаляет объявление скилла из Skillfile.json
csk status [target]                 # Показывает статус установленных скиллов и манифеста
csk check [target]                  # Проверяет Skillfile без установки
csk list                            # Выводит список зарегистрированных проектов и скиллов
csk project add <alias> <path>      # Регистрирует путь проекта в глобальной конфигурации
csk project resolve [target]        # Показывает резолюцию манифеста и целевых путей
```

</details>

<details>
<summary>Скиллы и зависимости</summary>

```bash
csk install [target]                # Устанавливает объявленные скиллы по локальным git-ссылкам
csk update                          # Выполняет git fetch для всех источников в skills_root
csk upgrade [target]                # Подтягивает обновления источников и выполняет установку
csk skill check <dir>               # Проверяет валидность структуры скилла и SKILL.md
```

</details>

<details>
<summary>Global и Hybrid</summary>

```bash
csk global init                     # Создаёт глобальный манифест ~/.cocoaskills/global/Skillfile.json
csk global add <name>               # Добавляет объявление скилла в глобальный манифест
csk global remove <name>            # Удаляет скилл из глобального манифеста
csk global list                     # Выводит список объявленных глобальных скиллов
csk global status                   # Показывает статус установки глобальных скиллов
csk global install                  # Устанавливает глобальные скиллы в профиль пользователя
csk global update                   # Подтягивает git-источники глобальных скиллов
csk global upgrade                  # Подтягивает git-источники и устанавливает глобальные скиллы
csk hybrid add <name>               # Связывает гибридный скилл с целевыми проектами
csk hybrid remove <name>            # Удаляет объявление гибридного скилла
csk hybrid list                     # Выводит список гибридных скиллов и привязок
csk hybrid status                   # Показывает статус гибридных скиллов и хранилища
```

</details>

<details>
<summary>Сборки и аудит</summary>

```bash
csk audit [target]                  # Запускает статический аудит безопасности скиллов
csk gc                              # Очищает неиспользуемые runtime-записи и кэш сборок
```

</details>

<details>
<summary>Сервисные</summary>

```bash
csk bootstrap                       # Создаёт глобальную конфигурацию ~/.cocoaskills/config.json
csk config show                     # Выводит путь и содержимое текущей конфигурации
csk shell-init                      # Генерирует или устанавливает код хука shell для PATH
```

</details>

Полное описание команд, флагов, позиционных аргументов и примеров использования находится в файле [`docs/cli.md`](docs/cli.md).

## Источники Skillfile schema 2

Схема 2 является основным форматом проектного Skillfile и поддерживается командами `csk check`, `csk install`, `csk upgrade` и `csk status` без дополнительной настройки. Схема 1 остаётся поддерживаемым legacy-форматом.

Настройка `experimental.skillfile_sources` и переменная `CSK_EXPERIMENTAL_SKILLFILE_SOURCES` сохраняют совместимость со старыми конфигурациями и не влияют на выбор схемы.

Команды переиспользуют общий интерфейс: `csk install` выполняет начальную установку или установку по локу, `csk upgrade` явно обновляет лок, `csk status` показывает актуальность без записи, а `csk check` проверяет Skillfile без установки. Команды установки читают источники `path` с диска, а источники `git` и `repository` получают через ограниченный транспорт по машинной политике `source-policy.json`. Руководство для авторов и операторов находится в [`docs/skillfile-sources.md`](docs/skillfile-sources.md). Историческое решение зафиксировано в [RFC 0009](docs/v0.16-design.md).

## Development tools

Перед каждым запуском git csk выбирает и проверяет CA bundle, затем закрепляет
приватную копию оставшихся исходных PEM-блоков на время операции. CA из
встроенного списка отказа удаляются; предупреждение `git_trust_anchor_distrusted`
один раз за процесс называет CA и исходный путь. Пустой после фильтрации или
повреждённый bundle вызывает `git_trust_anchor_invalid`. Проверка охватывает SSH,
локальные команды и URL rewrites, которые могут привести к HTTPS.
Собственный актуальный PEM-файл задаётся через `CSK_GIT_CA_BUNDLE`
или `git.https_ca_bundle` глобальной конфигурации.
[Порядок выбора и отказы](docs/reference.md#https-токены-внешних-репозиториев-сборки).

- `uv` installs the development environment and builds release artifacts. Run
  `uv sync --extra dev` and `uv build --out-dir .temp/dist`; build outputs stay
  under `.temp/dist/`.
- `pytest` and `pytest-xdist` run the test suite. Run `uv run pytest -q` for the
  full suite or focused test paths; temporary output belongs under `.temp/` or
  an external short-lived temp directory.
- `Go` собирает команды `go-v1` выбранным оператором `go` из `PATH`;
  проверьте его командой `go version`. Квалифицированы семейства 1.26 (от
  1.26.10) и 1.27 (от 1.27.3). Более ранние и prerelease-версии этих семейств
  отклоняются с `insecure_go_release`, семейства 1.25 и старше — с
  `unsupported_go_family`. Строка `remediation:` предлагает поставить Go
  1.27.3 или новее первым в `PATH`: например, `go = "1.27.3"` в проектном
  `.mise.toml` либо `brew upgrade go`. Порог нужен для исправленной проверки
  TLS-сертификатов: собранные CLI включают стандартную библиотеку выбранного Go.
  Новая версия тулчейна меняет cache key; следующая установка или обновление
  пересобирает CLI. Семейства новее 1.27 принимаются с предупреждением;
  строгий отказ задаётся через `builds.go_future_families = "refuse"` или
  `CSK_GO_FUTURE_FAMILIES=refuse`. CI закрепляет Go 1.26.10 и 1.27.3 через
  `actions/setup-go@v7` в `.github/workflows/ci.yml`; кэш сборок находится в
  `<csk-home>/builds/go-v1/`.
- `mypy` performs strict source type checking. Run `uv run mypy`; it writes no
  persistent artifact unless output is redirected to `.temp/`.
- `uv run python tests/audit_secrets_mutants.py` runs the audit security
  narrowing mutants in disposable copies, including review regressions, and names each failing regression.
  Select a focused set with `uv run python tests/audit_secrets_mutants.py raw-echo-if`
  or run `uv run pytest -q tests/test_usage_error_closure.py` for the parser
  closure regressions. Save output under `.temp/<TASK-ID>/`.
- `uv run python tests/repointed_tags_mutants.py` checks fetch and strict-tag
  narrowing mutants against the real CLI regressions in disposable copies.
  Logs and the named failing tests go under `.temp/repointed-tags-mutants/`.
- Launcher PATH has a manager-owned prefix and a dependency suffix. The prefix
  holds only the skill bin directory and the manager interpreter directory and
  never depends on manifest content. Dependency directories resolve through the
  manager's `tool_paths` resolver and are appended after the inherited PATH, so
  the caller's PATH always wins. Run `uv run pytest -q
  tests/test_launcher_tool_paths.py tests/test_tool_paths.py
  tests/test_tool_paths_symlinks.py tests/test_shims.py` to check prefix
  independence, shim exclusion, symlink chains, refusal before publication,
  suffix order, and legitimate dependency launch. Save validation logs under
  `.temp/`.
- `PyYAML` используется только в dev extra как независимый oracle для
  дифференциальных тестов frontmatter. Запускайте
  `uv run pytest -q tests/test_frontmatter_differential.py`; постоянные
  артефакты теста не создаются.
- `actionlint` validates GitHub Actions workflows. Run
  `actionlint .github/workflows/ci.yml`; validation logs belong under `.temp/`.
- `.scripts/release.sh` cuts and publishes signed stable releases. Run
  `.scripts/release.sh 0.18.4 --wb-notes .temp/wb-release-0.18.4.md` from a
  clean, current `main` checkout; use `--dry-run` to print every release step.
  Use `.scripts/release.sh 0.20.0 --no-wb` to defer the mirror without requiring
  `glab` or notes, then publish only that stage later with
  `.scripts/release.sh 0.20.0 --resume-from wb --wb-notes .temp/wb-release-0.20.0.md`.
  If distribution smoke fails, run its printed `gh run rerun <run-id> --failed`
  command manually, then resume with
  `.scripts/release.sh 0.18.4 --resume-from verify`.
  It requires `gh`, `delivery`, a loaded SSH signing key, `curl`, and
  `mise`; `glab` is required only when publishing the mirror stage.
  See [`RELEASING.md`](RELEASING.md) for the gates, Wildberries steps,
  and output behavior. It creates a local release branch and signed tag;
  published build artifacts stay in GitHub Actions and no local release log is
  retained.
- GitHub Actions runs the cross-platform CI matrices. The merge protocol lane
  keeps full Linux/macOS coverage and uses the checked-in `.research/` manifest,
  classification, and verifier for six deterministic Windows shards. Each
  Windows shard uploads its collected inventory, selected node IDs, verifier
  provenance, and JUnit XML as a workflow artifact.

## Дальше

Документация и справочные материалы CocoaSkills:

- [`docs/cli.md`](docs/cli.md): справочник команд `csk`, флагов и кодов завершения.
- [`docs/reference.md`](docs/reference.md): справочник по матрице установки, зависимостям скиллов, манифестам и аудиту безопасности.
- [`docs/external-build-repositories.md`](docs/external-build-repositories.md): устройство внешних репозиториев сборки, брокеров кредов SSH/HTTPS и моделей доступа.
- [`docs/design/linux-go-v1.md`](docs/design/linux-go-v1.md): Linux source-build design proposal, protocol requirements, qualification gates, and pending native probes.
- [`docs/skillfile-sources.md`](docs/skillfile-sources.md): источники Skillfile schema 2, машинная транспортная политика и семантика лока.
- [`docs/v0.16-design.md`](docs/v0.16-design.md): RFC 0009, запись проектного решения о формате источников schema 2 (на английском).
- [`ARCHITECTURE.md`](ARCHITECTURE.md): описание внутренней архитектуры, схемы работы конвейера установки, формата хранилищ и модели безопасности.
- [`SECURITY.md`](SECURITY.md): политика безопасности, границы изоляции и рекомендации по настройке.
- [`docs/skill-authoring.md`](docs/skill-authoring.md): руководство по структурированию пакетов скиллов, объявлению команд и настройке манифеста `agent-skill.json`.
- [`docs/authoring-cli-commands.md`](docs/authoring-cli-commands.md): руководство по поставке CLI-утилит в скиллах (Go, скрипт-команды, ограничения компилятора и планируемые языки).
- [`docs/troubleshooting.md`](docs/troubleshooting.md): диагностика ошибок установки, миграция со старых версий и решение частых проблем.
- [`CHANGELOG.md`](CHANGELOG.md): история релизов и список изменений по версиям.

## Лицензия

Проект распространяется на условиях лицензии [Apache 2.0](LICENSE).

### MCP alias inventory tool

Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src uv run python .scripts/scan_mcp_evaluator_aliases.py > .temp/macos-evaluator-aliases.json`
from the repository to audit system executable aliases without running them.
The scanner reads `/usr/bin`, `/bin`, `/usr/sbin` and `/sbin`; its dated output
records symlink, inode, shebang and literal `exec` evidence, plus unreadable-file
limits. Reviewed snapshots and package source links live in
`tests/fixtures/mcp/macos-evaluator-aliases.json` and
`tests/fixtures/mcp/packaged-evaluator-aliases.json`. Restricted editor aliases
and scripts interpreted by a refused program are conservatively refused too.
