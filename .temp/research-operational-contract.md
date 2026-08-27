# Research: проверенный операционный контракт для локальных/слабых моделей

Источники:
- Codex session `019d3f69` (2026-03-30): итеративная доводка SKILL.md под слабую модель,
  ключевой вывод «execute, don't instruct».
- Codex session `019d4656` (2026-04-01): worktree/session-listing скиллы, тот же стиль.
- Проверенный артефакт: `~/agents/skills/skill-youtrack/SKILL.md` (weak-model-tuned),
  эталон стиля назван также для `glab-mr-workflow`.

Это не теория — это контракт, выстраданный через реальные регрессы слабой модели.

## Корневой принцип: execute, don't instruct

Главный вывод сессии. SKILL.md должен быть **операционным контрактом для агента**,
а не **how-to для человека**.

Слабая модель читает how-to как «вот команды, которые надо ПРЕДЛОЖИТЬ пользователю»,
а не «вот команды, которые я должен ВЫПОЛНИТЬ сам». Она не ленится — она выбирает
более безопасный режим: инструкцию вместо действия.

Триггеры неправильного поведения (что провоцирует «инструкцию вместо действия»):
- много блоков Quick Start и shell-примеров
- формулировки `Use ...`, `Run ...`, `Find the board ID`
- нет явного правила «выполни сам и верни результат»
- нет happy path для типового запроса
- tutorial-style подача

Лечение — секция `Default Mode` сверху:
- Execute the bundled commands yourself and return the result.
- Do not answer with a shell tutorial when the skill is installed and auth exists.
- Show commands only when the user explicitly asks for instructions or setup/auth is missing.

## Проверенные правила (из реальных регрессов)

### 1. Абсолютные пути от SKILL.md, не относительные от CWD
Регресс: слабая модель выполнила `scripts/yt` как путь от текущей директории → fail.
Контракт:
- Resolve command paths from the SKILL.md file path.
- Use placeholders `<yt-command>`, not literal `scripts/yt` and not shell vars `$YT`
  (shell-var форма провоцирует буквальное выполнение).
- Do not run `scripts/...` relative to CWD.
- Платформенные варианты явно: unix `scripts/yt`, windows `scripts/yt.cmd`.

### 2. Жёсткий happy path / low-freedom
Регресс: слишком много решений для слабой модели → выбирает не тот путь.
Контракт:
- Фиксированный стартовый порядок (resolve context first): один путь, без ветвлений.
- Fast path для типового запроса («мои задачи»): пронумерованный список шагов.
- Чем меньше решений у модели, тем надёжнее.

### 3. When To Ask — явные границы
Спрашивать пользователя ТОЛЬКО если:
- нет ни одного instance / контекста
- неоднозначность, которую нельзя разрешить из контекста
- auth не настроен
Во всех остальных случаях — действовать, не спрашивать.

### 4. Output Contract — обязательная форма ответа
Регресс: модель слишком агрессивно сжимала ответ → неполнота.
Контракт:
- Явно перечислить обязательные поля вывода (что нельзя пропускать).
- Не скрывать данные по умолчанию (например `Done` задачи).
- Compact payloads для agent-facing reads (без полных описаний, если не просили детали).
- Детерминированный, парсируемый формат.

### 5. Без повторных одинаковых чтений
- Do not repeat an identical successful read command.
- Reuse the first successful result unless context changed or result was incomplete.

### 6. Один completeness-check, без зацикливания
- Before final answer, do ONE completeness check against the original request.
- If not good enough, keep using tools instead of finalizing.
- Делать это ОДИН раз, не уходить в бесконечный self-check.

### 7. Языковая дисциплина (жёстко)
Регресс: модель соблюдала «в целом отвечай на языке пользователя», но мешала
английский в заголовках и хвостовых фразах.
Контракт:
- Финальный ответ ЦЕЛИКОМ на языке пользователя.
- Нельзя мешать английский в заголовки, summaries, связующий текст.
- Английский только для literal values: статусы, IDs, raw URLs.
- Отдельный пункт в pre-final check: весь ли non-literal текст на языке пользователя.

### 8. Preview-first для мутаций
- Non-destructive writes: run without `--apply` → inspect preview → re-run with `--apply`.
- Оба шага в одном turn, сам, не просить пользователя делать второй шаг руками.
- Destructive operations требуют явного намерения пользователя.
- `--dry-run` перед raw-мутациями когда эффект неочевиден.

### 9. Без ad-hoc fallback
- Do not fall back to `jq`, `grep`, `python -c`, raw keychain reads, ad-hoc REST,
  если высокоуровневая команда может сделать работу.
- На структурированную ошибку (field_type_mismatch и т.п.): не перебирать варианты
  синтаксиса флагов. Прочитать структурированную ошибку, один guided retry по её hint.

### 10. Stable JSON для agent-facing reads
- Отдельная команда для агентов, эмитящая стабильный JSON (`ytx`), отдельно от
  human-facing (`yt`).
- Prefer agent-facing команду для чтений, которые потребляет модель.

### 11. Секреты в OS keyring
- Токены в системном keyring (Keychain / Credential Locker / Secret Service).
- Не в shell history, env files, checked-in files. Без plaintext fallback.

## Мета-вывод: tool surface > SKILL.md prose

SKILL.md — это **поведенческий steering**, не жёсткий runtime hook. Гарантии нет.
Для реальных гарантий логику переносят в **tool surface**:
- dedicated «meaning commands» (`board my-tasks`) вместо того чтобы модель сама
  выбирала между низкоуровневыми командами
- answer-shaped payload (сразу готовый к ответу JSON), не `boards: [...]`
- опционально компактный query-слой (`q`)

Порядок усиления контракта:
1. SKILL.md в формате execute-don't-instruct (самый дешёвый)
2. meaning commands в CLI (переносят решения из prompt в код)
3. query-слой (самый дорогой, для масштаба)

Иначе говоря: каждое решение, вынесенное из SKILL.md в команду, — это решение,
которое слабая модель уже не может принять неправильно.

## Структура проверенного SKILL.md (skill-youtrack)

Порядок секций (сверху вниз):
1. frontmatter: name, description, triggers (локализуемые)
2. `## Default Mode` — execute-don't-instruct + язык + path resolution + self-check
3. `## Resolve Context First` — жёсткий стартовый порядок
4. `## Fast Path: <типовой запрос>` — пронумерованный happy path
5. `## Scope Rules` — границы для больших инстансов
6. `## <Domain> Reads` — команды чтения с правилами выбора
7. `## Mutations` — preview-first, правила
8. `## Safety Rules` — секреты, токены
9. setup/bootstrap — внизу, как fallback
10. `## References` — ссылки на references/*.md
