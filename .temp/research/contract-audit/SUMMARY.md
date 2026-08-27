# Аудит скиллов инфры по операционному контракту

Дата: 2026-06-05. Контракт: aid/docs/skill-operational-contract.md. Аудировались 5 скиллов инфры (csk-skill schema v2).

## Матрица соответствия

| Скилл | Вердикт | PASS | PARTIAL | FAIL |
|-------|---------|------|---------|------|
| skill-gitlab | Strong | 24 | 4 | 1 |
| skill-youtrack | Strong | 26 | 3 | 2 |
| skill-sentry | Strong | 24 | 4 | 1 |
| skill-grafana | Strong | 25 | 4 | 1 |
| skill-band | Strong | 27 | 4+ | 1 |
| skill-bi | Weak | 9 | 7 | 9 |

skill-band (добавлен 2026-06-05): Token handling Strong (secure_auth байт-в-байт как sentry, токен только в keyring, нет флага --token вообще, BAND_TOKEN/MM_TOKEN env игнорятся, login/status/logout, бэкенд allow/block-listed). Cross-platform Strong (полный symlink-resolve в band, py/python fallback в .cmd, Scripts vs bin, APPDATA vs XDG, chmod 0600 под POSIX-guard). Это эталон по токенам и кроссплатформе, строже моего bi (band вообще не имеет --token). Общий с семьёй пробел: нет нумерованного Fast Path (branchy Workflow Selection table). Band-специфично: post/dm шлют сразу, без --apply gate (исходящее действие без preview-first).

Личный тулинг (codex-list-sessions, creator, debug-codex-requests, ios-app-manager, ios-testing-tools, local-install, product-forensics) не часть инфры (нет schema v2), в аудит не входил.

## Важная оговорка по bi

bi-аудитор читал установленную копию `~/agents/skills/skill-bi` на старой ветке. Keychain-миграция (bi-auth, keyring, токен из argv) ещё НЕ вмержена, живёт в MR !5 (feature/oparin/PMA-24147). Поэтому FAIL по секретам (`--token`/`BI_TOKEN`) закрывается мержем !5.

Но остальные FAIL у bi от auth не зависят и НЕ закрываются моими MR:
- нет `## Resolve Context First` и нет нумерованного `## Fast Path` (жёсткий happy path);
- нет секции when-to-ask;
- нет output contract (обязательные поля вывода);
- нет meaning-commands: только generic `bi-query --sql <raw SQL>`, хотя триггеры обещают аналитику фич, воронки, retention;
- зависимости не объявлены `type: system`;
- references толкают сырой REST и `re.findall`-скрейп вместо высокоуровневых команд.

bi имеет Default Mode, но не имеет остального скелета контракта. Это структурный долг, отдельный от auth.

## Системные (сквозные) пробелы у сильной четвёрки

1. Зависимости не объявлены `type: system` в csk-skill.json: FAIL у youtrack и grafana (gitlab объявил glab — PASS). Системная дыра механизма объявления.
2. Структурированные error-hints для guided retry: PARTIAL почти у всех. Ошибки actionable прозой, но не машинно-структурированы.
3. Порядок секций: `## Safety Rules` ниже `## Setup Fallback` у youtrack, gitlab, grafana. Setup внизу (хорошо), но Safety должен быть выше setup. Мелочь.
4. Agent-facing stable JSON как отдельная команда / meaning-commands: PARTIAL/FAIL у sentry, grafana (analyze отдаёт только русский markdown без parallel JSON), bi.

## Конкретные фиксабельные баги (не мелочь)

- skill-gitlab SKILL.md L249 (`await-pipeline`): инструктирует модель СПРАШИВАТЬ poll interval и timeout перед опросом. Это блокирующий вопрос, прямо запрещён контрактом (неинтерактивность). Команда уже имеет дефолты 60s/900s (gmr_main.py L1270) — надо просто выполнять. Реальный FAIL, не auth-исключение.
- skill-grafana analyze.py L47-55: упавший под-запрос печатает ERROR в stderr, но возвращает заглушку и продолжает с exit 0 — пустой отчёт выглядит успешным. Нарушает формат ошибок.

## Вывод

4 из 5 скиллов инфры соответствуют контракту (Strong, только мелкие PARTIAL). bi — явный аутсайдер (Weak): auth чинится мержем !5, но структурный скелет контракта (happy path, output contract, meaning-commands) у bi отсутствует и требует отдельной работы.
