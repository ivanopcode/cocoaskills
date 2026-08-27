# Поставка CLI-утилит в скиллах CocoaSkills

Авторы скиллов поставляют CLI-утилиты двух видов: компилируемые команды на Go и скрипт-команды. Этот документ описывает два способа размещения утилит, матрицу допуска компилятора `csk`, правила оформления скрипт-команд, минимальные примеры конфигураций и статус планируемых языков.

## 1. Размещение утилит

Скилл поставляет утилиту через один из двух путей размещения.

Встроенные исходные тексты живут в каталогах `build_roots` внутри репозитория скилла. Этот путь подходит для утилит собственного кода скилла, создаваемых и сопровождаемых вместе с инструкциями скилла. Установщик копирует каталоги `build_roots` во временное окружение сборки и удаляет их из финального контекста агента.

Внешний билд-репозиторий объявляется в секции `build_repositories` манифеста `agent-skill.json`. Скилл ссылается на отдельный Git-репозиторий с фиксированным `locked_commit` или тегом `tag`. Этот путь подходит для крупных или общих утилит, разрабатываемых независимо от скилла.

Таблица сравнения путей размещения:

| Критерий | Встроенные `build_roots` | Внешний `build_repositories` |
| :--- | :--- | :--- |
| Исходный код | Внутри репозитория скилла | В отдельном Git-репозитории |
| Драйвер команды | `go-v1` | `go-repository-v1` |
| Клонирование при сборке | Не требуется | Клонируется по SSH или HTTPS |
| Привязка версии | Вместе с коммитом скилла | Явное поле `locked_commit` |
| Изоляция контекста промпта | `build_roots` исключаются из контекста | Код репозитория не попадает в контекст |

## 2. Матрица допуска и ограничения компилятора

Менеджер `csk` собирает бинарные файлы через два раздельных механизма в зависимости от источника утилиты.

### Разделение компилируемых путей

Единственным компилируемым драйвером для изолированного воркера сборки является `go-v1` (`SUPPORTED_BUILD_DRIVERS = {"go-v1"}`). Воркер собирает исполняемые файлы из каталогов `build_roots` внутри пакета скилла.

Внешние билд-репозитории обслуживаются драйвером `go-repository-v1`. Этот драйвер обрабатывается напрямую слоем установщика (`src/csk/installer.py`), который клонирует репозиторий и передает сборку изолированному воркеру.

### Ограничения сборки и проверки исходного кода

Драйвер `go-v1` применяет фиксированную политику сборки `FIXED_GO_BUILD_POLICY`:

- Зависимости поставляются строго через каталог `vendor/` и файл `vendor/modules.txt`. Сборка исполняется с флагом `-mod=vendor`. При отсутствии каталога сборка завершается отказом `vendor_dependency_missing`.
- Сетевой доступ при сборке полностью запрещен (`network: "none"`).
- Среда Go берется из системного `GOROOT`. Версия компилятора проходит валидацию идентичности тулчейна.
- Взаимодействие с C отключено (`cgo: False`, отказ `cgo_required`).
- Профилирование PGO запрещено (файл `default.pgo` вызывает отказ `go_pgo_forbidden`).
- Кодогенераторы не запускаются в собственном коде скилла (`allows_go_generate = vendored`). Директива `//go:generate` в коде скилла вызывает отказ `go_generator_forbidden`. В зависимости в каталоге `vendor/` директива инертна.
- Тестовые файлы `*_test.go` исключаются из контекста сборки (вызов `go list` не должен выбирать тестовые пакеты, отказ `go_test_input_forbidden`).
- Ассемблерный код `.s` в собственном коде скилла запрещен (отказ `go_assembly_forbidden`).
- Внешняя линковка запрещена (`link_mode: "internal"`).

### Поддержка платформ и отказы Linux

Сборка компилируемых команд поддерживается только на macOS (`darwin`) и Windows (`win32`).

На операционной системе Linux менеджер `csk` отклоняет установку компилируемого скилла до старта процессов сборки. Вызовы происходят в разных точках кода и возвращают разные сообщения:

- Для встроенных исходников `go-v1` функция `inventory_platform()` в `src/csk/builds/go_v1.py` вызывает исключение `GoV1Error` с кодом `CODE_CONTROL_UNAVAILABLE` и текстом: `rc5-native-control-inventory-v1 covers exactly macOS and Windows`.
- Для внешних репозиториев `go-repository-v1` слой установщика в `src/csk/installer.py` вызывает исключение `InstallError` с текстом: `go-repository-v1 is supported only on macOS and Windows; Linux qualification is deferred`.

### Модульные корни схемы 8

Манифест схемы 8 позволяет объявить массив `modules` для команды с драйвером `go-v1`.

Объявленные пути модулей обязаны быть изолированными. Поле `modules` не может пересекаться или содержать пути из `build_roots` и `runtime_roots`. Функция `_reject_overlaps` в `src/csk/builds/module_roots.py` проверяет пересечение в обе стороны и отклоняет пересекающиеся пути с кодом `build_module_root_containment_invalid`.

