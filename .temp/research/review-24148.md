# Review: README.md change (PMA-24148, branch feature/oparin/PMA-24148)

Repo: skill-bi (git@gitlab.wildberries.ru:portals/partner-mobile/agentic-infra/skills/skill-bi.git)
Diff scope: command table + Конфигурация + new Аутентификация + new Системные зависимости + Ограничения.
Cross-checked against auth source in /Users/iv/Developer/Wildberries/cocoaskills/.temp/PMA-24147/worktree/scripts/.

## Verdict

The change is factually solid. The auth section is a faithful mirror of SKILL.md (lines 89-95) and matches the real code. No style hard-rule violations (no bold, no em-dash, no guillemets, no make install, no forbidden sections). The `<details>` blocks are balanced (one open at line 83, one close at line 96). Main issues are a stale bullet left over from the old bi-catalog framing and one en-dash usage to double-check.

## Blocker

None.

## Should-fix

1. Line 13 — stale "Навигация по дашбордам" bullet still describes the OLD bi-catalog.
   The command table (line 44) was correctly rewritten: bi-catalog now = event catalogs (path/list/save/validate-sql/annotate/load), NOT dashboards. But the "Что делает скилл" list at line 13 still reads:
   "- Навигация по дашбордам: листинг дашбордов, разбор виджетов и их `dataSourceId`, чтение SQL сохранённых запросов."
   Dashboards are now bi-query/references territory (references/dashboards.md), which is fine as a capability — but as written this bullet reads like it belongs to a catalog/navigation command and now contradicts the table. Verified: no script does dashboard navigation; it lives in references/dashboards.md and is driven by bi-query.
   Fix: keep the capability but reframe it as bi-query/SQL-driven, e.g.
   "- Дашборды и сохранённые запросы: через bi-query и references/dashboards.md (листинг, разбор виджетов и их dataSourceId, чтение SQL сохранённых запросов)."
   Note: line 13 is outside the diff hunks, so technically pre-existing — but the table change directly orphans it, so fix it in this commit.

2. Line 114 — "Python 3.10 - 3.13" uses a hyphen-with-spaces that reads like a range dash; confirm it is a plain ASCII hyphen "-" and not an en-dash "–".
   Factual range is correct (runtime_support.py: SUPPORTED_MINORS = (13, 12, 11, 10), message "Expected Python 3.10-3.13"). Style guide bans em-dash; en-dash in a numeric range is a gray area. Safest: write "Python 3.10-3.13" with no surrounding spaces and a plain hyphen, matching the source string exactly.

3. Install tag v1.0.0 — FLAG AS QUESTION, do not silently keep.
   Latest git tag of skill-bi is v1.0.0 (confirmed: only tag present). Auth (bi-auth, keyring storage, BI_TOKEN-ignored, --token break-glass) is a new user-facing feature added after v1.0.0. The README now documents behavior that does NOT exist in the v1.0.0 release. If someone installs `"tag": "v1.0.0"` they get a skill whose README (this one) describes commands the tagged code lacks.
   Action: decide before commit whether to cut a new tag (e.g. v1.1.0) for the auth feature and bump the install snippets at lines 55 and 68 accordingly. Do not assume — confirm with the release owner. As-is, README and tagged code are out of sync.

## Nit

4. Line 100 — keyring backend list "(macOS Keychain, Windows Credential Locker, Linux Secret Service или KWallet)" matches the code's allowed backends (secure_auth.py ALLOWED_BACKEND_PREFIXES: macos, secretservice, kwallet, windows). Accurate. No change needed; noted only because the same list is duplicated at lines 100 and 115 — fine, but keep them consistent if either is edited.

5. Line 108 / line 119 — slight redundancy: "Токен ... не попадает в командную строку, историю shell или логи" (line 100/108) and "Токен хранится только в системном keyring и никогда не выводится в ответах, файлах, командах или логах" (line 119) say nearly the same thing across two sections. Acceptable (different sections, different emphasis), but could tighten if trimming.

6. Section order vs guide: guide order is Что делает / Основные сценарии / Источники данных / Команды / Установка / Системные зависимости / Ограничения. README inserts Конфигурация and Аутентификация between Установка and Системные зависимости. The guide says auth details belong in a `<details>` block; here Аутентификация is a top-level section. This is a reasonable deviation (auth is now a first-class feature worth surfacing) and not a hard-rule violation, but if strict guide adherence is wanted, the bi-auth login/status/logout block could be wrapped in `<details>`. Low priority.

## Confirmed correct (no action)

- bi-catalog table description (line 44): accurate — per-feature event catalogs, path/list/save/validate-sql/annotate (and load). Does NOT navigate dashboards. Matches bi_catalog.py.
- bi-auth table description (line 45): accurate — keyring storage keyed by base URL, login/status/logout. Matches bi_auth.py / secure_auth.py.
- BI_TOKEN ignored with warning (line 108): accurate — bi_query.py:265-272 prints "Ignoring token environment variable(s)..." and never uses it.
- --token discouraged break-glass (line 108): accurate — bi_query.py:273-279 warns it is visible in process table/shell history/logs; argparse help (lines 298-304) says the same.
- Token keyed by base URL (lines 81, 100): accurate — secure_auth.service_name = f"{prefix}:{normalize_base_url(...)}".
- --stdin login and hidden prompt (line 108): accurate — secure_auth.read_token_from_user uses getpass or stdin; bi_auth login has --stdin.
- Removal of BI_TOKEN row from config table (line 79) and the "Токен здесь не задаётся" note (line 81): correct given env token is ignored.
- `<skill-path>/scripts/bi-auth ...` invocation style (lines 103-105): matches SKILL.md lines 89-92 exactly.
- Install commands present and correct: Skillfile.json block (lines 51-57), `csk install` (line 62), `csk global add ... --git ... --tag v1.0.0` + `csk global install` (lines 68-69). No make install / legacy paths. Only the tag value is in question (see Should-fix 3).
- No "Структура репозитория" / "Сопровождение" sections present.
- `<details>` balance: 1 open / 1 close, properly nested.
