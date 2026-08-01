# Архитектура

Перевод английской версии. Источник правды: [ARCHITECTURE.md](ARCHITECTURE.md).

Этот документ даёт контрибьютору карту кодовой базы: ключевые концепции,
конвейер установки, раскладку модулей, места хранения и границы безопасности.
Дизайн-решения живут в RFC в каталоге [docs/](docs/); документ указывает на
них там, где это уместно.

## Ключевые концепции

CocoaSkills работает с двумя манифестами с разными зонами ответственности:

- `Skillfile.json` описывает проект: скиллы, которые проект ставит напрямую,
  агентские среды для адаптации и локаль. Коммитится в репозиторий проекта.
- `agent-skill.json` описывает узел-скилл: экспортируемые команды, объявленные
  capabilities и требования к системным инструментам и другим скиллам. Живёт
  в репозитории скилла; `csk-skill.json` остаётся legacy-именем только для
  чтения.

Скилл schema 6 может материализовать три независимых слоя:

- Слой промпт-контекста: `SKILL.md`, `references/` и прочие файлы для агента
  копируются в `<project>/.agents/skills/<name>/` и зеркалируются в директории
  адаптеров агентов. Это то, что агент читает.
- Слой runtime: `runtime_roots` копируются в общее хранилище runtime и
  открываются как shim-ы команд в `<project>/.agents/bin/`. Это то, что
  агенты и люди вызывают по явному пути; опциональная shell activation только
  добавляет удобство bare-имён. Runtime-файлы остаются вне контекста агента.
- Скомпилированный слой: `build_roots` остаются в проверенном сыром снапшоте
  исходников, но не копируются ни в промпт-контекст, ни в хранилище скриптового
  runtime. Закрытый driver `go-v1` компилирует объявленную `source_dir` в
  защищённую immutable cache entry manager home, а shim команды указывает
  прямо на неё.

Разделение держит окно агента маленьким и делает возможными режимы активации:
зависимость может давать команды, контекст или и то и другое
([RFC 0007](docs/v0.9-design.md)).
Поэтому prompt-visible инструкции разрешают экспортированные script- и
compiled-shim-ы по project/global scope и никогда не адресуют `runtime_root`
или `build_root` относительно `SKILL.md`. Это правило сохраняется, когда
adapter зеркалирует контекст копированием, а не symlink-ом, и не зависит от
инициализации zsh/bash/PowerShell profile.

`skillspec.py` принимает schema манифеста с 1 по 6. Schema 6 добавляет
`build_roots` и закрытую build-команду
`{"type":"build","driver":"go-v1","source_dir":"..."}`. Build roots должны
быть реальными, без ссылок, переносимыми, уникальными, не пересекаться друг с
другом и с runtime roots и использоваться командой. Каждая source directory
принадлежит ровно одному build root, чей непосредственный `go.mod` является
ближайшим module root. Неизвестные drivers или поля сборки, выбранные пакетом,
отклоняются при разборе; fallback между drivers отсутствует.

## Конвейер установки

`csk install` для одного проекта проходит стадии по порядку:

1. Загрузить машинный и проектный конфиг (`config.py`, `manifest.py`) и
   проверить gitignore-гейт до любой записи в проект (`gitignore_gate.py`).
2. Загрузить dev-подмены (`dev_substitutions.py`) и применимые hybrid-
   декларации (`hybrid.py`). Строгий аудит отказывает подменённым установкам.
3. Разрешить транзитивное замыкание (`closure.py`): source-allowlist, точные
   refs, raw snapshots, один commit и канонический source на skill, отклонение
   циклов, activation edges и порядок provider-before-consumer.
4. Проверить каждый выбранный сырой снапшот (`skillcheck.py`, `skillspec.py`).
   Build roots остаются для валидации и хэширования, но whitelist и runtime
   plans их исключают. Выявить коллизии в общем namespace активных script- и
   compiled-launchers.
5. Проверить MCP requirements и выполнить source-, audit-, registry- и
   trust-гейты по всему замыканию (`mcp_configs.py`, `audit/`,
   `audit_registry.py`). Эти гейты предшествуют cache claims и компилятору.
