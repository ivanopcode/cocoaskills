# CocoaSkills

[![PyPI](https://img.shields.io/pypi/v/cocoaskills.svg)](https://pypi.org/project/cocoaskills/)
[![Python versions](https://img.shields.io/pypi/pyversions/cocoaskills.svg)](https://pypi.org/project/cocoaskills/)
[![License](https://img.shields.io/pypi/l/cocoaskills.svg)](https://github.com/ivanopcode/cocoaskills/blob/main/LICENSE)
[![CI](https://github.com/ivanopcode/cocoaskills/actions/workflows/ci.yml/badge.svg)](https://github.com/ivanopcode/cocoaskills/actions/workflows/ci.yml)

Перевод английской версии. Источник правды: [README.md](README.md).

`csk` это локальный менеджер скиллов для AI-агентов. Он устанавливает
переиспользуемые пакеты скиллов из git-репозиториев в репозитории ваших
проектов: воспроизводимые установки с контролем целостности по content-hash,
зависимости скилл-от-скилла и поддержку шести агентских сред: Claude Code,
Codex CLI, Cursor и Gemini через адаптеры-зеркала, плюс OpenCode и Windsurf,
которые читают каноническую директорию `.agents/skills/` нативно.

## Зачем

Ручное управление скиллами во многих проектах быстро разваливается: дрейф
между машинами, отсутствие пиновки версий, README и тесты протекают в контекст
агента, после удаления скилла остаётся мусор.

CocoaSkills делает попроектную установку скиллов декларативной и
воспроизводимой:

- Один `Skillfile.json` на проект, коммитится в систему контроля версий.
- Пиновка git-ссылок (tag / branch / revision) и установки с content-hash.
- Зависимости скилл-от-скилла: скилл объявляет скиллы, на которых строится, а
  `csk install` разрешает транзитивное замыкание с точными ссылками и режимами
  активации.
- Установка по whitelist: README, тесты, build-файлы и прочее не-скилловое
  содержимое остаются вне контекста агента.
- Одно каноническое место (`.agents/skills/`) с адаптерами на каждую среду:
  symlink или копия в `.claude/skills/`, `.codex/skills/`, `.cursor/rules/`,
  `.gemini/skills/`. OpenCode и Windsurf читают `.agents/skills/` нативно,
  зеркало им не нужно.
- Команды скиллов доступны через проектную директорию `.agents/bin/` на
  `PATH`.
- Опциональные глобальные скиллы ставятся один раз в `~/.cocoaskills/global/`
  и видны поддерживаемым агентам вне любого чекаута проекта.

## Установка

Выберите способ под свою машину. `pipx` рекомендуется на любой платформе.

### pipx (рекомендуется)

```bash
pipx install cocoaskills
```

### uv tool

```bash
uv tool install cocoaskills
```

### Homebrew (macOS, Linux)

```bash
brew tap ivanopcode/csk
brew install cocoaskills
```

### mise

```bash
mise use -g pipx:cocoaskills@latest
```

### Установочный скрипт

```bash
curl -fsSL https://cocoaskills.org/install.sh | sh
```

Скрипт находит Python, предпочитает `pipx` или `uv tool` и откатывается к
`pip install --user`. Прочитайте его перед запуском, если не доверяете сети.

### Обычный pip

```bash
python -m pip install --user cocoaskills
```

## Быстрый старт

1. Выберите или создайте директорию для git-репозиториев скиллов. Пример:
   `~/agents/skills/`. Существующие локальные репозитории читаются из этой
   директории; отсутствующие клонируются автоматически, когда декларация
   скилла содержит `git`.

2. Создайте машинный конфиг:

   ```bash
   csk bootstrap
   ```

   Команда записывает `~/.cocoaskills/config.json`: `skills_root`,
   предпочитаемую локаль и агентские среды по умолчанию.

3. Инициализируйте CocoaSkills в каждом проекте:

   ```bash
   cd /path/to/project
   csk init
   ```

   Команда создаёт `Skillfile.json` и добавляет генерируемые CocoaSkills пути
   в `.gitignore`.

4. Объявите нужные скиллы:

   ```json
   {
     "schema_version": 1,
     "project": { "alias": "demo-ios" },
     "agents": ["claude_code", "codex_cli", "cursor"],
     "locale": "en",
     "skills": [
       {
         "name": "skill-tracker",
         "git": "git@gitlab.example.com:skills/skill-tracker.git",
         "tag": "v1.0.0"
       },
       {
         "name": "skill-metrics",
         "source": "internal/skill-metrics",
         "branch": "main"
       }
     ]
   }
   ```

   Необязательное поле `locale` влияет только на скиллы с локализованными
   метаданными (`locales/metadata.json` плюс `.skill_triggers/<locale>.md`).
   Скиллы без файлов локализации ставятся без изменений.

5. Запустите `csk install` внутри чекаута.

Для синхронизации нескольких проектов явно зарегистрируйте их через
`csk project add` и запускайте `csk install --all` или `csk upgrade --all`.

## Зависимости скиллов

Начиная с v0.9.0 скилл может требовать другие скиллы
([RFC 0007](docs/v0.9-design.md)). Требование живёт в `csk-skill.json`
schema v4 в блоке `dependencies.skills`, самодостаточно (git URL плюс точный
`tag` или `revision`) и несёт режим активации:

```json
{
  "schema_version": 4,
  "runtime_roots": ["scripts"],
  "capabilities": { "exec": ["trk", "git"], "network": "none" },
  "commands": {
    "report": { "type": "script", "unix_path": "scripts/report" }
  },
  "dependencies": {
    "skills": {
      "skill-tracker": {
        "git": "git@gitlab.example.com:skills/skill-tracker.git",
        "ref": { "kind": "tag", "value": "v1.4.2" },
        "mode": "runtime",
        "commands": ["trk"]
      }
    }
  }
}
```

Режимы активации определяют, что провайдер даёт потребителю:

- `full` (по умолчанию) активирует промпт-контекст провайдера и все
  экспортируемые команды.
- `runtime` активирует только команды; необязательный список `commands` сужает
  активацию до названных экспортов.
- `context` активирует только промпт-контекст провайдера.

`csk install` разрешает транзитивное замыкание: провайдеры загружаются,
унифицируются до одного commit и одного канонического источника на имя,
устанавливаются раньше потребителей и проходят аудит вместе. Конфликты версий,
конфликты источников и циклы зависимостей падают с полными цепочками
требований.

Рабочий процесс поставляется как скилл, который объявляет требования и
экспортирует ноль команд; потребитель устанавливает весь состав одной записью
в `Skillfile.json`.

Два вспомогательных механизма:

- `Skillfile.dev.json` подменяет провайдеров локально во время разработки:
  путь к чекауту или git-ссылка, включая ветки. Файл остаётся вне версионного
  контроля, установка печатает каждую активную подмену, строгий аудит
  отказывает подменённым установкам.
- `allowed_sources` в `~/.cocoaskills/config.json` перечисляет канонические
  префиксы `host/path` и проверяет каждое клонирование. SSH- и HTTPS-адреса
  одного репозитория нормализуются в одну идентичность.

## Глобальные скиллы

Глобальные скиллы это пользовательский базовый набор. Они ставятся в
`~/.cocoaskills/global/` и линкуются в пользовательские директории агентов,
например `~/.claude/skills/` и `~/.codex/skills/`. Если среди целевых сред
есть OpenCode или Windsurf, глобальные скиллы линкуются также в
`~/.agents/skills/`, которую обе среды находят нативно.

```bash
csk global init
csk global add skill-metrics \
  --git git@gitlab.example.com:skills/skill-metrics.git \
  --tag v1.0.0
csk global install
```

Глобальные команды доступны через `~/.cocoaskills/global/bin`. Во время
`csk global install` CocoaSkills также публикует перенаправляющие shim-ы в
безопасный пользовательский bin, уже присутствующий на `PATH`, например
`~/.local/bin`, поэтому глобальные команды работают из любой директории без
попроектной активации.

Если безопасного пользовательского bin нет, установка завершается успешно и
печатает предупреждение. В этом случае добавьте `~/.cocoaskills/global/bin` в
`PATH`, задайте `CSK_GLOBAL_USER_BIN` с записываемой директорией из `PATH` или
установите shell-hook:

```bash
eval "$(csk shell-init zsh)"
```

Внутри проекта shell-hook по-прежнему важен для затенения команд: shim-ы из
`.agents/bin` должны идти раньше глобальных. Проектные скиллы с тем же именем
затеняют глобальные. Глобальные скиллы никогда не заменяют закоммиченные
декларации проектного `Skillfile.json`.

## Манифесты команд скилла

Скиллы объявляют команды, capabilities и зависимости через `csk-skill.json`.
Schema v2 поддерживает многофайловые runtime: `runtime_roots` копируются в
`~/.cocoaskills/runtime/<skill>/<commit>/` и исключаются из промпт-контекста
агента. Schema v3 добавляет envelope `capabilities`, который используют
`csk audit` и строгие гейты установки. Schema v4 добавляет требования скиллов
(см. [Зависимости скиллов](#зависимости-скиллов)).

```json
{
  "schema_version": 4,
  "runtime_roots": ["scripts"],
  "capabilities": {
    "network": ["gitlab.example.com"],
    "filesystem": "repo",
    "exec": ["review-cli"],
    "secrets": "none",
    "env_read": ["HOME"],
    "prompt_scope": "Review merge request metadata and produce local advice."
  },
  "commands": {
    "mr": {
      "type": "script",
      "unix_path": "scripts/mr"
    },
    "review-cli": {
      "type": "system",
      "command": "review-cli",
      "hint": "Install the review CLI through project bootstrap tooling"
    }
  }
}
```

Команды `system` только проверяются через `shutil.which`; CocoaSkills никогда
не устанавливает системные инструменты, а манифесты не несут install-хуков и
проб версий.

## Аудит скиллов

`csk audit` запускает проверки безопасности на том же закоммиченном снапшоте
скилла, который использовал бы `csk install`. Статические детекторы работают
всегда. Опциональные backend-ы `command` и `codex` извлекают дополнительные
структурированные находки; решение об установке остаётся детерминированным
внутри CocoaSkills.

```bash
csk audit
csk audit . --json
csk audit --global
```

Гейты установки включаются на команду или через конфиг:

```bash
csk install --audit
csk install --audit strict
csk global install --audit
```

Advisory-аудит печатает предупреждения и продолжает. Строгий аудит блокирует
находки на настроенном пороге и выше. Скиллы schema v1/v2 не объявляют
capabilities; строгий аудит требует миграции на schema v3 или новее либо
пиновки content-hash через trust-механизм, когда он включён.

Правила безопасности backend-ов:

- Локальные `command`-backend-ы получают сырые файлы скилла и считаются
  доверенными локальными инструментами.
- Локальные `codex`-backend-ы требуют `oss=true` и явный `local_provider`.
- Облачные backend-ы требуют `audit.allow_cloud=true` и совпадения публичной
  source policy. Содержимое файлов редактируется до отправки в облачный
  backend.
- Неверифицируемые находки backend-ов показываются в отчётах и никогда не
  блокируют строгие установки.

## CLI

| Команда | Поведение |
|---|---|
| `csk bootstrap` | Создать машинный глобальный конфиг; интерактивно или скриптово через `--skills-root`, `--default-agents`, `--non-interactive`, `--force`. |
| `csk init [path]` | Создать проектный `Skillfile.json` и managed-блок `.gitignore`. Поддерживает `--alias`, `--agents` и `--no-interactive` для скриптовой настройки. |
| `csk install [target]` | Применить `Skillfile.json` по текущим git-ссылкам. Отсутствующие источники с `git` URL клонируются в `skills_root`; существующие локальные репозитории не фетчатся. Без target действует текущий проект; `target` может быть алиасом, `.` или путём проекта. |
| `csk install --audit [strict]` | Запустить гейт аудита только для этой установки. Без `strict` аудит advisory и не меняет конфиг. |
| `csk install --all` | Установить каждый проект, явно зарегистрированный в глобальном конфиге. |
| `csk update` | Зафетчить все git-репозитории в `skills_root`. Проекты не изменяются. |
| `csk upgrade [target]` | Выполнить `update`, затем `install`. |
| `csk upgrade --all` | Выполнить `update`, затем установить каждый зарегистрированный проект. |
| `csk status [target]` | Показать манифест против установленного состояния, включая активные dev-подмены. `--check` завершится ненулевым кодом, если что-то не up-to-date; `--json` печатает машиночитаемый вывод. |
| `csk status --all` | Показать статус каждого зарегистрированного проекта. |
| `csk add <name> --tag/--branch/--revision ...` | Добавить или заменить декларацию скилла в проектном Skillfile; применить через `csk install`. |
| `csk remove <name>` | Удалить декларацию скилла из проектного Skillfile; следующая установка вычищает генерируемые файлы. |
| `csk gc` | Удалить неиспользуемые записи runtime, кэша снапшотов и мёртвые записи реестра потребителей. |
| `csk audit [target]` | Запустить аудит безопасности скиллов для текущего проекта, алиаса, `.` или пути проекта. Поддерживает `--all`, `--global` и `--json`. |
| `csk skill check <dir>` | Проверить одну директорию скилла без глобального конфига и настройки проекта. |
| `csk list [--paths]` | Перечислить настроенные проекты и объявленные скиллы. |
| `csk project add <alias> <path>` | Зарегистрировать проект для `--all` и создать манифест, если его нет. |
| `csk project resolve [target]` | Показать разрешённые алиас проекта, алиас чекаута, Skillfile и пути установки. |
| `csk global init` | Создать пользовательский глобальный `Skillfile.json`, глобальный контекст скиллов, bin и env-файлы. |
| `csk global add <name> --tag/--branch/--revision ...` | Добавить или заменить глобальную декларацию скилла. |
| `csk global remove <name>` | Удалить глобальную декларацию; следующая глобальная установка вычищает генерируемые файлы. |
| `csk global install` | Установить все глобально объявленные скиллы без фетча. |
| `csk global update` | Зафетчить исходные репозитории глобально объявленных скиллов. |
| `csk global upgrade` | Выполнить глобальный update, затем глобальный install. |
| `csk global status` | Показать глобальный манифест против установленного состояния. |
| `csk global list` | Перечислить глобальные декларации скиллов. |
| `csk config show` | Напечатать путь и содержимое разрешённого конфига. |
| `csk shell-init [zsh\|bash\|powershell]` | Напечатать shell-hook для глобальной и проектной авто-активации `PATH`. `--no-global` ограничивает активацию чекаутами проектов. |
| `csk --version` | Напечатать версию и выйти. |

Флаги, общие для `install` и `upgrade`:

- `--dry-run`: спланировать работу без изменения файлов.
- `--verbose`: напечатать разрешённые commit-ы и установленные shim-ы команд.
- `--fix-gitignore`: устаревший обходной путь; предпочитайте `csk init`.
- `--strict-tags`: упасть, если тег был локально перемещён на другой commit.

Коды выхода: `0` успех, `1` один или несколько проектов либо скиллов упали,
`2` ошибка конфигурации, `3` конкуренция за блокировку.

## Разработка

Требуется Python 3.11+.

```bash
git clone https://github.com/ivanopcode/cocoaskills.git
cd cocoaskills
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest
```

Локальная сборка артефактов:

```bash
python -m build
twine check dist/*
```

Runtime-пакет использует только стандартную библиотеку. Версионирование ведёт
`setuptools-scm` по git-тегам; генерируемый `src/csk/_version.py` не
коммитится.

Процесс контрибуции, соглашения по коду и RFC-процесс для изменений дизайна
описаны в [CONTRIBUTING.md](CONTRIBUTING.md).

## Документация

- [Обзор архитектуры](ARCHITECTURE.md): карта модулей, конвейер установки,
  разделение контекста и runtime, раскладка хранилищ и границы безопасности.
- [Зависимости скиллов, RFC 0007](docs/v0.9-design.md): требования schema v4,
  разрешение замыкания, режимы активации, dev-подмены, allowlist источников.
  Русский перевод: [docs/v0.9-design.ru.md](docs/v0.9-design.ru.md).
- [Руководство автора скиллов](docs/skill-authoring.md): практический контракт
  для репозиториев скиллов, совместимых с CocoaSkills: runtime roots schema
  v2, capabilities schema v3, требования schema v4, системные зависимости,
  поведение аудита и релизный чеклист.
- [Аудит безопасности скиллов, RFC 0005](docs/audit-design.md): capabilities
  schema v3, детерминированные гейты аудита, кэш вердиктов и trust-механизм.
- [LLM-backend-ы аудита, RFC 0006](docs/v0.8-design.md): backend-ы `command` и
  `codex`, редактирование содержимого файлов, таймауты и поведение
  fail-open/fail-closed.
- [Спецификация дизайна MVP](docs/mvp-design.md): контракт v0.1; поздние RFC
  заменяют его части.
- [CHANGELOG](CHANGELOG.md): история релизов в формате Keep a Changelog.

## Безопасность

Поддерживаемые версии и процесс сообщения об уязвимостях описаны в
[SECURITY.md](SECURITY.md). Подсистема аудита и её гарантии описаны в
[docs/audit-design.md](docs/audit-design.md).

## Лицензия

Apache-2.0. См. [LICENSE](LICENSE).
