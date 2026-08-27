# README review: skill-youtrack (PMA-24149)

Worktree: `/Users/iv/Developer/Wildberries/cocoaskills/.temp/PMA-24149/worktree/README.md`
Branch: `feature/oparin/PMA-24149`
Reviewed: full diff vs old README, cross-checked against `/Users/iv/agents/skills/skill-youtrack` (SKILL.md + scripts/ytx.py + setup_main.py) and git tags.

## Verdict

Clean rewrite. No Blockers. Hard rules all pass, facts all verified. A couple of Nits only. Ready to commit after deciding on the Nit-2 wording if you care.

## Hard-rule compliance (all PASS)

- Russian: yes.
- No bold `**`: none found.
- No em-dash `—` / en-dash `–`: none found.
- No guillemets `« »`: none found.
- Install only via CocoaSkills: yes. No `make install`, no `setup_main.py`, no `bootstrap_runtime`, no legacy install anywhere.
- Forbidden sections "Структура репозитория" / "Архитектура" / "Обновление и сопровождение": none. The old "## Архитектура" section was correctly dropped.
- Long auth/platform/CLI instructions in `<details>`: yes. Auth scope/CA/platform, instance model, large-instance routing, and full CLI usage are all collapsed.
- `<details>` / `<summary>` balanced: 4 open / 4 close each. Balanced.
- Section order vs style guide (`Что делает скилл` -> `Основные сценарии` -> `Команды` -> `Установка и интеграция` -> `Системные зависимости` -> `Ограничения и безопасность`): matches the recommended structure. `Аутентификация` + the `<details>` blocks sit between install and limits, which is fine (guide says not all sections are mandatory and order is "recommended").

## Factual cross-check (all PASS)

- `yt` / `ytx`: correct. `yt` = full upstream CLI (instances, boards, sprints, issues, comments); `ytx` = agent wrapper with stable JSON + mutations. Matches SKILL.md and ytx.py.
- Auth/login flow preserved (in `<details>`): instance-named login (`--instance primary auth login --base-url ...`), keyring storage, scoped board ids at login (`--board-id` repeated), custom CA flags (`--cert-file`, `--ca-bundle`, `--no-verify-ssl`). All present and correct.
- Instance model precedence preserved: 1) `--instance`, 2) `YOUTRACK_INSTANCE`, 3) pinned active instance, 4) single registered instance. Matches SKILL.md exactly.
- `instances` subcommands preserved: `list`, `current`, `use`, `scope set`, `scope clear`, `rename`, plus `auth status` / `auth logout`. All real (confirmed in ytx.py parsers; `scope`/`rename` live in yt_main, consistent with the `yt` prefix used).
- `--mine` behavior preserved: "определяет текущего разработчика из `git config user.email`". Correct. (Minor: see Nit-2 re: dropped detail.)
- Preview-first (`--apply`) flow preserved: yes, including same-turn apply and ask-only-on-ambiguity, plus `--dry-run` for raw commands. `board-add` / `board-remove` preserved (confirmed as real parsers in ytx.py).
- Upstream attribution preserved: `youtrack-cli==0.22.2`, `yt-cli`, Ryan Cheley (lead says "Райана Чили", license footer says "Ryan Cheley"), MIT, homepage URL. Present in both lead paragraph and `## Лицензия`.
- Install tag `v1.1.0`: CORRECT. Latest git tag of skill-youtrack is `v1.1.0` (tags: v1.1.0, v1.0.0). Both the `Skillfile.json` block and the `csk global add` line use v1.1.0. Old README used v1.0.0; bump is right.

## Findings

### Blocker
None.

### Should-fix
None.

### Nit

Nit-1 (line 73) — `Python 3.10 - 3.13` uses spaced hyphen as a range. It is an ASCII hyphen (not an em/en-dash), so it does NOT violate the no-dash rule. Old README wrote `Python 3.10-3.13`. Purely cosmetic; leave as-is or close up to `3.10-3.13` for consistency with the package pin style. No action required.

Nit-2 (line 24, 180) — `--mine` description is slightly shorter than the source. SKILL.md spec: "resolves from `git config user.email`, falls back to global git config, takes the localpart before `@`, and searches YouTrack users; if ambiguous, fails with candidate users." The README keeps only the `git config user.email` part. This is acceptable for a human-facing README (the full resolution+ambiguity logic is agent behavior that belongs in SKILL.md), but if you want it airtight, line 180 could add a half-sentence: that an ambiguous match fails and asks which account to use. Optional.

Nit-3 — Old README's install locale modes (`en`, `ru`, `en-ru`, `ru-en`) and the `LOCALE=<locale>` / `--locale` install knob were dropped. ASSESSMENT: dropping is ACCEPTABLE and correct. Those modes belonged to the legacy `make install` / `setup_main.py` path, which is now banned in favor of CocoaSkills-only install. CocoaSkills install via `Skillfile.json` has no locale knob, so documenting `LOCALE` would contradict the install section. The locale machinery still exists in `setup_main.py` (`SUPPORTED_LOCALE_MODES`) for whoever drives setup directly, but it is not part of the documented CocoaSkills flow. No action — do not re-add.

Nit-4 — The `<details>` summary "Использование CLI: чтения, мутации, привязка к спринту" bundles four topics into one collapsible. Fine for README density, no rule against it. Note only: the example set inside was trimmed (e.g. `board scoped-issues --mine`, `board issues --source web`, `issue create-subtask`, `comment-list` no longer all shown). Trimming is acceptable for a README that points to SKILL.md as the working reference; all trimmed commands remain documented in SKILL.md. No action.

## Bottom line

Style-compliant and factually accurate. No blockers, no should-fixes. The two optional Nits (1 and 2) are cosmetic. Safe to commit.
