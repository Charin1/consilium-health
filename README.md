# Consilium

> The AI advisory board for healthcare, pharma, and life sciences.

Consilium is a multi-agent boardroom. You describe a decision, summon the specialists who
should be in the room, and they debate it — in front of you, disagreeing with each other.

The premise is that **the debate engine is a commodity and the roster is the moat.** A generic
C-suite advisor says *"consider compliance risk."* A Consilium advisor says *"your RAF lift
assumes 90% chart retrieval and the industry gets 60% — rebuild the model."*

---

## The roster — 44 seats across four packs

| Pack | Own seats | Covers |
| :--- | ---: | :--- |
| **Core Boardroom** | 14 | Domain-neutral C-suite — CEO, CFO, COO, CTO, GC, and the rest |
| **Consilium Health** | 15 | Providers & payers — RAF/HCC, HEDIS/Stars, EHR integration, revenue cycle, HIPAA |
| **Consilium Pharma** | 9 | Drug development — trial design, regulatory, pharmacovigilance, market access, CMC |
| **Consilium Life Sciences** | 6 | Discovery & translational — target validation, biomarkers, omics, IP strategy |

Each domain pack inherits `moderator`, `ceo`, `finance`, `ops` from core rather than
restating them.

**Packs are views, not partitions.** A session can seat a CFO next to a risk adjustment
specialist next to a market access lead — that combination is the point, and the roster
carries 43 declared cross-pack disagreements to make those rooms productive.

---

## Why the seats argue

Every persona declares three things, and a seat that cannot supply all three gets folded into
another rather than added:

1. **A distinct failure mode it alone watches for.**
2. **Number sense** — real benchmarks, so it can challenge an unrealistic assumption rather
   than gesture at one.
3. **A declared conflict** with at least one other seat.

That third one is structural. `conflicts_with` lives in the pack manifest as data, so the
system can tell you *"this room has three live tensions"* — or warn you that everyone in it
agrees and the debate will only confirm what you already think.

---

## Running it

```sh
# Backend
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # add your model API key
PYTHONPATH=. .venv/bin/python -m uvicorn main:app --reload

# Frontend
cd frontend && npm install && npm run dev
```

Tests:

```sh
cd backend && PYTHONPATH=. .venv/bin/python -m pytest tests/ -q
# 268 passed in under two seconds
```

---

## Guardrails

Consilium advises **healthcare businesses**. It does not practise medicine.

- **No clinical decision support for individual patients.** Personas advise on strategy,
  operations, and program design. An individual patient case gets declined and redirected.
- **Not a medical device.** Nothing in the output may read as diagnosis, treatment
  recommendation, or triage.
- **No PHI.** The product is not designed to receive it.
- **Coding guidance is educational.** Never codes for submission on a real claim, and
  upcoding risk gets flagged rather than optimised around.

These are declared per-pack in the manifests (`guardrails: healthcare_v1`) and injected into
**every advisor prompt**, not just the Chair's — an advisor without the boundary can breach it
in its own turn, and the Chair only sees it afterwards. The user-facing disclaimer is served
from the same policy so the header, the export, and the prompt cannot drift apart.

---

## Three surfaces

**Console** — one screen, a directory, a tabbed middle panel (**Room** / **Seated** / **Work
Log**), and an inspector. The directory lists all 44 seats by tier; Room and Seated cover
building and reasoning about the room; Work Log is the transcript. Each tab gets the panel's
full height — they used to share a fixed vertical split, which meant a room with several seats
could get squeezed half out of view under its own brief box.

You get a room two ways, and they land in the same place: write a brief and let the
deterministic router seat one, or click seats yourself. Either way the tension count updates
live — a room with zero declared disagreements says so before you pay for the debate.

Convening doesn't block: **Convene the board** returns immediately, each seat's turn streams
into Work Log as it's actually generated (roughly every 1.5s), and **Stop** ends the round after
whoever is currently speaking finishes — a live model call can't be cut off mid-sentence. Once a
round is done, the same button reads **Continue the discussion**, which starts a fresh round on
the same session rather than resuming the finished one.

```
GET  /api/org/packs                          packs, ladders, guardrail policies, seat counts
GET  /api/org/seats?packs=…                  the directory — never returns system prompts
GET  /api/org/seats/{id}                     one dossier, conflicts in both directions
POST /api/org/summon                         deterministic router → room + the rules that fired
POST /api/org/tensions                       declared disagreements for a hand-picked room
POST /api/chat/sessions/{id}/convene-async   202 + a round to watch; streams turns live
POST /api/rounds/{id}/stop                   stop after the current speaker finishes
```

**Floor** — the whole org from above. Desks are zoned by pack, and each one reports what that
person is doing *right now*: idle, queued, working, delivered, failed. Give an order and it
lands instantly; the desk starts working and says so. Dashed lines join seats with declared
disagreements.

The floor is only honest because the work behind it is genuinely async — a job row is written
before any model call and its progress is persisted as it happens. A status bubble that isn't
reading real state is a decoration that lies.

```
POST /api/chat/sessions/{id}/assign-async   202 + a job to watch
GET  /api/jobs?active_only=true             what every seat is doing
```

Motion carries state and nothing else: a desk pulses because that seat has a running job, not
because movement looks alive. Under `prefers-reduced-motion` the pulse becomes a static ring.

**History** — every past session and every task ever assigned, across all of them. "What did I
ask for, and did I get it?" spans sessions, so it can't be answered from inside one; a task
counts as delivered only when a message actually fulfils it, never from a separate status flag.

---

## Models

Anthropic, OpenAI, Gemini, Groq, or a local Ollama — switchable from Settings, with a **Test
connection** button that makes a real call. "A key is present" is not the same as "the key
works", and only one of those is worth telling you.

Which model each seat uses follows its tier: the chair and executives synthesize across the
whole table and get the frontier model; specialists argue from one discipline and get the
balanced one. Prices live in `backend/app/providers.json` and drive the console's cost
readout, so the quote follows the provider you are actually on.

---

## Observability

Every advisor turn, chair synthesis, mission strategy, and report summary is traced through
[Langfuse](https://langfuse.com) — the actual prompt, the completion, per-node cost and token
usage, and how a run degraded if it did. One call site (`UnifiedLLMClient.generate_detailed` in
`backend/app/services/llm_client.py`) instruments every provider, so nothing depends on each
call site remembering to trace itself.

```sh
./scripts/setup-langfuse.sh   # one-time: clones, starts, auto-provisions keys into backend/.env
```

Self-hosted only — the client refuses to trace against Langfuse's cloud endpoint without an
explicit opt-in, since a boardroom session carries clinical/payer-specific content. Prompts and
completions are masked (emails, SSNs, phone numbers, MRNs) before they leave the process, via a
`mask` callable this repo defines and hands to the Langfuse SDK, not something Langfuse provides
itself. Tracing is entirely best-effort: unreachable or misconfigured, the app runs identically
with it off.

---

## Where things are

| | |
| :--- | :--- |
| What it is and why it's shaped this way | [`docs/architecture.md`](docs/architecture.md) |
| How to use it | [`docs/usage.md`](docs/usage.md) |
| Contracts & scar tissue | `.agents/blueprint.md` * |
| Roster design & routing | `docs/planning/org-roster.md` * |
| Original vertical spec | `docs/planning/consilium-health.prompt.md` * |
| Build guidance | `.agents/skills/engineering/` * |
| Personas | `backend/app/personas/<pack>/` |
| Routing rules | `backend/app/routing_rules.json` |

\* local only — `.agents/` and `docs/planning/` are gitignored, not in the pushed repo