6. Заморозить проверенный сырой snapshot каждого build provider и вычислить
   `curator-build-source-v1` (`builds/source.py`). Выбрать и
   фингерпринтировать доверенную оператором установку Go
   (`builds/toolchain.py`), вывести native target и полный логический input
   `manager-worker-v1`, затем только прочитать защищённый cache
   (`builds/planner.py`). Providers планируются раньше consumers, команды
   внутри provider — лексически.
7. Для реального cache miss удерживать per-key build lock и компилировать в
   приватной директории операции вне manager-home mutation lock.
   Исполнительной границей владеет `builds/go_v1.py`. Cache hit никогда не
   запускает source-aware команды Go.
8. Спланировать каждую materialization target и снять её preimage:
   промпт-контекст, runtime generations, project/global/hybrid compiled- и
   script-launchers, env-файлы, adapters, markers, ledgers, stale removals и
   consumer state.
9. Взять manager-home mutation lock. Восстановить прерванные journals,
   перепроверить поколения замыкания, raw source, cache evidence и target
   preimages, затем атомарно опубликовать проверенные misses через native
   backend (`builds/cache_posix.py` или `builds/cache_windows.py`).
10. Закоммитить полную project transaction (`transactions.py`). Consumer target
    коммитится после providers. При ошибке все targets откатываются в обратном
    порядке под удерживаемым lock, а прежняя live installation сохраняется.
    Безопасно опубликованная, но неиспользуемая immutable cache entry может
    остаться для последующего GC.
11. Выполнить locked fail-safe GC (`gc.py`). Hybrid materialization для
    адресованного проекта участвует в той же project transaction.

Global-установка следует тому же порядку в `global_install.py` и использует
одну all-or-rollback transaction для контекстов, marker-only nodes, runtimes,
compiled/script launchers, безопасных user-bin forwarders, environment,
adapters, ledgers и stale entries. Порядок shadowing: project, hybrid, global.

Dry-run расходится после стадии 6. Он может проверить и хэшировать frozen
snapshot, установить toolchain identity, вывести native target и cache key и
только прочитать protected cache entries. Он возвращается до `go list`,
`go build`, любого compiler/linker, постоянного cache/snapshot, mutation lock
или journal и всей материализации. Стабильные build results:
`cache-hit`, `would-preflight-and-build`,
`would-rebuild-untrusted-cache`, `corrupt` и `unsupported`.

`csk status` независимо перевыводит schema-6 build inputs и сравнивает raw
snapshot, target, toolchain, policy, key, защищённые receipt/artifact, marker
v2 и managed launcher. `--json` открывает стабильные build rows и отдельное
result-only capability evidence; `--check` возвращает 1 для любого
non-current skill или build. Status ничего не меняет. `csk status --attest`
дополнительно перепроверяет установленные скиллы против registry (`attest.py`),
а `csk audit` запускает стадию аудита отдельно.

## Карта модулей

