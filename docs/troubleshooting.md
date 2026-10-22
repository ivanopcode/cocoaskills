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

Начните с `csk init`, затем `csk check` и `csk install`. Schema 3 проходит тот же MCP-валидатор, что install; `check` ничего не записывает и не проверяет auth/connectivity. Install сам добавляет точные ignore entries для создаваемых им MCP configs с локальной git policy. Deprecated `--fix-gitignore` для них не нужен; для остальных generated paths используйте `csk init`.

`gitignore_required` в MCP-строке указывает на общий ignore preflight, а `blocked by project_preflight_failed` — на общую ошибку без отдельного кода: смотрите следующую диагностику проекта. Это не ошибка MCP declaration. Если согласия требует соседний сервер, сервер без такой потребности получает `blocked by mcp_plan_unapproved`.

`mcp_command_invalid` also refuses any runner/evaluator token inside `args`, including unknown wrappers such as `find . -exec npx pkg ;`. Matching ignores path, case and executable suffixes. Even an intended data argument such as `--tool node` refuses; the diagnostic names the token. Use a direct bare executable with ordinary arguments, or a direct supported exact-pin `npx`/`uvx` recipe. Direct evaluator launchers and inline evaluation remain refused.

`gitignore_required` can mean a nested rule defeats the proposed automatic MCP ignore repair. Repair that conflicting rule and retry; the refusal leaves native configs and the live `.gitignore` unchanged. Git evaluates the proposed hierarchy before consent. `source_output_overlap` can identify an unwritable output parent; restore write/search access and retry. These readiness checks precede the prompt and do not replace publication-time checks.

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

## unsafe transaction tree entry: `.../runtime/<skill>/<commit>/.venv/...`

Скилл ранней версии забутстрапил виртуальное окружение прямо в runtime-дерево. Транзакция установки отказывается работать поверх symlink.

Очистите устаревшие виртуальные окружения перед повторным запуском:

```bash
rm -rf ~/.cocoaskills/runtime/*/*/.venv
```

После удаления каталогов venv следующая команда скилла автоматически пересоздаст изолированное окружение.

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

## build_repository_ssh_credential_missing

Для приватного SSH-репозитория сборки не выбраны креды. При наличии интерактивного TTY csk выводит меню обнаруженных кандидатов; без TTY установка завершается ошибкой `build_repository_ssh_credential_missing`.

Привяжите креды к скоупу канонической идентичности репозитория:

```bash
csk config build-ssh add <host>/<namespace> --agent auto --identity ~/.ssh/<key>.pub
```

Команда записывает скоуп в `~/.cocoaskills/config.json` и печатает `Configured build-ssh scope <scope>`. Повторите установку: инсталлятор возьмёт креды из скоупа.

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

Адрес HTTPS в поле `build_repositories.*.git` не содержит обязательного суффикса `.git`. Сервис GitLab или Git-хост возвращает ответ 301 Redirect, но установщик выполняет `git fetch` с `http.followRedirects=false` и не следует перенаправлениям по соображениям безопасности. Установщик выводит ошибку `build_repository_source_unavailable: exact external source is unavailable`. Запуск команды `git -c http.followRedirects=false ls-remote <url>` вручную воспроизводит подробное сообщение `fatal: ... The requested URL returned error: 301`.

Добавьте суффикс `.git` к адресу репозитория в поле `build_repositories.*.git` манифеста `agent-skill.json`:

```json
{
  "build_repositories": {
    "core": {
      "git": "https://gitlab.example.com/portals/infra.git"
    }
  }
}
```

Установщик при повторном запуске выполнит `git fetch` по прямому каноническому адресу без HTTP-редиректа.

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
