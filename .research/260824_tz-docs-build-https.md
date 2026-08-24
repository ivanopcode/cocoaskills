# ТЗ: доки cocoaskills под приватный HTTPS (build_https)

Дата: 2026-08-24. Основание: feat/build-https-broker влита в main
(be88caa, 9a1f9da, 5381317 поверх 97cfa77), задача борда
TASK-260824-1dop3s принята ревью. Сьют 1576 passed.

Правки, которые main УЖЕ содержит (переделывать не нужно):
external-build-repositories.md (секция приватного HTTPS: брокер, три
источника токена, скоупы, precheck, кроссплатформенный механизм,
предупреждение про непиненный override), cli.md (группа csk config
build-https add/login/list/remove + CSK_BUILD_HTTPS_TOKEN /
CSK_BUILD_HTTPS_HOST), reference.md (поле build_https: грамматика
скоупов, источники, precedence), CHANGELOG.md (запись в Unreleased).

## 1. README.md: quickstart для приватных репозиториев (обязательно)

Рядом с существующим SSH-блоком, тем же тоном, 6-10 строк:
- одна фраза: приватный репозиторий сборки работает и по SSH, и по HTTPS;
- для HTTPS ничего заводить не надо, если человек уже клонирует по HTTPS:
  csk предложит переиспользовать его собственные креды при первой
  установке (один Enter);
- команда для неинтерактивного случая:
  csk config build-https add gitlab.example.com/portals/infra --token git-credentials
- строка про CI: CSK_BUILD_HTTPS_TOKEN (+ CSK_BUILD_HTTPS_HOST, если
  билд-репозитории живут на разных хостах);
- закрывающая фраза в духе существующей: пакет скилла креды выбрать не
  может.

## 2. docs/skill-authoring.md §3 (schema v7): транспорт не диктует доступ

Строки ~326-330 говорят только про SSH. Переписать абзац:
- в build_repositories.*.git допустимы обе формы: git@host:path.git и
  https://host/path.git;
- приватный репозиторий в обоих случаях требует явного выбора кредов
  оператором: скоупы build_ssh и build_https соответственно;
- выбор транспорта не решает, у кого установка получится: SSH требует
  ключ, HTTPS требует существующие креды Git или токен;
- ссылку на docs/external-build-repositories.md сохранить.

ЯВНО упомянуть: HTTPS URL в манифесте обязан нести суффикс .git. Без
него GitLab отвечает 301, fetch идёт с http.followRedirects=false, и
установка падает build_repository_source_unavailable. Это требование к
манифесту; писать там, где авторы пишут URL.

## 3. docs/troubleshooting.md: две новые записи

Формат существующий (симптом, причина, команда).

build_repository_credential_policy_invalid: скоуп выбран, источник
ничего не дал. Три ветки, по команде на каждую: не установлена
переменная из token_env; token: keyring без сохранённого токена
(лечение: csk config build-https login <scope>); token: git-credentials
без записи у helper'а для хоста (клонировать один раз по HTTPS либо
login). Текст ошибки называет конкретный случай.

fatal: ... The requested URL returned error: 301: HTTPS URL без
суффикса .git; fetch не следует редиректам by design. Лечение: добавить
.git в build_repositories.*.git. Симптом ловится как
build_repository_source_unavailable, запись должна находиться и по
этому коду.

Windows-специфика (там же или в записи про login): git credential
approve рапортует успех, даже когда Windows Credential Manager
недоступен; csk ловит это перечитыванием и говорит, что делать;
рецепты: интерактивная сессия либо git config --global
credential.credentialStore dpapi. Проверено на живой Windows-машине.

## 4. docs/reference.md: матрица установки

Проверить, упоминает ли матрица вариантов установки, что компилируемые
команды (go-repository-v1) поддерживаются только на macOS и Windows.
Если нет: добавить строку, что на Linux скилл с такой командой не
установится независимо от кредов, отказ происходит до старта воркера.

## Ограничения

- Язык каждого файла сохранять, не смешивать внутри файла.
- docs/prose-style.md обязателен; тире как риторическая связка запрещено
  в любом языке.
- Секреты в примерах не показывать; токен нигде не принимается флагом.
- Каждый пример прогнать на csk из дерева (.venv/bin/csk, НЕ brew) и
  сверить с фактическим выводом.

## Приёмка

1. Ссылки из README ведут на новые разделы; проверка ссылок зелёная.
2. Ни одного примера, которого нет в CLI (сверить с
   .venv/bin/csk config build-https --help).
3. Автор скилла из одного skill-authoring.md знает про обе формы URL и
   обязательный .git в HTTPS.
4. Человек с build_repository_credential_policy_invalid находит свою
   ветку и команду в troubleshooting.md.
