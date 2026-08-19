# Documentation Refresh

Status: draft for execution. Owner: orchestrator session, 2026-08-19.
Board scope: epic `docs-refresh` (see Execution plan).

## Motivation

The current documentation grew feature by feature. README.md is an
838-line English reference that mixes pitch, tutorial, and schema
reference. The Russian README is a translation that trails the source.
ARCHITECTURE.md describes mechanisms but rarely explains why a
mechanism was chosen. A newcomer cannot answer three questions quickly:
why this tool exists, why it beats the alternatives, and what the first
five commands are.

This refresh restructures the documentation around two audiences:

1. A user who wants to install skills into a project. They read the
   root README and stop.
2. A contributor or security reviewer who needs the internal model.
   They read ARCHITECTURE.md and SECURITY.md.

## Language policy

- `README.md` (repo root): Russian. Primary entry point.
- `README.en.md`: English, full content parity with `README.md`.
  Each file links to the other in the first screen.
- `ARCHITECTURE.md`, `SECURITY.md`, `docs/*`: English only.
- `ARCHITECTURE.ru.md` and `SECURITY.ru.md` are removed. `README.md`
  (Russian) links to the English internals docs directly.
- `README.ru.md` is removed; its role is taken by the new root
  `README.md`.
- `pyproject.toml` `readme` switches to `README.en.md` so the PyPI
  page stays English. The release contract tests
  (`tests/test_release_contract.py` and packaging config) must pass
  after the switch; if hatchling excludes the file from the sdist,
  add it explicitly.
- Site pages under `docs/` (index.html, sitemap) that link to
  `README.ru.md` or the removed `.ru` docs must be updated in the same
  change.

## Target document set

### README.md (Russian, rewritten)

Short and task-oriented. Sections, in order:

1. Definition. One paragraph: what `csk` is, what it manages, which
   agent environments it serves. No feature list.
2. Зачем (why the problem is real). One short section: drift between
   machines, no pinning, README/tests leaking into agent context, no
   cleanup on removal.
3. Почему CocoaSkills, а не альтернативы. Honest comparison, one
   paragraph per alternative:
   - manual symlinks / copied skill folders per repo;
   - git submodules / subtree;
   - agent-native plugin marketplaces (cover per-agent lock-in and
     absence of cross-agent installs);
   - a shared monorepo directory synced by hand or by shell scripts.
   For each: what it gives, where it breaks, what CocoaSkills does
   instead. Close with what CocoaSkills does NOT try to be (not a
   registry, not an agent runtime, not an MCP manager).
4. Быстрый старт. Install (pipx one-liner plus a link to the full
   install matrix in README.en.md or a section below), `csk init`,
   first `csk add`, `csk install`, verify in an agent. Numbered steps,
   copy-pasteable, each with its expected observable result.
5. Режимы установки скиллов. Three subsections with one worked
   example each:
   - Проектный (local): `Skillfile.json` in the repo, committed,
     reproducible for the whole team.
   - Глобальный (global): once per machine under
     `~/.cocoaskills/global/`, visible outside any checkout.
   - Гибридный (hybrid): declared once per machine in
     `~/.cocoaskills/hybrid/Skillfile.json`, activated per target
     project by alias, path, or glob; nothing is committed to target
     repos. State explicitly this is the mode for workflow/process
     skills a platform team rolls out to selected repositories.
   Include the shadowing order: project, then hybrid, then global.
6. Дальше. Links: README.en.md, ARCHITECTURE.md, SECURITY.md,
   docs/skill-authoring.md, CHANGELOG.md.

Everything else that lives in the current README (schema versions,
compiled commands, audit registry, CLI reference) moves out of the
Russian README; it stays in README.en.md or in docs/.

### README.en.md (English)

Full parity with the Russian README plus the reference material the
root README no longer carries: install matrix, skill dependencies,
command manifests, compiled commands overview (with a link to
ARCHITECTURE.md for the contract), audit and registry, CLI table,
development and documentation indexes. Existing README.md content is
the raw material; the rewrite applies the new structure and the style
guide rather than copying paragraphs verbatim.

### ARCHITECTURE.md (English, extended)

Keep the current structure (core concepts, install pipeline, module
map, storage layout, schema-6 contract, security boundaries, testing)
and add the rationale layer:

- For each load-bearing decision, a short "why" paragraph next to the
  mechanism: whitelist-based stripped layout, one canonical
  `.agents/skills/` root with per-agent adapters, content-hashed
  installs, protected build cache, manager-owned execution, the audit
  gate, fail-closed installs.
- A "Security model" section that states the threat model in plain
  terms (untrusted skill repositories, compromised refs, context
  poisoning, command execution boundary) and maps each threat to the
  mechanism that answers it. SECURITY.md remains the reporting policy
  and hardening checklist; ARCHITECTURE.md explains why the boundaries
  sit where they sit. Cross-link both ways, no duplicated text.

