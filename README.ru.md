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
- Команды скиллов доступны через явные project/global shim-пути; shell profile
  и пользовательский `PATH` не являются условием работы агента.
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

   Проектная автоматизация может сделать этот шаг идемпотентным, не
   перезаписывая существующий машинный конфиг разработчика:

   ```bash
   csk bootstrap --if-missing --non-interactive --skills-root ~/.cocoaskills/skills
   csk upgrade .
   ```

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
([RFC 0007](docs/v0.9-design.md)). Требование живёт в `agent-skill.json`
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

Работа агента не зависит от активации shell profile. Установленные скиллы
сначала разрешают `<repo>/.agents/bin/<command>` (`<command>.cmd` на Windows),
затем `<csk-home>/global/bin`, затем проверенную bare-команду. Этот контракт
одинаков для zsh, bash, PowerShell, Git Bash, CI и неинтерактивных процессов.

Сгенерированные runtime-шимы добавляют в начало дочернего `PATH` только
необходимые пути: текущий project/global bin, Python-окружение запущенного
`csk` и каталоги объявленных системных зависимостей. Унаследованный `PATH`
сохраняется, но вызовы между скиллами и Python-launcher не зависят от
shell-hook.

На Windows PowerShell 5.1, PowerShell 7 и `cmd.exe` выполняют `.cmd` shim-ы
напрямую. Опциональная автоматическая активация доступна в PowerShell и Git
Bash; `cmd.exe` profile-hook для работы агента не нужен.

Если безопасного пользовательского bin нет, установка завершается успешно и
печатает предупреждение. Агент продолжает использовать явный global-путь.
Человек может задать `CSK_GLOBAL_USER_BIN` или вызвать shim явно.

Shell-hook является только опциональным удобством для bare project-команд.
Команда без аргумента сама определяет zsh/bash, Git Bash или PowerShell:

```bash
csk shell-init --install
# Явный выбор: zsh, bash, powershell
```

Команда атомарно кэширует hook и печатает корректную строку подключения для
`.zshrc`, `.bashrc` или PowerShell profile. Не добавляйте в profile
`eval "$(csk shell-init ...)"`: это запускает Python на старте каждого shell.
После обновления CocoaSkills повторите `--install`, чтобы обновить опциональный
кэш.

Переменная `CSK_AUTO_ENV=0`, заданная до загрузки hook, отключает обход
проектных директорий на зависшем или нездоровом filesystem. Глобальные команды
останутся активны, а project shim останется доступен по явному пути. Глобальные
скиллы никогда не заменяют закоммиченные декларации проектного
`Skillfile.json`.

## Манифесты команд скилла

