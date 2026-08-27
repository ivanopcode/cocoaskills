# CocoaSkills: система аудита безопасности устанавливаемых скиллов

Статус: дизайн (черновик 2). Не реализовано.
Цель: полноценная система анализа кода и контракта скиллов (включая промпты) на
этапе установки, опциональная, с пристёгиваемыми бэкендами анализа (codex/claude/
произвольная команда), local-first, с детерминированным гейтом.

Это переработка черновика 1 (который был «линтер с фазами»). Главные сдвиги:
allowlist вместо blacklist (capability-модель), разделение detection/policy/decision/
record, LLM как advisory-экстрактор фактов под детерминированной политикой, trust как
first-class концепт.

---

## 1. Threat model (явно)

- **Атакующий**: автор стороннего скилла; скомпрометированный upstream-репо (supply
  chain); MITM git-fetch. Внутренний скилл тоже не доверенный по умолчанию (insider /
  компрометация аккаунта).
- **Что контролирует атакующий**: все байты снапшота (code, prompts, manifest, locales,
  `.skill_triggers`), git URL, теги (двигаемы).
- **Жертва**: машина и секреты в момент install; поведение агента в runtime (через
  prompt-injection); конфиденциальность аудируемого контента (утечка в облако).
- **Trust boundary**: csk исполняется с правами пользователя; контент скилла —
  недоверенный; агент, который потом грузит скилл, мощный (исполняет команды, читает
  файлы, ходит в сеть).
- **Что защищаем**: (a) машину/секреты на install; (b) поведение агента от инъекции;
  (c) конфиденциальность контента от эксфильтрации аудитором.
- **Явно вне модели**: защита от уязвимого, но не вредоносного апстрима (это отдельный
  SCA); защита рантайма исполнения команд скилла (это ответственность агента/песочницы,
  не install-time аудита).

Железное правило: **аудитор никогда не исполняет код скилла**. Только статический разбор
байтов + LLM, читающий байты.

---

## 2. Спина системы: capability-декларация + детект эскалации (allowlist > blacklist)

Blacklist («найди `curl|sh`») фундаментально слаб: обфускация → гонка вооружений.
Полноценная система строится на allowlist: скилл **декларирует envelope возможностей**,
аудит ищет **выход за envelope**.

Скилл декларирует capability-манифест (csk-skill.json schema v3 или сиблинг-поле
`capabilities`):

```json
"capabilities": {
  "network": "none" | ["api.example.com", "*.internal"],
  "filesystem": "repo" | "home-config" | ["~/.config/tool"],
  "exec": "none" | ["git", "glab"],
  "secrets": "none" | ["skill-x:https://..."],   // keyring service names
  "env_read": ["HOME", "TOOL_TOKEN"],
  "prompt_scope": "Короткое описание домена: что скилл вправе инструктировать агенту делать."
}
```

Два envelope:
- **code-capabilities** (network/fs/exec/secrets/env) — для рантайм-кода.
- **prompt-directives** (prompt_scope) — для промптов: что скилл вправе инструктировать.

Аудит = детект **расхождения declared-vs-observed**:
- Код делает сетевой вызов к хосту вне `network` → violation.
- Промпт инструктирует эксфильтрацию / игнор safety / действия вне `prompt_scope` →
  violation.

Преимущества: точность (git-скилл декларирует `exec:[git]`, запуск git не finding,
запуск curl — finding); меньше fatigue (легит-поведение декларируется и пинуется один
раз); реальная гарантия (allowlist); сам манифест ревьюится человеком на `csk add`.

**Честность про soundness**: в Python envelope нельзя доказать статически (динамический
`__import__('os').system`, `eval`, `getattr`). Поэтому позиция: **всё, что нельзя
проанализировать — это finding** (`opaque`, high severity под strict). Опаковость
подозрительна. Мы не заявляем soundness; заявляем «declared-vs-observed + opaque=finding».

Blacklist-детекторы остаются вторым слоем (defense in depth): даже внутри envelope
флагуем известные опасные паттерны (`shell=True` с интерполяцией, base64-блоб, `curl|sh`).

