# План работ: auth-паттерн bi + доку bi/youtrack

Дата: 2026-06-04. Источник правды по скиллам: `~/agents/skills/*` (origin gitlab, дефолтная ветка `master`).

## Эталоны (из исследования)

Все три эталона сводятся к одному принципу: **секрет в OS keychain, env только как fallback (а лучше — игнорится с предупреждением), токен НИКОГДА не в argv/логах**.

- **skill-youtrack**: делегирует upstream `youtrack-cli`, тот юзает PyPI `keyring` + Fernet. Никакого ручного `security` shell-out. Per-instance namespacing.
- **skill-gitlab**: делегирует `glab --use-keyring`. Свой секрет-стораж не держит.
- **skill-sentry / skill-grafana**: общий `secure_auth.py` на PyPI `keyring` (macOS Keychain нативно через Security.framework, Linux Secret Service/KWallet, Windows Credential Locker) + venv + `requirements.txt(keyring>=25,<26)` + команда `auth login|status|logout`. **Это прямой шаблон для bi**, потому что у bi нет внешнего CLI (свой Python+urllib).

`keyring` сам выбирает backend по платформе — кросс-платформенность из коробки, macOS как референс работает нативно.

## PMA-24147 — skill-bi: keychain-аутентификация

Worktree: `.temp/PMA-24147/worktree`, ветка `feature/oparin/PMA-24147`.

Текущее состояние bi: токен — обязательный `--token` argv → летит в `Authorization: Bearer` и утекает в ps/историю/логи агента. `BI_TOKEN` в SKILL.md задокументирован, но кодом НЕ читается (вранье). Хранилища нет.

Правки (порт sentry-паттерна):
1. `scripts/secure_auth.py` — скопировать verbatim из skill-sentry (skill-agnostic).
2. `scripts/run_in_skill_venv.py` — скопировать verbatim.
3. `scripts/runtime_support.py` — скопировать, заменить `SKILL_NAME="skill-bi"` и env-ключи питон-интерпретатора (`BI_SKILL_PYTHON`/`SKILL_BI_PYTHON`).
4. `scripts/requirements.txt` — `keyring>=25,<26`.
5. `scripts/bootstrap_runtime.py` — переписать на venv-bootstrap (sentry-стиль): required_paths, создание venv, pip install, проба `import keyring`, в конце вызов `bi_auth status --bootstrap-check`.
6. `scripts/bi_auth.py` — новый. `SERVICE_PREFIX="skill-bi"`, `BASE_URL="https://bi.wb.ru"` (фиксирован). Подкоманды `login|status|logout` через store/get/delete/inspect + `read_token_from_user` (getpass или `--stdin`).
7. `scripts/bi_query.py` — `--token` сделать опциональным break-glass с громким stderr-warning; резолв: explicit `--token` > `get_token(SERVICE_PREFIX, BASE_URL)`; env token-vars детектить и игнорить с предупреждением (sentry-стиль); на `MissingTokenError` — подсказка `bi-auth login`.
8. Врапперы: `scripts/bi-query` + `.cmd` прогнать через `run_in_skill_venv.py bi_query.py`; добавить `scripts/bi-auth` + `.cmd` (→ `run_in_skill_venv.py bi_auth.py`).
9. `csk-skill.json` (schema v2) — зарегать вторую команду `bi-auth`; обновить `agents/runtime.json` если есть.
10. Доку: `SKILL.md` — убрать вранье про `BI_TOKEN`, описать `bi-auth login`. (README — отдельная задача 24148.)
11. Тесты (Swift нет — тут Python): `tests/test_secure_auth.py` (из sentry) + обновить `tests/test_bi_query.py` (--token не required, keyring мокать). Прогнать.

Решения: env как fallback НЕ используем (только warn) — security. `--token` оставляем скрытым break-glass, чтобы не ломать headless резко, но canonical путь — keychain + `--stdin`.

## PMA-24148 — skill-bi: README под единый стиль

Worktree: `.temp/PMA-24148/worktree`, ветка `feature/oparin/PMA-24148`.

README сейчас — дефолтный GitLab-stub (англ, мусор). Переписать с нуля по `.temp/aid/templates/skill-readme.md` + гайд `.temp/aid/docs/skill-readme-style.md`. Секции: лид, Что делает скилл, Основные сценарии, Источники данных, Команды (включая новый `bi-auth`), Установка и интеграция (csk install + csk global; git url `...skills/skill-bi.git`, tag из последнего), Системные зависимости, Ограничения и безопасность. Контент брать из SKILL.md + scripts. Делать ПОСЛЕ 24147 (отразить новые auth-команды). Правила: русский, без `**`, без `—`, без `« »`, без make/legacy, без секций Структура/Сопровождение.

## PMA-24149 — skill-youtrack: README шлифовка

Worktree: `.temp/PMA-24149/worktree`, ветка `feature/oparin/PMA-24149`.

README подробный но по старой структуре. Сжать лид, переименовать `Что делает навык`→`Что делает скилл`, добавить таблицы Основные сценарии/Команды, удалить секции Структура репозитория/Архитектура/Обновление и сопровождение, заменить весь install (make/py -3) на csk install + csk global (tag v1.1.0), Требования→Системные зависимости (таблица), первый логин/auth спрятать в `<details>`, добавить Ограничения и безопасность, убрать все `—` (grep), снизить англо-микс. Независима от bi.

## Порядок

1. PMA-24147 (auth) — самая рискованная, делаем первой и тщательно.
2. PMA-24148 (bi README) — после, отражает финальные команды.
3. PMA-24149 (youtrack README) — независимо, можно параллельно с 24148.

Каждая ветка → отдельный MR в gitlab. Коммитим/пушим только по запросу пользователя.
