# Documentation Feedback, Round 2

Status: for execution. Owner: orchestrator session, 2026-08-21.
Follows `.spec/docs-refresh.md`; this document amends it where they
disagree. Feedback sources: WB team review (Maksim Malyshev) and the
owner's market landscape appendix (`.research/260821_market-landscape-source.md`).

## Directives

1. `README.en.md` is removed. The repository is Russian-first: the root
   `README.md` is the single README. Anything valuable in `README.en.md`
   that has no home in `docs/` moves into `docs/` before deletion.
   `pyproject.toml` `readme` returns to `README.md`; the PyPI page
   becomes Russian, which the owner accepts. Release contract tests must
   pass.
2. `docs/skill-authoring.md` is rewritten in Russian (same file name,
   content translated and restyled per `docs/prose-style.md`; code
   blocks, JSON, and identifiers stay as they are).
3. The market landscape goes near the top of `README.md`, distilled, not
   pasted: the comparison table of Vercel skills.sh, SkillKit, Tessl,
   NVIDIA Skills / SkillSpector, Agent Plugins, and the CocoaSkills
   position (the six load-bearing properties). Source in
   `.research/260821_market-landscape-source.md`; typography must follow
   `docs/prose-style.md` (no em-dashes, no guillemets).
4. The "Почему CocoaSkills, а не альтернативы" section is restructured
   for scanability: per-alternative bold lead-in or subheading with two
   short points (where it breaks, what `csk` does instead). Team
   feedback: the five-paragraph wall is hard to read.
5. Quick start gains collapsible install options: `<details>` blocks for
   pipx (recommended, open by default), uv, Homebrew, mise, pip.
6. `README.md` gains a "Команды" section in the skillkit style:
   collapsible `<details>` groups, each a code block of one-line
   `command  # что делает` entries, with a link to a new full reference
   `docs/cli.md` (Russian). `docs/cli.md` is the single source for
   flags, arguments, and examples; it absorbs the CLI table from
   `README.en.md` and is verified against `csk --help` output.

## README.md target layout after this round

1. Definition (unchanged).
2. Зачем (unchanged).
3. Рынок и позиция CocoaSkills (new, distilled market landscape:
   short intro, comparison table, six properties as a compact list,
   one-sentence conclusion).
4. Почему CocoaSkills, а не альтернативы (restructured per directive 4;
   covers the DIY approaches: copies/symlinks, submodules, agent
   marketplaces, synced monorepo dir).
5. Быстрый старт (with install spoilers).
6. Режимы установки (unchanged).
7. Команды (new, collapsible groups + link to docs/cli.md).
8. Дальше (updated links; no README.en.md).

## Language policy update

Russian is the primary documentation language: `README.md`,
`docs/skill-authoring.md`, `docs/cli.md` are Russian. `ARCHITECTURE.md`
and `SECURITY.md` stay English for now (no directive to translate).
`CONTRIBUTING.md` records this policy; `CONTRIBUTING.ru.md` mirrors it.

## Style notes

`docs/prose-style.md` stays binding. Two amendments the style task
should fold into the guide:

- Comparative overviews may use a structured breakdown (table or
  per-item lead-ins with short points) when readers need to scan;
  reasoning inside each item stays prose.
- Collapsible `<details>` blocks are allowed for parallel variants
  (install methods, command groups); the summary line names the variant
  exactly.

## Execution plan

Story `community-feedback` under epic `docs-refresh`, task_class=docs.

| Task | Scope | Depends on |
| --- | --- | --- |
| ru-readme-restructure | Directives 3, 4, 5 in README.md; style guide amendments | - |
| cli-reference | docs/cli.md (Russian, verified vs csk --help) + Команды section in README.md | ru-readme-restructure |
| drop-en-readme | Move remaining value from README.en.md into docs/, delete it, pyproject readme=README.md, update CONTRIBUTING policy both files, fix links | cli-reference |
| skill-authoring-ru | docs/skill-authoring.md rewritten in Russian | - |
| open-pr-round2 | Branch, commit, PR to GitHub main, merge per owner instruction, sync WB GitLab | all above |

Writers: agy `gemini-3.6-flash-high` with the mandatory shell-only
tooling note. Reviewers: claude-opus-5. Same producer/reviewer loop as
round 1.
