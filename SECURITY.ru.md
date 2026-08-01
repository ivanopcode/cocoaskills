# Политика безопасности

Перевод английской версии. Источник правды: [SECURITY.md](SECURITY.md).

## Поддерживаемые версии

Исправления безопасности выходят в последней линейке релизов.

| Версия | Поддерживается |
|---|---|
| 0.9.x | да |
| < 0.9 | нет; обновитесь до последнего релиза |

## Сообщение об уязвимости

Сообщайте об уязвимостях приватно через GitHub:
[Security advisories](https://github.com/ivanopcode/cocoaskills/security/advisories/new).
Пожалуйста, держите детали уязвимости вне публичных issue и pull request до
выхода исправления.

Включите то, что можете: затронутую версию, воспроизведение, наблюдаемое
влияние и предлагаемое исправление, если оно есть. Проект сопровождается по
мере возможностей; ожидайте первичный ответ в течение недели.

## Скоуп

Отчёты особого интереса:

- Запись вне назначенных директорий при установке (выход за пределы пути
  через манифесты, архивы или имена).
- Исполнение команд через содержимое манифеста, git URL или архивы скиллов.
- Влияние пакета schema 6 за пределами роли compiler input на executable
  manager/worker, process graph Go, toolchain path, arguments, environment,
  controls, output, receipt, publication или activation.
- Переиспользование или активация compiled cache entry, для которой нельзя
  независимо доказать ownership, permissions или DACL, containment, link
  safety, receipt, artifact или currentness.
- Изменение identity worker, source snapshot или fingerprinted toolchain во
  время source-aware operation без fail-closed teardown.
- Обход allowlist источников, гейтов аудита или trust-механизма.
- Загрязнение промпт-контекста: содержимое репозитория, дошедшее до окна
  агента мимо whitelist.
- Раскрытие секретов в логах, отчётах или генерируемых файлах.

## Граница скомпилированных команд

Раздел ограничен принятой schema-6 границей из
[protocol core rc.5](https://github.com/relux-works/curator-spec/blob/v1.0.0-rc.5/protocol/core.md),
а не более поздними ревизиями протокола.

Build source schema 6 считается сторонним вводом. `build_roots` проверяются и
хэшируются в замороженном сыром snapshot, но исключаются из установленного
промпт-контекста и хранилища script runtime. Build-команда имеет закрытую форму
`{"type":"build","driver":"go-v1","source_dir":"..."}`; ни одно поле
пакета не может добавить program, argument, environment value, flag, tag,
target, toolchain, output, hook, generator, plugin или post-build action.
Неизвестный driver отказывает без fallback.

`go-v1` собирает ровно один нативный executable `package main` с vendor-only
module resolution. Нижняя граница протокола — Go 1.23, а эта реализация
принимает только текущее доверенное оператором семейство Go 1.25. Telemetry Go
использует приватное off-state; workspace, переключение toolchain,
cross-compilation, cgo, PGO, выбранные пакетом assembly/host objects,
generators, tests, overlays, plugins, external linking, libgcc fallback и
build-time сеть для зависимостей отклоняются.

`manager-worker-v1` — обязательный logical input для cache, receipt,
marker-currentness и claim, а не опция оператора. Process graph фиксирован:

```text
manager parent
  -> принадлежащий менеджеру worker с проверенной идентичностью
       -> фингерпринтированный <GOROOT>/bin/go
            -> фингерпринтированные обычные дочерние файлы под <GOROOT>/pkg/tool/
```

Manager проверяет worker до запуска и через свежий nonce-bound proof. Один
worker выполняет один фиксированный `go list`, ждёт полной graph validation и
аутентифицированного permit, затем выполняет один фиксированный `go build`.
После исполнения повторно проверяются frozen source, worker и full toolchain
identities; полный worker domain завершается и join-ится до возврата. Лишнее
сообщение или process request вызывает teardown. Manager никогда не запускает
artifact при validation, installation, status, repair, rollback или GC.

Manager-owned bounds одной операции: 120 секунд, 8 MiB совокупного вывода,
artifact 128 MiB, 512 MiB на файл, 1 GiB приватного build storage, 2 GiB памяти
и 64 процесса. File-, memory- и process-механизмы применяются только там, где
inventory ниже помечает их доступными; эти значения не являются hard aggregate
descendant guarantee.

Native control inventory задан явно, а не выводится из имени платформы:

| `rc5-native-control-inventory-v1` | macOS | Windows |
|---|---|---|
| `descendant-domain-termination` | teardown process group/session | Job Object kill-on-close |
| `active-process-count-limit` | недоступен | limit Job Object |
| `aggregate-memory-limit` | недоступен | process/job memory limits Job Object |
| `per-file-size-limit` | `RLIMIT_FSIZE` | недоступен |
| `inherited-handle-restriction` | close-on-exec и release descriptors | явный handle inheritance list |

Каждая source-aware execution operation создаёт один закрытый результат
`capability-evidence-v1` с одной entry на inventory control и реальным
состоянием `available/applied` или `unavailable/unavailable`, измеренным до
запуска worker. Недоступный inventory control не отклоняет portable build.
Обязательный portable control, который нельзя применить, отклоняет операцию с
`build_execution_control_unavailable` до запуска worker или Go и ничего не
публикует. Evidence является только результатом; оно не может влиять на или
аутентифицировать cache key, receipt, marker, claim или currentness.

Source-aware compilation доступна на macOS и Windows. На других host-ах она
отказывает до запуска worker или Go child. Linux source-aware support явно
отложена в `TASK-260728-1skseh` и `TASK-260728-1e6811`; обычная поддержка
script/system скиллов на Linux является отдельной.

Portable policy не предоставляет и не заявляет отложенные hardened
guarantees:

- `total-network-denial`;
- `read-only-source-and-toolchain`;
- `private-build-root-only-writes`;
- `hard-aggregate-descendant-resource-bounds`;
- `exact-executable-allowlisting`;
- `fail-closed-capability-preflight`.

Например, фиксированная offline-конфигурация Go не является kernel network
isolation, identity rechecks не являются read-only mount, manager-selected
paths не ограничивают записи descendants, а manager-owned bounds не являются
hard aggregate limits для каждого потомка.

Logical build inputs, canonical receipt bytes и относительные artifact
bytes/hash/size образуют portability boundary. Физическая раскладка
`<csk-home>/builds/go-v1/<key>/` CocoaSkills — с `csk-receipt.ccj.json` и
`bin/<command>` либо Windows `bin/<command>.exe` — и native protection backend
в неё не входят. Dry-run сообщает недоверенную boundary как
`would-rebuild-untrusted-cache`, а не принимает её.

Установленный `content_sha256` также отличается от сырого
`curator-build-source-v1`. Непротиворечивые receipt и artifact доказывают
только согласованность; provenance protected state дополнительно требует
независимой проверки созданной менеджером границы ownership, permission/DACL,
containment, file types и links.

Реальная project/global/hybrid installation публикует проверенные artifacts и
каждую activation surface под journaled all-or-rollback transaction. Status
ничего не меняет. На поддерживаемой платформе reinstall чинит отсутствующие,
повреждённые, wrong-input, legacy/unsupported-identity или untrusted candidates,
перестраивая новое protected state и никогда не принимая их bytes; unsupported
platform по-прежнему fail-closed. Locked GC удаляет только проверенные
неиспользуемые entries старше 24 часов; неопределённое состояние сохраняется.

## Обзор защит

Модель угроз рассматривает репозитории скиллов как сторонний ввод. Границы
описаны в
[ARCHITECTURE.md, Security boundaries](ARCHITECTURE.md#security-boundaries),
подсистема аудита специфицирована в
[docs/audit-design.md](docs/audit-design.md) и
[docs/v0.8-design.md](docs/v0.8-design.md).