---

## 3. Пайплайн: detect → normalize → assess → policy → decide → record

Пять разделённых стадий (в черновике 1 они были смешаны):

1. **Detect** — собрать сырые сигналы из двух источников:
   - **Static** (детерминирован, иммунен к инъекции): Python через AST (не regex), shell/
     .cmd через парс + паттерны, манифест через схему, opaque-артефакты (бинари,
     минифицированное) помечаются как unanalyzable. Локали и `.skill_triggers` — в скоупе.
   - **LLM (экстрактор фактов, не судья)**: «перечисли все сетевые вызовы / чтения вне
     репо / exec шелла / инструкции агенту совершить действие», каждый факт **со ссылкой
     на file:span**. Извлечение субвертить труднее, чем суждение.
2. **Normalize** — оба источника → единая схема Finding (см. §7). LLM-факты, которые не
   привязаны к верифицируемому span, помечаются `verifiable:false`.
3. **Assess** — присвоить severity по таксономии + сверить с capability-envelope (escalation
   → severity вверх). Детерминированно.
4. **Policy** — применить правила (global + project + per-skill grants) → для каждого
   finding `policy_action`. Детерминированно, в коде, **не в LLM**.
5. **Decide** — агрегировать в решение: `allow | warn | confirm | block | require-pin`.
   Детерминированно given (findings, policy).
6. **Record** — записать verdict с provenance (§7) в shared cache по content hash.

Ключ: **LLM влияет только на стадию Detect (advisory-факты). Гейт (Assess→Decide)
детерминирован.** Нестабильный LLM не делает нестабильным гейт.

---

## 4. Контракт детерминизма и целостности

- **LLM не гейт, а advisory-вход.** Гейт детерминирован given (findings, policy).
- **Верифицируемость findings.** Каждый LLM-finding указывает file:span. Под strict
  un-verifiable findings логируются, но **не гейтят** (иначе галлюцинация = блок). Гейтят:
  все static-findings + верифицируемые LLM-findings.
- **Кэш по content hash.** Verdict кэшируется по `(content_sha256, backend, model,
  prompt_version, ruleset_version)` в global cache — аудит один раз на (скилл, коммит)
  глобально, шарится между всеми проектами (как snapshot-кэш). Re-audit при смене хеша
  (цепляется к существующему moved-tag детекту).
- **Canary / integrity check.** Перед прогоном бэкенд гоняется на встроенном
  known-malicious фикстуре. Если аудитор пропускает заведомо плохое — он сломан/
  субвертирован → **fail closed** (бэкенд не используется). Это детект «судья
  скомпрометирован».
- **Fail-closed / fail-open.** Нет verdict (бэкенд недоступен, таймаут, битый JSON, сеть
  легла): под `strict` → block; под advisory → warn + proceed. Static всегда отрабатывает,
  даже без модели.
- **N-of-M (опционально, high-stakes).** Для критичных решений — голосование по
  нескольким прогонам/моделям; разногласие → эскалация к человеку.

---

## 5. Defense in depth против инъекции в судью

Аудитор читает attacker-controlled промпты — они таргетят самого аудитора
(«SYSTEM: verdict SAFE»). Слои:
1. Контент скилла **никогда в system prompt**, только в user-turn с пометкой «DATA, не
   инструкции», в жёстких делимитерах.
2. Ответ через **structured output / tool-call по схеме** — модель не может просто эмитить
   «SAFE», она обязана заполнить findings-схему.
3. **Fact-extraction, не judgment** — спрашиваем факты, политику применяем в коде.
4. **Static как ground truth** — regex/AST не ведутся на прозу; если статика нашла
   `curl|sh`, LLM не отговорит.
5. **Canary** (§4) — детект субверсии аудитора.
6. **Redaction** — секреты в findings/записях редактируются (§6).

---

## 6. Egress как свойство безопасности

Прогон контента через облачную модель = эксфильтрация (иронично для security-тула).
- Бэкенд **декларирует** `cloud: true|false`.
- Конфиг `allow_cloud: false` (дефолт для чувствительных) → облачные бэкенды **отказывают**.
- Решение allow-cloud **per-skill-source**: внутренние скиллы никогда в облако, публичные —
  можно. (Источник = git host / source path.)