Скиллы объявляют команды, capabilities и зависимости через `agent-skill.json`.
Schema v2 поддерживает многофайловые runtime: `runtime_roots` копируются в
`~/.cocoaskills/runtime/<skill>/<commit>/` и исключаются из промпт-контекста
агента. Schema v3 добавляет envelope `capabilities`, который используют
`csk audit` и строгие гейты установки. Schema v4 добавляет требования скиллов
(см. [Зависимости скиллов](#зависимости-скиллов)), schema v5 — требования к
MCP-серверам, а schema v6 — компилируемые команды и исключённые из контекста
`build_roots`.

Существующие пакеты с именем `csk-skill.json` остаются читаемыми. Новые и
обновляемые пакеты должны записывать только `agent-skill.json`. При поэтапном
переименовании оба файла могут временно сосуществовать только с одинаковыми
JSON-значениями; конфликтующие файлы блокируют установку.

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

Команды `system` только проверяются через `shutil.which`; CocoaSkills никогда
не устанавливает системные инструменты, а манифесты не несут install-хуков и
проб версий.

## Скомпилированные команды (schema 6)

Этот раздел описывает принятую schema-6 границу `go-v1` из
[protocol core rc.5](https://github.com/relux-works/curator-spec/blob/v1.0.0-rc.5/protocol/core.md)
и соответствующее landed-поведение csk. Более поздние ревизии протокола
находятся вне скоупа.

Пример выше — полный смешанный манифест команд: `format-report` является
скриптовым runtime, `repo-report` собирается из исходного кода Go, а `git` —
предоставленное оператором системное требование в принимаемом compatibility-
расположении. Новые скиллы помещают потребляемые системные инструменты в
`dependencies.commands`; csk только проверяет их наличие и не создаёт для них
launcher. Дерево исходников имеет такой вид:

```text
agent-skill.json
scripts/format-report
scripts/format-report.cmd
build/go.mod
build/cmd/repo-report/main.go
build/vendor/                    закоммиченные модули при импорте нестандартных пакетов
```

Каждая запись `build_roots` — реальная, не содержащая ссылок переносимая
относительная директория. Build roots уникальны и не пересекаются друг с
другом или с `runtime_roots`; каждая должна использоваться хотя бы одной
build-командой. `source_dir` — реальная директория без ссылок ровно под одним
build root. Этот build root содержит `go.mod` непосредственно и обязан быть
ближайшим корнем модуля: промежуточный `go.mod` является ошибкой. Build roots
остаются в проверенном сыром снапшоте исходников, но не попадают ни в
установленный промпт-контекст, ни в хранилище скриптового runtime.

У build-команды ровно три поля:

```json
{"type":"build","driver":"go-v1","source_dir":"build/cmd/repo-report"}
```

Пакет не может выбирать выходной путь, программу, аргументы, окружение,
build-теги, флаги, toolchain, target, build-скрипт, hook, plugin, generator или
post-build действие. `go-v1` — единственный принимаемый driver; другой driver
отклоняется без fallback. Единственный результат — вычисленный менеджером
`bin/<command>` на Unix или `bin/<command>.exe` на Windows.
Fail-closed поведение не даёт package data выбрать нереализованный execution
contract или смешать artifacts, построенные с разной cache semantics.

### Фиксированный контракт `go-v1`

Протокол задаёт Go 1.23 как минимальное семейство, которое менеджер вправе
поддерживать. Одновременно он требует принимать только доверенное оператором
семейство, для которого есть handoff evidence. Текущая реализация CocoaSkills
принимает только семейство Go 1.25. Поэтому Go 1.23 — нижняя граница протокола,
а не утверждение, что этот `csk` принимает каждое семейство от 1.23 и выше.

Выбранная установка Go должна быть предоставленным оператором,
фингерпринтируемым нативным toolchain. Хеширование полного `GOROOT` ограничено
deadline в 600 секунд на проход; оператор поднимает его до 3600 секунд через
`CSK_GO_FINGERPRINT_TIMEOUT`, когда холодный каталог Go читается медленно —
обычно на Windows, за on-access антивирусом. Превышение deadline отклоняет
toolchain с `go-v1 toolchain_timeout` и называет эту переменную в
сообщении об ошибке; поднятие deadline никогда не допускает toolchain,
который был бы отклонён иначе. CocoaSkills собирает ровно один
исполняемый `package main` для host `GOOS` и `GOARCH`. Менеджер выключает
телеметрию Go, использует приватные корни конфигурации, кэша и временных
файлов, фиксирует `GOTOOLCHAIN=local`, `GOENV=off`, `GOWORK=off`,
`CGO_ENABLED=0` и `GO_EXTLINK_ENABLED=0` и запускает только такие
source-aware команды из объявленной `source_dir`:

```text
go list  -mod=vendor -deps -json -buildvcs=false -compiler=gc -pgo=off .
go build -mod=vendor -trimpath -buildvcs=false -buildmode=exe -compiler=gc \
         -pgo=off -ldflags="-linkmode=internal -libgcc=none" -o <private-output> .
```

Все нестандартные пакеты должны разрешаться из закоммиченных vendor-данных;
скачивание зависимостей и прочая build-time сеть отключены. Валидация пакета
запрещает workspace и переключение toolchain, cross-компиляцию, cgo, PGO,
генераторы, тесты, плагины, overlays, выбранные пакетом assembler- или host
object-файлы, внешнюю линковку и fallback на libgcc. Полный граф пакетов должен
содержать ровно один корневой не-тестовый `package main`; входы стандартной
библиотеки обязаны происходить из фингерпринтированного `GOROOT`, а все прочие
входы компилятора — оставаться внутри объявленного build root.

Заданные менеджером границы одной операции: 120 секунд wall time, 8 MiB
совокупного вывода, артефакт 128 MiB, 512 MiB на файл, 1 GiB приватного
build-хранилища, 2 GiB памяти и 64 активных процесса. Ограничения на файл,
память и число процессов применяются только там, где native inventory ниже
помечает соответствующий механизм доступным. Эти границы не заявляют
отложенную жёсткую aggregate-гарантию для всех потомков, перечисленную в
[Исполнительных контролях](#исполнительные-контроли).

Source-aware `go-v1` поддерживается на macOS и Windows. На других host-ах
операция отказывает до запуска worker или дочернего Go-процесса. Поддержка
Linux явно отложена в `TASK-260728-1skseh` и `TASK-260728-1e6811`; это
ограничение source-aware сборки не меняет поддержку обычных script/system
скиллов и остальной части CocoaSkills.

### Переносимая исполнительная граница

`manager-worker-v1` — обязательная идентичность execution policy. Она является
нормативным input для cache, receipt, marker-currentness и claim; это не опция,
host label, предпочтение оператора или видимая пакету настройка. Разные
идентичности execution policy дают разные cache keys.

Граф процессов фиксирован четырьмя узлами:

```text
родительский менеджер CocoaSkills
  -> принадлежащий менеджеру worker с проверенной идентичностью
       -> фингерпринтированный <GOROOT>/bin/go
            -> фингерпринтированные обычные дочерние файлы под <GOROOT>/pkg/tool/
```

Скрытый worker является точным повторным запуском менеджера, а не выбранной
манифестом программой. Менеджер проверяет его идентичность до запуска; worker
доказывает её относительно свежего session nonce. Одна сессия может выполнить
ровно один фиксированный `go list`, дождаться проверки полного package graph
родителем и выполнить ровно один фиксированный `go build` после
аутентифицированного permit. Лишнее сообщение, повтор, процесс, скачивание,
generator, test или run-запрос приводит к teardown сессии без разрешения
дополнительной работы компилятора.

Снапшот исходников остаётся замороженным. Его целостность, identity worker и
identity полного Go toolchain повторно проверяются после выполнения. Весь
домен worker завершается и join-ится до возврата операции. Лишь затем менеджер
может опубликовать ограниченный обычный артефакт. CocoaSkills никогда не
запускает свежесобранный артефакт при валидации, установке, status, repair,
rollback или GC. Артефакт выполняется только позже, когда пользователь или
агент явно вызывает его активированный shim.

### Исполнительные контроли

Каждая source-aware execution operation создаёт ровно один закрытый результат
`capability-evidence-v1` с одной записью для каждого контроля из
`rc5-native-control-inventory-v1`. Записи содержат `name`, `availability`,
`status` и `probed_at: "pre-worker-launch"`; сам record также несёт версию,
`manager-worker-v1` и платформу. Для доступного контроля `status` равен
`applied`, для недоступного — `unavailable`.

| Контроль inventory | macOS | Windows |
|---|---|---|
| `descendant-domain-termination` | доступен: teardown process group и session | доступен: Job Object kill-on-close |
| `active-process-count-limit` | недоступен: нет приватного aggregate domain | доступен: active-process limit Job Object |
| `aggregate-memory-limit` | недоступен: нет приватного aggregate domain | доступен: process и job memory limits Job Object |
| `per-file-size-limit` | доступен: `RLIMIT_FSIZE` | недоступен: нет приватного aggregate domain |
| `inherited-handle-restriction` | доступен: close-on-exec и явное освобождение descriptors | доступен: явный список наследуемых handles |

Контроль inventory, помеченный недоступным, не отклоняет переносимую сборку.
Отсутствующий обязательный переносимый контроль отклоняет её: операция
возвращает `build_execution_control_unavailable` до запуска worker или Go и
ничего не публикует. Capability evidence является только результатом. Оно не
входит в cache key, receipt, marker, claim или решение currentness; при наличии
скомпилированных команд `csk status` сообщает его отдельно.

Эта переносимая policy **не** предоставляет и не заявляет ни одну из отдельно
отложенных hardened-гарантий:

- `total-network-denial`;
- `read-only-source-and-toolchain`;
- `private-build-root-only-writes`;
- `hard-aggregate-descendant-resource-bounds`;
- `exact-executable-allowlisting`;
- `fail-closed-capability-preflight`.

Переносимые механизмы выше по-прежнему отказывают закрыто, если невозможно
применить их собственные обязательные проверки; они не являются
kernel-enforced версиями этих шести hardened-гарантий.

### Кэш, жизненный цикл, status и активация

Логическая идентичность кэша включает полный проверенный сырой исходник,
объявленные build root/source directory/command, нативный target,
фингерпринтированный Go toolchain, фиксированную Go policy и
`manager-worker-v1`. Эти логические входы, канонические байты receipt,
относительный путь артефакта и его байты/hash/size образуют границу
переносимости. Физическая раскладка manager home CocoaSkills специфична для
реализации:

```text
<csk-home>/builds/go-v1/<64-lowercase-hex-cache-key>/
  csk-receipt.ccj.json
  bin/<command>                Unix
  bin/<command>.exe            Windows
<csk-home>/.builds-staging/
<csk-home>/.builds-quarantine/
```

Не смешивайте `content_sha256` установленного дерева с
`curator-build-source-v1`: первый хэширует установленное содержимое, исключая
его marker, а второй идентифицирует полностью проверенный сырой снапшот и
поэтому включает build-only исходники. Аналогично receipt с согласованными key,
input, artifact path, hash и size внутренне непротиворечив, но это не доказывает
provenance защищённого состояния. Для постоянного переиспользования также
нужны созданная менеджером граница владения, permissions/DACL, containment,
обычные типы файлов и link safety. Receipt hashes — идентификаторы
непротиворечивости/currentness, а не подписи, MAC, attestations или
доказательства provenance.

Реальные project- и global-установки разрешают провайдеров раньше потребителей,
а build-команды внутри провайдера — лексически. Валидация, dependency closure,
source- и audit-гейты, заморозка, выбор toolchain и планирование cache
предшествуют компилятору. Cache miss компилируется в приватном staging операции
вне manager-home mutation lock. Под этой блокировкой CocoaSkills восстанавливает
прерванные транзакции, повторно проверяет поколения и preimage целей,
публикует immutable winner защищённого кэша и атомарно коммитит
материализацию. Project, global и адресованные hybrid surfaces работают по
all-or-rollback: контексты, runtimes, compiled/script shim-ы, adapters,
env-файлы, markers, удаление устаревшего и consumer state перемещаются вместе,
иначе прежняя установка восстанавливается. Безопасно опубликованная, но ещё не
используемая immutable cache entry может остаться для последующего GC.

`csk install --dry-run`, `csk upgrade --dry-run` и их global-варианты
останавливаются до мутаций и до `go list`, `go build`, compiler или linker.
Они могут проверить и хэшировать замороженный source, установить идентичность
доверенного toolchain и только прочитать защищённый cache. Они не создают
постоянный cache, snapshot, mutation lock или journal. Каждый build plan
сообщает одно из `cache-hit`, `would-preflight-and-build`,
`would-rebuild-untrusted-cache`, `corrupt` или `unsupported` вместе с
build-source identity, cache key, native target, driver, command, build root и
source directory.

`csk status --json` и `csk global status --json` сообщают build rows с
provider, command, label/detail, ожидаемым и записанным cache key,
`manager-worker-v1` и отдельным результатом capability evidence. `--check`
возвращает 1, если любой skill или build не current. Currentness требует
согласия активного descriptor, raw snapshot, build-source identity, toolchain,
native target, execution policy, cache key, защищённых receipt/artifact,
marker и managed shim. Status только читает и никогда не пересоздаёт
отсутствующее состояние.
Стабильные build labels: `current`, `build-command-drift`,
`missing-build-marker`, `unsupported-build-driver`, `build-input-drift`,
`missing-build-artifact`, `corrupt-build-cache`, `untrusted-build-cache`,
`unsupported-build-platform`, `build-marker-drift`, `build-shim-drift` и
`build-state-changed`.

Repair выполняется обычной переустановкой: снова запустите `csk install` или
`csk global install`. На поддерживаемой платформе отсутствующий, повреждённый,
wrong-input, legacy/unsupported-identity или недоверенный candidate state
перестраивается из заново замороженного и перепроверенного source в новое
защищённое состояние; csk не принимает и не правит candidate bytes. Реально
неподдерживаемая платформа остаётся fail-closed и не «чинится» локально.
`csk gc` берёт manager-home lock, помечает schema-6 keys, на которые ссылаются
валидные project/global/hybrid markers, marker roots зарегистрированных
consumers или живые transaction journals, и удаляет только защищённые,
доказуемо неиспользуемые entries старше 24 часов. Неопределённые marker,
journal, boundary или receipt сохраняются с предупреждением, а не считаются
безопасными для удаления.

Активация никогда не копирует скомпилированный артефакт в хранилище скриптового
runtime. Project- или адресованная hybrid-установка создаёт
`<project>/.agents/bin/<command>`; global-установка создаёт
`<csk-home>/global/bin/<command>` и, когда это безопасно, user-bin forwarder.
На Unix управляемый `/bin/sh` launcher напрямую делает `exec` абсолютного
артефакта защищённого кэша и передаёт `"$@"`. На Windows управляемый
`<command>.cmd` напрямую вызывает заключённый в кавычки абсолютный `.exe`,
передаёт `%*` и возвращает его exit status. Агент по-прежнему разрешает
project shim, затем global shim, затем проверенную bare-команду, поэтому
активация shell profile не требуется.

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
| `csk bootstrap` | Создать машинный глобальный конфиг; интерактивно или скриптово через `--skills-root`, `--default-agents`, `--non-interactive`, `--force`. `--if-missing` ничего не меняет при существующем конфиге и несовместим с `--force`. |
| `csk init [path]` | Создать проектный `Skillfile.json` и managed-блок `.gitignore`. Поддерживает `--alias`, `--agents` и `--no-interactive` для скриптовой настройки. |
| `csk install [target]` | Применить `Skillfile.json` по текущим git-ссылкам. Отсутствующие источники с `git` URL клонируются в `skills_root`; существующие локальные репозитории не фетчатся. Без target действует текущий проект; `target` может быть алиасом, `.` или путём проекта. `--dry-run` проверяет и планирует compiled cache outcomes без постоянных мутаций и работы компилятора. |
| `csk install --audit [strict]` | Запустить гейт аудита только для этой установки. Без `strict` аудит advisory и не меняет конфиг. |
| `csk install --all` | Установить каждый проект, явно зарегистрированный в глобальном конфиге. |
| `csk update` | Зафетчить все git-репозитории в `skills_root`. Проекты не изменяются. |
| `csk upgrade [target]` | Зафетчить только прямые и транзитивные репозитории скиллов выбранного проекта, затем установить. `--dry-run` не обновляет постоянный кэш и не записывает файлы. |
| `csk upgrade --all` | Один раз зафетчить объединение dependency closure и установить каждый зарегистрированный проект. |
| `csk status [target]` | Показать манифест против установленного состояния, включая активные dev-подмены и currentness скомпилированных команд. `--check` завершится ненулевым кодом, если любой skill или build не current; `--json` включает стабильные build rows и result-only capability evidence. |
| `csk status --all` | Показать статус каждого зарегистрированного проекта. |
| `csk add <name> --tag/--branch/--revision ...` | Добавить или заменить декларацию скилла в проектном Skillfile; применить через `csk install`. |
| `csk remove <name>` | Удалить декларацию скилла из проектного Skillfile; следующая установка вычищает генерируемые файлы. |
| `csk gc` | Под manager-home lock удалить неиспользуемые runtime и snapshot entries, защищённые compiled-cache entries старше 24 часов и мёртвые записи consumer registry. Неопределённое защищённое состояние сохраняется. |
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
| `csk global upgrade` | Выполнить глобальный update, затем глобальный install. `--dry-run` пропускает update и строит неперсистентный план установки. |
| `csk global status` | Показать глобальный манифест и compiled-build state; поддерживает `--json` и `--check`. |
| `csk global list` | Перечислить глобальные декларации скиллов. |
| `csk config show` | Напечатать путь и содержимое разрешённого конфига. |
| `csk shell-init [auto\|zsh\|bash\|powershell]` | Опционально напечатать shell-hook для human-facing авто-активации `PATH`. `auto` выбирается по умолчанию; `--install` атомарно кэширует hook и печатает команду загрузки для профиля. Работа агента от hook не зависит. |
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
python -m pytest
python -m mypy
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
  v2, capabilities schema v3, требования schema v4, compiled-команды schema
  v6, системные зависимости, поведение аудита и авторский чеклист.
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