Корень сборки `build_root` обязан напрямую содержать файл `go.mod`. Проверка `_validate_nearest_go_module` в `src/csk/skillspec.py` запрещает вложенные модули между `build_root` и `source_dir` и возвращает отказ вида `commands.<name>.source_dir intervening module <path>/go.mod is below build root <build_root>`.

## 3. Скрипт-команды и политики исполнения

Скрипт-команда выполняет готовый сценарий без этапа компиляции Go.

### Допустимые интерпретаторы и резолв шимов

Обычные скрипт-команды используют системные интерпретаторы host-машины через строчку шебанга (`#!/usr/bin/env python3`, `#!/bin/sh`, `#!/usr/bin/env bash`).

При установке `csk` копирует файлы скриптов из `runtime_roots` в каталог `~/.cocoaskills/runtime/<skill>/<commit>/`.

Менеджер создаёт исполняемый шим в `.agents/bin/` (или `~/.cocoaskills/bin/` для глобальной установки):

- На POSIX-системах (macOS, Linux) создается исполняемый скрипт-лончер `#!/bin/sh` либо символическая ссылка.
- На Windows создается `.cmd` скрипт-лончер (`@echo off`, `set "PATH=..."`, `call ...`).

### Схема 8: execution_policy и rejection

Манифест схемы 8 позволяет объявить параметры `execution_policy` и `interpreter` для скрипт-команды.

Допустимым значением `execution_policy` является `script-worker-v1`. Допустимыми значениями `interpreter` являются `python3-v1` и `node-v1`. Указание одного из полей требует указания второго.

В текущей версии `csk` изолированный воркер скриптов не реализован (`SCRIPT_EXECUTION_POLICIES_IMPLEMENTED` равен пустому множеству). При попытке установить скилл с `execution_policy: "script-worker-v1"` менеджер отвергает установку с ошибкой `script_execution_policy_unsupported`:

```text
error: skill.script_execution_policy_unsupported agent-skill.json: script_execution_policy_unsupported: this manager does not implement the selected script execution policy, so it refuses to install <command_name> (script-worker-v1). The command is not downgraded to a declared-only shim.
```

Сообщение подтверждает отказ от автоматического понижения утилиты до обычного шима.

## 4. Минимальные примеры конфигураций

Приведенные ниже примеры демонстрируют минимальную конфигурацию манифеста для каждого поддерживаемого пути поставки утилит.

### 4.1. Встроенная go-v1 утилита

Манифест `agent-skill.json` объявляет корень сборки `src` и команду `my-tool`:

```json
{
  "schema_version": 8,
  "capabilities": {},
  "build_roots": ["src"],
  "commands": {
    "my-tool": {
      "type": "build",
      "driver": "go-v1",
      "source_dir": "src/cmd/my-tool"
    }
  }
}
```

Файлы утилиты располагаются в репозитории скилла следующим образом:

```text
skill-go-example/
  SKILL.md
  agent-skill.json
  src/
    go.mod
    cmd/
      my-tool/
        main.go
    vendor/
      modules.txt
```

Команда `csk install` компилирует бинарный файл `my-tool` и публикует шим `.agents/bin/my-tool`.

### 4.2. Внешняя go-repository-v1 утилита

Манифест `agent-skill.json` объявляет внешний Git-репозиторий в секции `build_repositories`:

```json
{
  "schema_version": 8,
  "capabilities": {},
  "build_repositories": {
    "infra-tools": {
      "git": "git@github.com:org/infra-tools.git",
      "tag": "v1.2.0",
      "locked_commit": {
        "object_format": "sha1",
        "hex": "a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e"
      }
    }
  },
  "commands": {
    "infra-cli": {
      "type": "build",
      "driver": "go-repository-v1",
      "repository": "infra-tools",
      "target": "infra-cli"
    }
  }
}
```

Адрес `git` при работе по HTTPS обязателен с суффиксом `.git`. Настройка аутентификации SSH и HTTPS описана в документе [Внешние билд-репозитории](external-build-repositories.md).

### 4.3. Скрипт-команда

Манифест `agent-skill.json` объявляет корень исполняемых файлов `scripts` и скрипт-команду `my-script`:

```json
{
  "schema_version": 8,
  "capabilities": {},
  "runtime_roots": ["scripts"],
  "commands": {
    "my-script": {
      "type": "script",
      "unix_path": "scripts/my-script.py",
      "win_path": "scripts/my-script.cmd"
    }
  }
}
```

Скрипты располагаются в репозитории скилла следующим образом:

```text
skill-script-example/
  SKILL.md
  agent-skill.json
  scripts/
    my-script.py
    my-script.cmd
```

Установщик `csk` копирует скрипты в окружение runtime и создает шим `.agents/bin/my-script`.

## 5. Планируемые языки

Языки Kotlin (`kotlin`), Swift (`swift-v1`) и Rust (`rust`) не реализованы в компиляторе `csk`.

Указание недопустимого значения в поле `driver` вызывает отказ валидации при запуске `csk skill check`:

```text
error: skill.spec_invalid agent-skill.json: Command '<command_name>' field 'driver' must be 'go-v1' or 'go-repository-v1'
```

Диагностика `skill.spec_invalid` подтверждает разрешение только драйверов `go-v1` и `go-repository-v1`.