| Модуль | Ответственность |
|---|---|
| `cli.py` | Разбор аргументов и диспетчеризация команд. |
| `config.py` | Машинный конфиг и принудительный системный слой: `skills_root`, среды по умолчанию, режим адаптеров, настройки аудита, `allowed_sources`, `audit_registries`. |
| `manifest.py` | Разбор и правка `Skillfile.json`. |
| `skillspec.py` | Разбор `agent-skill.json`: команды, runtime/build roots, capabilities, зависимости, требования и закрытая build-форма schema 6 (schema 1–6). |
| `closure.py` | Транзитивное разрешение требований, унификация, поиск циклов, рёбра активации, топологический порядок. |
| `source_identity.py` | Каноническая идентичность `host/path` для git URL и матчинг allowlist. |
| `mcp_configs.py` | Проверка объявленных зависимостей от MCP-серверов по конфигурациям агентских сред (только чтение) со статическими проверками доступности: резолюция stdio-команд в PATH, фильтрация отключённых серверов и подсказки о trust-гейтах для деклараций только проектного уровня. |
| `hybrid.py` | Манифест hybrid-скоупа и попроектная привязка активации. |
| `dev_substitutions.py` | Разбор `Skillfile.dev.json` для локальной подмены провайдеров. |
| `git_ops.py` | Усиленные git-операции: клонирование с allowlist протоколов, разрешение ссылок, извлечение архива с проверками путей. |
| `snapshot.py` | Content-addressed кэш сырых снапшотов commit-ов скиллов. |
| `builds/source.py` | Link-safe frozen raw snapshots и identity `curator-build-source-v1`. |
| `builds/toolchain.py` | Снимок operator PATH, bootstrap Go, вывод native target, fingerprint полного `GOROOT` и проверка доверенного семейства. |
| `builds/metadata.py` | Переносимый logical input, policy `manager-worker-v1`, CCJ-1 cache key, canonical receipt и artifact metadata. |
| `builds/planner.py` | Provider-first planning, generation checks, dry-run records и read-only cache outcomes. |
| `builds/go_v1.py` | Закрытое manager/worker исполнение, фиксированные Go graph validation/build, bounds, native-control probes, capability evidence, проверка артефакта и teardown. |
| `builds/cache.py` | Платформенно-нейтральный интерфейс protected cache и стабильные outcomes hit/miss/corrupt/untrusted. |
| `builds/cache_posix.py`, `builds/cache_windows.py` | Native ownership/permission или DACL boundaries, immutable publication, quarantine, inspection и GC. |
| `builds/currentness.py` | Read-only классификация активных и записанных compiled-команд. |
| `whitelist.py` | Правила копирования промпт-контекста: какие файлы скилла доходят до агента. |
| `locale.py` | Рендер локали для локализованных метаданных скилла. |
| `shims.py` | Наполнение script runtime и прямые protected-artifact launchers compiled-команд. |
| `installer.py` | Project/hybrid planning, приватная компиляция, cache publication и transactional materialization. |
| `global_install.py`, `global_bins.py` | Пользовательские установки скиллов и глобальные shim-ы команд. |
| `transactions.py` | Journaled multi-target commit, recovery, target-preimage guards и обратный rollback. |
| `install_marker.py` | Разбор marker v1/v2 и канонические installed build records. |
| `adapters.py` | Директории адаптеров на каждую среду с учётом managed-записей; среды с нативным обнаружением (OpenCode, Windsurf) читают каноническую директорию и не получают проектных зеркал. |
| `status.py` | Отчёт манифеста против установленного состояния. |
| `attest.py` | Перепроверка установленных маркеров против доверенных реестров аудита. |
| `audit_registry.py` | Клиент реестра аудита: верификация записей, deny-wins федерация, проверка снапшотов, кэш запросов. |
| `_ed25519.py` | Вендоренная проверка подписи Ed25519 на стандартной библиотеке. |
| `gc.py` | Locked fail-safe mark/sweep для runtime, snapshots и protected compiled cache. |
| `consumers.py` | Реестр чекаутов, ссылающихся на общие хранилища. |
| `locking.py` | Упорядоченные project/global, per-build-key и manager-home locks с восстановлением зависших lock-ов. |
| `hashing.py` | Content-hash установленных деревьев; отдельно от raw build-source identity. |
| `identifiers.py` | Правила безопасных идентификаторов для имён, становящихся путями файловой системы. |
| `audit/` | Аудит безопасности: статические детекторы, проверки capabilities, решения политики, trust-хранилище, backend-ы извлечения. |

## Раскладка хранилищ

Машинный уровень, в `~/.cocoaskills/`:

```text
config.json                  машинный конфиг
cache/<source>/<commit>/     снапшоты содержимого коммитов скиллов
runtime/<skill>/<commit>/    runtime-файлы и входные точки команд
builds/go-v1/<hex-key>/      защищённые immutable receipt и compiled artifact
.builds-staging/             manager-owned staging публикации кэша
.builds-quarantine/          выведенные protected entries до безопасного удаления
global/                      пользовательские скиллы, bin и манифесты
hybrid/                      машинные скиллы, активируемые попроектно
dev/<skill>/                 клоны для git dev-подмен
cache/registry/              кэш запросов и снапшотов реестра аудита
consumers.json               чекауты, ссылающиеся на общие хранилища
```