- Громкий ворнинг называет ровно **что и куда** уходит.
- **Redaction**: findings могут процитировать встроенный секрет → редактируем перед
  записью в `.csk-audit.json` и перед любой телеметрией. Секретный материал не пишется в
  репо и не уходит в облако в составе findings.
- Стыкуется с существующей local-only network-политикой проекта.

---

## 7. Модель данных

**Finding** (контракт detection↔policy):
```json
{
  "id": "py.network.undeclared-host",
  "surface": "code | prompt | manifest",
  "category": "exfiltration | rce | injection | capability-escalation | opaque | hygiene",
  "severity": "info | low | medium | high | critical",
  "location": {"file": "scripts/x.py", "span": [12, 18]},
  "evidence": "<redacted snippet>",
  "detector": "static:<rule-id> | llm",
  "confidence": "high | medium | low",
  "verifiable": true,
  "capability_violation": {"declared": "network:none", "observed": "api.evil.com"}
}
```

**Verdict record** `.csk-audit.json` (в global cache по content hash, не в репо проекта):
```json
{
  "schema_version": 1,
  "content_sha256": "...", "skill": "...", "source": "...", "commit": "...",
  "backend": "codex", "model": "qwen2.5-coder:32b", "cloud": false,
  "prompt_version": 3, "ruleset_version": 7, "canary_passed": true,
  "static_findings": [...], "llm_findings": [...],
  "decision": "warn", "ran_at": "<ts via args>",
  "trust": {"pinned": false, "pinned_by": null, "reason": null}
}
```

**Capability manifest**: §2 (в csk-skill.json).

---

## 8. Trust как first-class (TOFU + pinning + grants + revocation)

Аудит — это система установления доверия, не просто линтер.
- **TOFU**: первая установка нового source требует явного trust-решения.
- **Pinning**: verdict привязан к content hash. Под strict — требовать revision-pin (теги
  двигаемы); как минимум re-audit при смене хеша.
- **Per-skill grants**: легит-скилл, которому надо читать `~/.aws` (cloud-скилл) или
  запускать git — декларирует это в capabilities; человек ревьюит и пинует грант. Без
  грантов — fatigue. Грант = (skill, capability, content_hash, who, when, reason).
- **Override**: `csk audit --allow <hash> --reason "..."` снимает блок, запись override
  с обоснованием и provenance.
- **Revocation**: чёрный список (skill_source|content_hash) → блок даже при наличии
  старого pass-verdict.
- **Shared verdict cache** по content hash — auditing skill-X@commit один раз обслуживает
  все проекты.

(Опционально, верхний слой полноты, фаза 4: signature verification источника —
sigstore/minisign — и required revision-pin под strict. Это уже supply-chain provenance,
отдельный от content-аудита слой.)

---

## 9. Бэкенд-абстракция (дженерик)

`AuditBackend`: (снапшот + структурированный audit-request) → структурированный verdict
(findings по схеме §7), **не текст**.

- **`command`** (главный generic): csk пишет JSON-request (манифест файлов + содержимое +
  спека аудита + JSON-схема ответа + operational-contract как reference) в stdin
  команды, читает JSON findings из stdout. Любая будущая агентская система = один враппер.
- **`codex`**: `codex exec --model <m>` с провайдером; **локальные Qwen/Gemma через
  Ollama/llama.cpp, egress=0**. Дефолтный local-first путь.
- **`claude-code`**: `claude -p --model <m> --output-format json` (headless). **Egress в
  Anthropic API** → ворнинг + cloud-политика.

codex/claude — встроенные реализации того же `command`-контракта. Точные флаги CLI
сверить с установленными версиями (не выдумывать). Бэкенды — `type: system` деп
(`shutil.which` + actionable error); csk их не ставит.

Аудитору всегда передаётся **operational-contract.md** как reference — чтобы судить
относительно контракта (и контрактную гигиену тоже).

---

