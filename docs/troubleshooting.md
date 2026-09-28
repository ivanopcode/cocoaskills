# Диагностика установки

Описанные ниже симптомы возникают при обновлении с ранних версий csk (<=0.12) и при работе с компилируемыми командами.

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

## go-v1 unsupported_go_family

Сообщение называет обнаруженную версию, путь к Go, квалифицированные семейства и содержит строку `remediation:`. Укажите Go 1.25–1.27 (или новее) первым в `PATH`, например `go = "1.27"` в проектном `.mise.toml` или установите Go командой `brew install go`. Семейства новее 1.27 принимаются с предупреждением по умолчанию; для строгого отказа задайте `builds.go_future_families: "refuse"` в `~/.cocoaskills/config.json` либо `CSK_GO_FUTURE_FAMILIES=refuse`.

## build_repository_ssh_credential_missing

Для приватного SSH-репозитория сборки не выбраны креды. При наличии интерактивного TTY csk выводит меню обнаруженных кандидатов; без TTY установка завершается ошибкой `build_repository_ssh_credential_missing`.

Привяжите креды к скоупу канонической идентичности репозитория:

```bash
csk config build-ssh add <host>/<namespace> --agent auto --identity ~/.ssh/<key>.pub
```

Команда записывает скоуп в `~/.cocoaskills/config.json` и печатает `Configured build-ssh scope <scope>`. Повторите установку: инсталлятор возьмёт креды из скоупа.

## build_repository_credential_policy_invalid

Скоуп аутентификации `build_https` совпал в конфигурации, но выбранный источник токена не вернул данные. Текст ошибки называет конкретный случай из трёх возможных:

1. Правило `token_env` указывает незаданную переменную окружения (`build_https scope '<scope>' names environment variable '<token_env>', which is unset`). Экспортируйте значение переменной из хранилища перед запуском:

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