### Style guide (docs/prose-style.md, English)

A committed style guide for anyone writing CocoaSkills documentation,
synthesized from three sources: The Go Programming Language (Donovan,
Kernighan), The Swift Programming Language, and the Russian
engineering-prose rules below. CONTRIBUTING.md gets a one-line pointer
to it. The guide also carries the AI-slop blacklist used by reviewers.

## Prose rules (binding for all tasks)

English prose:

- Lead with the definition; consequences follow. "X is Y. Use X to Z."
- Name the actor: the installer copies, the audit gate rejects, you
  run. No "it should be noted", no agentless passives where the agent
  matters.
- One new concept per paragraph. A paragraph states a claim, explains
  it, and shows the consequence or an example.
- Prefer running prose to bullet lists. A list is for genuinely
  enumerable items (flags, file names), not for argument structure.
- Repeat the term instead of a pronoun whenever the referent could be
  ambiguous.
- State caveats next to the claim they limit, not in a separate
  "limitations" dump.
- No marketing register: no "powerful", "seamless", "robust",
  "battle-tested", no exclamation points.

Russian prose (инженерная проза):

- Субъект действия назван: "установщик копирует", not "производится
  копирование".
- Глагол вместо отглагольного существительного: "проверяет", not
  "выполняет проверку".
- Определение до следствий; одно новое понятие за раз.
- Термин повторяется там, где местоимение создаёт неоднозначность.
- Технические термины остаются на английском (symlink, ref, commit),
  без перевода и без кавычек.
- Никакого канцелярита: "при необходимости", not "в случае наличия
  необходимости".

AI-slop blacklist (both languages; reviewer rejects on sight):

- Antithesis constructions: "it's not X, it's Y", "не просто X, а Y",
  "this isn't about X".
- Em-dashes as rhetorical glue. In Russian text, use the hyphenated
  dash sparingly for syntax the grammar requires, never as a stylistic
  tic; in English prefer commas, colons, or separate sentences.
- Russian guillemets (« »). Use plain quotes only where a quote is
  genuinely needed; prefer code formatting for identifiers.
- Chains of three-item enumerations and adjective triples.
- Summary paragraphs that restate the section just written.
- "Let's", "we'll dive into", "давайте разберёмся", "стоит отметить".

## Execution plan

Board epic `docs-refresh`, task_class=docs everywhere.

| Story | Task | Depends on |
| --- | --- | --- |
| style-foundations | orchestrator attaches style-guide + outline resources (no spawn) | book extraction research |
| readme-flip | TASK ru-root-readme: write new Russian README.md, remove README.ru.md, update links | style resources |
| readme-flip | TASK en-readme: write README.en.md, switch pyproject readme, fix site links, keep tests green | ru-root-readme |
| internals | TASK architecture-rationale: extend ARCHITECTURE.md, add security model section, remove .ru internals docs, cross-link SECURITY.md | style resources |
| internals | TASK prose-style-doc: commit docs/prose-style.md from the style resource, point CONTRIBUTING.md at it | style resources |
| audit | TASK slop-audit: final sweep of every shipped doc against the blacklist and style guide | all above |

Writers: `task-board spawn ... --role doc-writer --background --agent agy
--model gemini-3.6-flash-high --timeout 35m`. The user asked for Gemini
3.7 Flash or 3.6 Flash; the agy contract exposes no 3.7, so the pinned
model is `gemini-3.6-flash-high`. Reviewers are spawned per the normal
review loop with an explicitly selected stronger model; Russian prose
review requires a model competent in Russian.

Producers do not commit. After the slop audit is accepted, the
orchestrator commits the reviewed diff on a dedicated `docs-refresh`
branch and opens a pull request into `main` on
github.com/ivanopcode/cocoaskills; the open PR is the final deliverable.
Commits carry no Co-Authored-By or AI attribution lines.

## Decisions

1. Root README language flips to Russian; English parity file is
   `README.en.md`. PyPI reads `README.en.md`.
2. `.ru` variants of internals docs are removed rather than kept
   stale; the language policy section above is the durable rule.
3. Hybrid skills are documented as an existing shipped mode. The
   earlier assumption that hybrid mode does not exist is wrong:
   `csk hybrid add` shipped with the scopes-and-gc epic
   (STORY-260712-df8495) and is already described in the current
   README.
4. The style guide is a committed doc, not only a board resource, so
   future contributions inherit it.

## Open questions

- Whether GitHub renders README.md (Russian) for an audience that is
  mostly English-speaking is a product call; current owner decision is
  Russian-first, revisit if the repo goes public-first.
- docs/ site (index.html) currently mirrors README content by hand;
  a follow-up could generate it, out of scope here.