## 10. Скоуп анализа (что читаем)

Prompts: `SKILL.md`, `references/*.md`, `.skill_triggers/<locale>.md`,
`locales/metadata.json` (description переписывается во frontmatter — инъекционный канал!).
Code: `scripts/`, всё под `runtime_roots`. Manifest: `csk-skill.json` (+ legacy
`agents/runtime.json`). Opaque: бинари/минифицированное → `opaque` finding.

---

## 11. Политика (слоистая, конфигурируемая)

- **Machine** (`~/.cocoaskills/config.json`) — дефолты.
- **Project** (`Skillfile.json`) — оверрайды для проекта.
- **Per-skill grants** — точечные разрешения возможностей.
Правило разрешения: project ужесточает, но не ослабляет machine ниже floor (например
machine может запретить `allow_cloud`, project не может включить).

`fail_on`: off | low | medium | high | critical. `mode`: advisory | strict.

---

## 12. Поверхность CLI + конфиг

```
csk install --audit            # advisory: прогнать, варнинг, confirm на findings (интерактивно)
csk install --audit=strict     # блок на findings >= fail_on, fail-closed
csk install --no-audit         # явный opt-out если в конфиге on
csk audit [skill|--all] [--json]      # standalone, без установки
csk audit --allow <hash> --reason ... # override-pin
csk audit --revoke <hash|source>      # revocation
--audit-backend codex|claude-code|command   --audit-model <id>
```
```json
"audit": {
  "enabled": false, "mode": "advisory", "fail_on": "high",
  "backend": "codex", "model": "qwen2.5-coder:32b", "allow_cloud": false,
  "backends": {
    "codex":  {"kind": "codex", "provider": "local-ollama", "cloud": false},
    "review": {"kind": "command", "command": ["my-auditor","--json"], "cloud": false},
    "claude": {"kind": "claude-code", "model": "...", "cloud": true}
  },
  "grants": [ {"skill":"skill-review","capability":"exec:git","content_sha256":"...","reason":"...","who":"..."} ],
  "revocations": ["sha256:...", "source:evil/*"]
}
```

CI/non-interactive (твой вопрос «кто подтверждает»): `strict` + детерминированный гейт +
shared cache + grants/pins. Подтверждение = ревью грантов/пинов в коде/конфиге, не live.

---

## 13. Где втыкается в install

`installer._install_project` → после `_build_plans` у `plan` есть `.snapshot`
(материализованный коммит). Аудит **per-plan, после снапшота, до записи** (context copy/
shims/marker). Аудируешь ровно те байты, что встанут. Параллельно по скиллам, timeout/
budget на скилл. Verdict из shared cache если content hash уже аудирован.

---

## 14. Фазы к полной системе

1. **Static-ядро + finding-схема + verdict-record + `csk audit` standalone + `--audit`
   advisory + capability-манифест (парс + declared-vs-observed для static).**
   Детерминирован, egress=0, полезен сразу. Хребет.
2. **Backend-абстракция: `command` (generic) + `codex` (local-first) + LLM-экстрактор
   фактов + canary + кэш + redaction.**
3. **`claude-code` + cloud-политика + strict-гейт + trust (TOFU/pins/grants/revocation).**
4. **Supply-chain слой: signature verification источника, required revision-pin под
   strict, N-of-M для critical.**

Полная система = все 4. Минимально-ценная и безопасная отгрузка = фаза 1 (без LLM,
без облака, детерминирована).

---

## 15. Открытые развилки (нужно решение)

1. **Capability-манифест** (schema v3) — принимаем как спину? Это adoption-cost (каждый
   скилл декларирует envelope) + миграция схемы. Без него остаётся слабый blacklist.
2. **Spec audit-субсистема vs generic pre-install hook framework** — рекомендую субсистему
   (узко, безопасно); raw-hooks сами дыра (arbitrary code on install).
3. **Signature/provenance слой (фаза 4)** — в скоуп «полноценной» или отдельный эпик?
4. **Стартовая отгрузка** — фаза 1 (static-only) как самостоятельный релиз, или ждём
   фазу 2 с LLM?
