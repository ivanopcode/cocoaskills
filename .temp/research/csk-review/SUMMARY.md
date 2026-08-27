# CocoaSkills: ревью архитектуры / имплементации / интерфейса / доки

Дата: 2026-06-09. Тесты: 115 passed. Полные отчёты: architecture.md, interface.md, docs.md (рядом).

## Вердикт

Критическая переработка НЕ нужна ни на одном уровне. Архитектура здравая: модули мелкие и слоистые, state-файлы с инвариантами, атомарные свапы директорий, schema_version везде, traversal-защита для путей команд и runtime_roots уже есть. Но есть 3 локализованных критических бага (security/correctness), которые надо чинить до того, как тулом начнут пользоваться шире, и пачка should-fix до окостенения интерфейса.

## Критические (чинить первыми)

1. GC сносит runtime незарегистрированных проектов (нашли независимо 2 ревьюера). csk install . (v0.3) намеренно не пишет проект в конфиг, но project-шимы ссылаются на ~/.cocoaskills/runtime/<skill>/<commit>. Любой следующий install --all / global install вызывает collect_runtime (gc.py:10-24), который сканирует только config.projects + global — и удаляет runtime, на который смотрят шимы worktree-чекаутов (а worktree — головной сценарий). Команды ломаются молча. Фикс: либо runtime-маркеры со списком потребителей, либо last-used-таймстампы + grace, либо регистрация ephemeral-потребителей.

2. git clone injection (git_ops.py:31-44). clone_repo передаёт remote_url без -- и без валидации транспорта: Skillfile с "git": "ext::sh -c ..." = RCE при csk install. Фикс: git clone -- <url> <dst>, allowlist схем (ssh/https/file/scp-подобные), запрет ext::/опасных схем.

3. Path traversal через имена (manifest.py:117-140, skillspec.py:73-75, shims.py). Имя скилла/командные ключи валидируются только на непустую строку; имя команды из стороннего csk-skill.json попадает в имя файла шима — ../../x даёт запись вне директории шимов. Пути команд и runtime_roots УЖЕ защищены, дырка именно в name/command-key. Фикс: regex-валидация идентификаторов (^[a-zA-Z0-9_-]+$).

## Should-fix (до окостенения интерфейса)

- env.sh использует ${BASH_SOURCE[0]} — под zsh вычисляет неверный корень (воспроизведено), а csk shell-init zsh заявлен (env_files.py:11-17).
- _install_runtime_commands продублирован в installer.py и global_install.py — фикс traversal надо вносить в оба, дрейф неизбежен. Вынести в общий модуль.
- Lockfile без проверки живости PID: после краша все команды блокируются до ручного удаления (locking.py).
- Сиротские .tmp-<pid>/.backup-<pid> копятся в .agents/skills.
- Exit-коды: install при отказе gitignore-gate или отсутствии Skillfile выходит 0; status всегда 0 (нет --check) — CI слепой.
- Нет --json у status/list; человеческий формат окостеневает.
- --verbose no-op; global --strict-tags принимается, но не реализован.
- Нет csk add/remove на проектном уровне (асимметрия с global), нет gc для snapshot-кеша (растёт бесконечно).
- Missing git => сырой traceback (FileNotFoundError не в catch-списке cli.py:33-40).
- Неизвестные имена агентов в Skillfile молча игнорятся.

## Дока

- CHANGELOG: v0.6.0 (global skills) не выпущен в отдельную секцию — всё висит в Unreleased, compare-ссылка v0.5.0...HEAD.
- Ловушка locale: README советует "locale": "en", authoring-guide советует .skill_triggers/ — вместе это валит установку, если нет locales/metadata.json (locale.py:11-22). Контракт locale не описан нигде.
- README: нет csk global list, init --alias/--agents/--no-interactive, shell-init --no-global.
- mvp-design.md продаётся как frozen contract, но местами противоречит v0.3 (регистрация проектов) — нужен superseded-баннер.
- Нет справочника Skillfile.json (exactly-one-of tag/branch/revision, валидные agent ids) вне дизайн-доков; нет флоу удаления скилла из проекта.
- Недокументированная утечка контекста: скилл без declared commands копирует весь scripts/ в промпт-контекст (installer.py:278).

## Что хорошо (подтверждено)

Слоистость и размер модулей; атомарные свапы; идемпотентные установки через маркеры; stdout/stderr дисциплина; контракт exit-кодов в основном потоке; качество валидации csk-skill v2; traversal-защита путей команд/runtime_roots; tar-safety; schema_version на каждом артефакте; все 5 каналов установки согласованы.