Проектный уровень, генерируется и игнорируется git:

```text
.agents/skills/<name>/       промпт-контекст плюс маркер .csk-install.json
.agents/bin/<command>        script- или прямой protected-artifact launcher
.claude/skills/, .codex/skills/, .cursor/rules/, .gemini/skills/
                             зеркала адаптеров на каждую среду
```

OpenCode и Windsurf находят `.agents/skills/` нативно и зеркала не получают;
глобальные установки для них обслуживаются через `~/.agents/skills/`.

Специфичная для csk compiled entry имеет вид:

```text
<csk-home>/builds/go-v1/<64-lowercase-hex-cache-key>/
  csk-receipt.ccj.json
  bin/<command>              Unix
  bin/<command>.exe          Windows
```

Эта физическая раскладка намеренно не является переносимой идентичностью
протокола. Переносимое состояние — полный logical input, cache key, точные
canonical receipt bytes, относительный artifact path и artifact
bytes/hash/size. Физические manager-home paths, названия
cache/staging/quarantine, имя receipt, lock names и native storage backend
остаются деталями реализации csk.

## Контракт сборки schema 6

Эта архитектурная граница следует принятому
[protocol core rc.5](https://github.com/relux-works/curator-spec/blob/v1.0.0-rc.5/protocol/core.md).
Более поздние ревизии протокола находятся вне скоупа этого документа.

### Идентичность и защищённый кэш

Identity `curator-build-source-v1` хэширует полностью проверенный сырой
source snapshot, включая build roots и предоставленные пакетом bytes
descriptor/marker, присутствующие в этом snapshot. Она отличается от
`content_sha256` установленного дерева, который хэширует выбранное
установленное содержимое и исключает сам установленный marker. Одно значение
не может заменять другое.

Полный логический input Go также включает build root, command, source
directory, native target, полный `curator-go-toolchain-v1`, фиксированную Go
policy и `manager-worker-v1`. Его CCJ-1 digest является cache key. Receipt
сохраняет весь input плюс вычисленный менеджером относительный artifact path,
SHA-256 и длину в байтах. Receipt hash считается по точным сохранённым
canonical bytes.

Непротиворечивость receipt не равна provenance защищённого состояния. Cache
reader обязан независимо перевывести ожидаемые input и key и проверить
созданную менеджером границу ownership, permission/DACL, containment, обычных
односсылочных файлов и no-follow до доверия совпадающим receipt и artifact
bytes. Недоверенное состояние является miss: реальная установка перестраивает
его в свежем protected state, dry-run сообщает
`would-rebuild-untrusted-cache`, status становится non-current. Ни marker, ни
совпадающие hashes не аутентифицируют незащищённый cache.

### Фиксированные Go и process graph

Нижняя граница протокола — Go 1.23, но менеджер принимает только доверенное
оператором handoff-семейство. Текущий allowlist csk содержит семейство 1.25.
Менеджер строит только нативный target host-а, выключает telemetry в приватное
состояние и использует vendor mode с `GOTOOLCHAIN=local`, `GOENV=off`,
`GOWORK=off`, `CGO_ENABLED=0`, `GO_EXTLINK_ENABLED=0`, отключённым PGO,
compiler gc и internal linking без libgcc. Workspace, cross-compilation, cgo,
generators, tests, plugins, overlays, выбранные пакетом assembly/host objects,
external linking, module downloads, произвольные arguments, flags,
environment, tools, hooks и post-build actions закрыты. Другой driver
отклоняется без fallback.

`manager-worker-v1` — нормативный input кэша, receipt, marker-currentness и
claim, а не выбор оператора. Граф фиксирован четырьмя узлами:

```text
manager parent
  -> принадлежащий менеджеру worker с проверенной идентичностью
       -> фингерпринтированный <GOROOT>/bin/go
            -> фингерпринтированные обычные дочерние файлы под <GOROOT>/pkg/tool/
```

Manager проверяет worker до запуска, получает его nonce-bound identity proof,
проверяет полный вывод одного фиксированного `go list`, выдаёт один
аутентифицированный build permit и принимает один фиксированный `go build`.
Source замораживается, identity toolchain фиксируется, а identities
source/toolchain/worker повторно проверяются после выхода children. Полный
worker domain завершается и join-ится до возврата. Любое лишнее сообщение state
machine или process request вызывает teardown. Manager никогда не запускает
artifact при validation, installation, status, repair, rollback или GC.

Одна операция имеет manager-owned bounds: 120 секунд, 8 MiB совокупного
вывода, artifact 128 MiB, 512 MiB на файл, 1 GiB приватного storage, 2 GiB
памяти и 64 процесса. Native facilities определяют, какие границы
file/memory/process применяются; это не hard aggregate descendant guarantee.

### Платформенные контроли и evidence

Source-aware compilation поддерживается на macOS и Windows. Linux является
явно отложенной build platform с владельцами `TASK-260728-1skseh` и
`TASK-260728-1e6811`; на других host-ах `go-v1` отказывает до запуска worker
или Go child. Это не отменяет Linux support для script/system частей
CocoaSkills.

Для каждой source-aware execution operation csk создаёт один закрытый record
`capability-evidence-v1`, содержащий ровно одну запись на каждый control из
`rc5-native-control-inventory-v1`:

| Контроль | macOS | Windows |
|---|---|---|
| `descendant-domain-termination` | доступен: teardown process group/session | доступен: Job Object kill-on-close |
| `active-process-count-limit` | недоступен | доступен: limit Job Object |
| `aggregate-memory-limit` | недоступен | доступен: process/job memory limits Job Object |
| `per-file-size-limit` | доступен: `RLIMIT_FSIZE` | недоступен |
| `inherited-handle-restriction` | доступен: close-on-exec и release descriptors | доступен: явный handle inheritance list |

Каждая entry записывает name, availability, applied/unavailable status и
время probe `pre-worker-launch`. Inventory control, помеченный unavailable,
не отклоняет build. Невозможность применить обязательный portable control
отклоняет операцию с `build_execution_control_unavailable` до worker и ничего
не публикует. Evidence является только результатом и никогда не входит в
cache identity, receipt, marker, claim или currentness.

Portable policy не предоставляет и не заявляет
`total-network-denial`, `read-only-source-and-toolchain`,
`private-build-root-only-writes`,
`hard-aggregate-descendant-resource-bounds`,
`exact-executable-allowlisting` или
`fail-closed-capability-preflight`. Это шесть отложенных hardened guarantees,
а не альтернативные названия для portable mechanisms, которые csk реально
применяет.

### Status, repair, GC и активация

Marker v2 записывает raw build-source identity и отсортированные schema-6 build
roots и commands с driver, cache key, receipt hash, artifact hash и artifact
path. Execution policy уже транзитивно связана через input/key/receipt и не
является package-settable полем marker. Currentness перевыводит каждую build
surface, а не доверяет marker.

Обычная переустановка является repair. На поддерживаемой платформе
отсутствующие, повреждённые, wrong-input, legacy/unsupported-identity или
untrusted candidates перестраиваются из заново замороженного и
перепроверенного source; csk никогда не принимает candidate bytes. Реально
неподдерживаемая платформа остаётся fail-closed. Project, global и адресованный
hybrid repair используют те же правила transaction/rollback, что и первая
установка.

`csk gc` удерживает manager-home lock. Он отмечает keys из валидных
project/global/hybrid marker v2, marker v2 зарегистрированных consumers и
active transaction journals, сохраняет всё при неопределённом mark source или
protected boundary и удаляет только проверенные неиспользуемые protected
entries старше 24 часов. Receipt сам по себе не является liveness root.

Compiled artifacts остаются в protected cache. Project и targeted-hybrid
launchers живут в `<project>/.agents/bin`; global launchers — в
`<csk-home>/global/bin`, с безопасными user-bin forwarders там, где они
доступны. Unix launcher использует `/bin/sh`, чтобы сделать `exec` абсолютного
artifact и передать `"$@"`. Windows `.cmd` вызывает заключённый в кавычки
абсолютный `.exe`, передаёт `%*` и сохраняет его exit status. Agent-facing
resolution идёт через project launcher, global launcher и затем проверенную
bare command; activation profiles опциональны.

## Границы безопасности

- Имена, становящиеся путями файловой системы, проходят правило безопасного
  идентификатора (`identifiers.py`), поэтому сторонний манифест не может
  писать вне назначенных директорий.
- `git clone` ограничивает транспорты через `GIT_ALLOW_PROTOCOL` и отделяет
  URL от опций, что блокирует remote-helper URL, исполняющие команды.
- Извлечение архива отклоняет выход за пределы пути и ссылки.
- Манифесты объявляют и никогда не исполняют: install-хуки, проверки и пробы
  версий отклоняются на этапе разбора.
- Allowlist источников (`allowed_sources`) проверяет каждое клонирование по
  канонической идентичности `host/path` до первой сетевой операции.
- Whitelist промпт-контекста держит метаданные репозитория, тесты и
  build-файлы вне окна агента.
- Build-only bytes остаются только compiler input: они не могут выбирать
  worker, программу toolchain, arguments, environment, working directory,
  controls, limits, output, cache metadata, publication или activation.
- Protected compiled cache адресуется только независимо выведенными logical
  inputs. Код cache и transactions отклоняет неоднозначные ownership, links,
  изменения target, non-canonical receipts и несогласованные artifacts;
  непротиворечивые незащищённые bytes никогда не принимаются как provenance.
- Source-aware graph и portable controls являются manager-enforced mechanisms;
  шесть hardened guarantees выше явно находятся вне предоставленной границы.
- Подсистема аудита оценивает каждый узел замыкания установки; решение об
  установке остаётся детерминированным внутри CocoaSkills
  ([RFC 0005](docs/audit-design.md), [RFC 0006](docs/v0.8-design.md)).
- Записи реестра аудита проверяются против пинованных вне канала ключей Ed25519
  до доверия, федерация работает по deny-wins, а принудительный системный слой
  конфигурации с locked-ключами не даёт разработчику расширить границу доверия
  ([RFC 0008](docs/v0.11-design.md)).

## История дизайна

| Документ | Объём |
|---|---|
| [docs/mvp-design.md](docs/mvp-design.md) | Контракт v0.1: манифесты, ссылки, конвейер установки, блокировки, адаптеры. |
| [docs/v0.3-design.md](docs/v0.3-design.md) | RFC 0001: `csk init`, явный `--all`, установки текущего проекта. |
| [docs/v0.4-design.md](docs/v0.4-design.md) | RFC 0002: автоклонирование объявленных `git`-источников. |
| [docs/v0.5-design.md](docs/v0.5-design.md) | RFC 0003: `runtime_roots` для многофайловых runtime команд. |
| [docs/v0.6-design.md](docs/v0.6-design.md) | RFC 0004: глобальные скиллы. |
| [docs/audit-design.md](docs/audit-design.md) | RFC 0005: манифесты capabilities и детерминированный гейт аудита. |
| [docs/v0.8-design.md](docs/v0.8-design.md) | RFC 0006: LLM-backend-ы аудита. |
| [docs/v0.9-design.md](docs/v0.9-design.md) | RFC 0007: зависимости скиллов, режимы активации, dev-подмены, allowlist источников. |
| [docs/v0.11-design.md](docs/v0.11-design.md) | RFC 0008: реестр аудита, цепочка доверия, федерация, принудительный системный конфиг. |

## Тестирование

Тесты живут в `tests/` и запускаются обычным `pytest`. Фикстуры в
`tests/conftest.py` строят одноразовые git-репозитории для скиллов и проектов,
поэтому end-to-end тесты установки проверяют настоящий конвейер на временных
хранилищах. Платформенные ожидания, например symlink-shim-ы, несут явные
платформенные маркеры, а фикстуры script-команд поставляют обе входные точки,
`unix_path` и `win_path`, поэтому общий набор запускается на Linux, macOS и
Windows. Source-aware поведение `go-v1` имеет lanes для macOS и Windows; тесты
на других host-ах проверяют намеренный fail-closed platform result. Linux
source-aware support остаётся у `TASK-260728-1skseh` и
`TASK-260728-1e6811`.
