# TASK-260821-3s96o5 Review Verdict: changes requested

Reviewer run: RUN-260821-82e9d8 (claude-opus-5). Run is not goal-bound
(`task-board spawn goal` returned "none").

Verdict: **changes_requested -> to-dev**. The three directives are
implemented and the substance is good, but the change ships one broken
markup regression outside scope and one factual claim that contradicts the
code. Both are hard AC failures.

## Blocking findings

### B1. License badge destroyed (`README.md:5`)

The badge image URL was replaced with the LICENSE page URL:

```
-[![License](https://img.shields.io/pypi/l/cocoaskills.svg)](https://github.com/ivanopcode/cocoaskills/blob/main/LICENSE)
+[![License](https://github.com/ivanopcode/cocoaskills/blob/main/LICENSE)](https://github.com/ivanopcode/cocoaskills/blob/main/LICENSE)
```

`![...](url)` renders `url` as an image `src`. The new `src` is an HTML
page, not an SVG, so the badge renders as a broken image on GitHub and on
the PyPI project page. This edit is also outside the task scope
("README.md sections 3-5 ... No other files"), is not mentioned in the
outcome report, and was not requested by any directive.

Fix: restore `https://img.shields.io/pypi/l/cocoaskills.svg` as the image
URL and keep the LICENSE blob URL as the link target.

### B2. Adapter claim contradicts the code (`README.md:53`)

New text:

> Что делает `csk`: хранит единый файл `Skillfile.json` в проекте и
> генерирует адаптеры для всех поддерживаемых агентов.

Code says otherwise. `src/csk/adapters.py:25`:

```python
NATIVE_DISCOVERY_AGENTS = frozenset({"windsurf", "opencode"})
```

`src/csk/cli.py:334` states it in the help text: "opencode and windsurf
read .agents/skills natively, no mirror is created". Adapters are
generated for Claude Code, Codex CLI, Cursor and Gemini only.

The pre-change paragraph carried this correctly ("раскладывает скиллы по
адаптерам Claude Code, Codex CLI, Cursor и Gemini, а OpenCode и Windsurf
читают канонический каталог `.agents/skills/` напрямую"); the restructure
dropped the distinction and replaced it with a false generalization. It
also contradicts `README.md:73` (`csk init` writes only `.claude/skills/`,
`.codex/skills/`, `.cursor/rules/`, `.gemini/skills/`).

This fails the AC "all factual claims about csk match the code" and the DoD
"No discrepancies between code and description".

Fix: name the four adapter agents and state that OpenCode and Windsurf read
`.agents/skills/` natively. Keep it inside the two-point structure, for
example: "Что делает `csk`: хранит единый `Skillfile.json` и раскладывает
скиллы по адаптерам Claude Code, Codex CLI, Cursor и Gemini; OpenCode и
Windsurf читают `.agents/skills/` напрямую."

## Non-blocking findings (fix while in there)

### N1. Missing blank line in the style guide (`docs/prose-style.md:87`)

The comparative-overview amendment starts on the line directly after the
"Numbered steps are for procedures..." paragraph, with no blank line. Both
render as one merged paragraph. Insert a blank line before "Comparative
overviews may use...". The second amendment (`<details>` blocks) is
separated correctly.

### N2. Test evidence in the outcome report does not match reality

`TASK-260821-3s96o5_results.md` reports `1661 passed in 10.45s (exit code:
0)`. The actual suite on this working tree:

```
1418 passed, 243 skipped, 24 warnings in 269.42s (0:04:29)
```

1418 + 243 = 1661, and 10.45s is not a plausible wall time for a 4.5-minute
run. The suite is green, so this is not a functional problem, but the
reported evidence was not transcribed from a real run. Future outcome
reports must paste the actual pytest summary line.

## What passes

- Directive 3. `## Рынок и позиция CocoaSkills` sits directly after
  `## Зачем`. The GFM table covers all five required entries (Vercel
  skills.sh, SkillKit, Tessl, NVIDIA Skills / SkillSpector, Agent Plugins)
  with the four source columns, distilled rather than pasted. The six
  properties are a compact parallel list, closed by a one-sentence
  conclusion.
- Property claims check out against the code: conflict detection
  (`src/csk/closure.py:182` "Version conflict for ..."), source allowlist
  (`src/csk/source_identity.py:119`), three-layer materialization
  (`ARCHITECTURE.md:23` "A schema-6 skill may materialize three independent
  layers"), transactional commit with rollback (`src/csk/transactions.py`,
  `src/csk/installer.py:285`), canonical `.agents/skills/`, local audit
  gate (`ARCHITECTURE.md:117`).
- Directive 4. Four alternatives, each with a bold lead-in and exactly two
  points ("Где ломается" / "Что делает `csk`"). The closing
  what-csk-is-not paragraph is preserved verbatim. Only B2 is wrong inside
  it.
- Directive 5. Five `<details>` blocks, `pipx` first with `open`. Every
  command matches the install matrix in `README.en.md:129-167` byte for
  byte (`pipx install cocoaskills`, `uv tool install cocoaskills`,
  `brew tap ivanopcode/csk` + `brew install cocoaskills`,
  `mise use -g pipx:cocoaskills@latest`,
  `python -m pip install --user cocoaskills`). The blocks sit at 3-space
  indent inside list item 1, which GFM and GitLab both render: the raw HTML
  block ends at the blank line, the fence parses as list-item content. The
  stale "полная матрица ... в файле `README.en.md`" pointer was correctly
  dropped.
- Style amendments are present in `docs/prose-style.md` under
  "Lists vs prose" and match the spec Style notes wording.
- Typography: `grep -n '—\|–\|«\|»' README.md` returns nothing. The only
  hits in `docs/prose-style.md` (lines 143, 146, 171) are the guide's own
  Bad examples, unchanged.
- Tests green: `1418 passed, 243 skipped in 269.42s`.
- Scope: `docs/skill-authoring.md` in the same working tree belongs to
  TASK-260821-1h8thl, not this task. `LOGBOOK.md` carries this task's
  entry, which is expected.

## Routing

Status set to `to-dev`. Fix B1, B2 and N1, re-verify with
`grep -n 'img.shields.io' README.md` and a fresh `git diff README.md`, then
return to review. No commit evidence is recorded: this run is a reviewer
archetype and did not accept the work.
