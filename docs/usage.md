# Consilium — How to use it

---

## Getting it running

```sh
./start.sh              # backend :8000 + frontend :5173
./start.sh backend      # backend only
./start.sh frontend     # frontend only
./start.sh check        # health check, roster count, config posture
```

First run creates the venv, installs dependencies, and bootstraps `.env` from
`.env.example`. Open **http://localhost:5173**.

### Point it at a model

Two ways, and the first is easier.

**In the app.** Click the provider chip in the header → pick a provider → paste a
key → **Test connection**. That last step makes a real call, because "a key is
present" and "the key works" are different facts and only one of them matters.
Keys entered here live in the server process for that run.

**In `.env`,** to persist across restarts:

```sh
LLM_PROVIDER=anthropic        # or openai | google | groq | ollama
ANTHROPIC_API_KEY=sk-ant-...
```

Leave `LLM_PROVIDER` unset and Consilium picks the first provider that has a
working credential. For a local, free, no-key setup: install
[Ollama](https://ollama.com), `ollama pull llama3`, and pick **Ollama (local)**.

### Choose which packs you serve

```sh
CONSILIUM_PACKS=core,healthcare     # the clinical ladder + healthcare guardrails
```

The **last** pack listed owns the phase ladder and the guardrail policy. This is
only the default — a session can declare its own packs, and the pack chips in the
header switch them live.

### See what the models actually said (optional)

```sh
./scripts/setup-langfuse.sh
```

One-time: clones and starts a local, self-hosted [Langfuse](https://langfuse.com)
instance (Docker) and wires its keys into `backend/.env`. From then on every
advisor turn, chair synthesis, and report is traced — the real prompt, the
completion, per-node cost and token usage — at **http://localhost:3000**.
Entirely optional: skip it and the app runs exactly the same, just untraced.
Re-running the script is safe — it won't re-provision or duplicate config.

To stop it later: `cd ../langfuse && docker compose stop` (or the ■ button in
Docker Desktop). Data persists either way; only `docker compose down -v`
deletes it.

---

## The Console

Three panes: **Directory** (left), a tabbed middle panel (**Room** / **Seated** /
**Work Log**), **Inspector** (right).

The middle panel used to be one column split between the room and the
transcript, and the split was never quite big enough for either — a room with
several seats could squeeze itself half out of view under its own brief box.
Tabs fixed that: whichever one you're on gets the *whole* panel. You don't
have to think about switching most of the time — summoning a room jumps you to
**Seated**, and anything that produces a turn (convening, sending a message,
assigning, synthesizing) jumps you to **Work Log**.

### Summon a room

On the **Room** tab, type the decision into the brief box and hit **Summon a
room**.

> *Should we build retrospective risk-adjustment chart chase in-house or buy it?*

You land on **Seated**, and — this is the part worth reading — **why these
seats**:

```
risk_adjustment   matched "risk-adjust", "chart chase"
```

The readout at the top of the Room tab tells you three things before you spend
anything:

| | |
| :--- | :--- |
| **Tension** | how many seated pairs are on record disagreeing. Zero is a warning. |
| **Est. cost** | per-seat attribution, priced for the provider you are on |
| **Seated by** | `rules` (keyword match), `model` (no rule matched, an LLM picked), or `fallback` |

**Cap** limits room size. Smaller rooms are sharper and cheaper.

### Build a room by hand

Click seats in the Directory. Search by name, role, or tag; filter by tag chips.
Switch to the **Seated** tab any time to see the whole room at once, plus live
tensions and the routing rationale — the tension count updates as you add and
remove people, so an all-agreeing room tells you so before the debate rather
than after.

### Convene the board

One button, three states, sitting on the right of the tab bar — separated from
Room/Seated/Work Log so it reads as the panel's action, not a fourth tab. It
stays there no matter which tab you're on, so you're never hunting for it mid-debate.

| Button reads | When | What happens |
| :--- | :--- | :--- |
| **Convene the board** | no round has run yet | Starts the round and returns immediately — it does not block while the room debates. |
| **Stop the debate** | a round is running | Asks the round to stop. Not instant: a live model call can't be cut off mid-sentence, so it ends after whoever is currently speaking finishes. Skips the chair's closing synthesis — you asked to stop, so nothing spends one more call summarizing what was said. |
| **Continue the discussion** | a round already finished | Starts a **new** round on the same session. This is not resuming the old one — a finished round is done, there's no picking it back up mid-flight — it's a fresh round that continues the conversation. |

While convening, each seated specialist speaks in turn, each seeing what the
previous ones said. The **Work Log** tab shows a live status strip — who is
speaking and which turn it is (`3/5`) — updating roughly every 1.5 seconds as
the debate actually happens; you're watching it happen, not waiting for one
response at the end. That strip has its own small Stop button too, for when
you're already looking at the transcript.

While a round is running, the brief, cap, and room composition are locked on
the Room tab; **Clear** and the Work Log composer wait for it to finish or be
stopped.

Reload the page mid-debate and it picks the round back up — the browser only
remembers which session was open, and the session itself is asked whether a
round is still going.

**Ask the chair to synthesize**, on the Work Log tab, re-runs the briefing over
the whole transcript once the room is between rounds.

### Talk to one person

Hover a seat → **Talk one-on-one** in the Inspector. A private session with just
that person, manual turn mode — nobody else speaks unless you ask them to. This
jumps you straight to **Work Log**; the Convene/Stop/Continue button hides
itself during a one-on-one, since those sessions don't use the round machinery
it drives.

Inside any session, the composer at the bottom of the work log has a target
selector: **The room** runs a round; naming a seat puts the question to that
person alone.

### Assign a task

Hover a seat → **Assign a task**. Describe the work and set a priority.

This is not the same as asking a question. The brief tells the seat to *deliver
the work itself, not a plan to do it later* — an advisor prompted like a boardroom
turn answers with an opinion. The task appears in the work log's task table with
its owner, and turns from **outstanding** to **delivered** when the answer lands.

### Read a dossier

Hover any seat. The Inspector shows its role, tone, tags, pack, and — most useful
— **who it argues with** and **who challenges it**, in both directions. Seats
already in your room are highlighted, which answers *"will adding this person
change the debate, or just agree with everyone?"*

---

## The Floor

Switch with **Floor** in the header. The whole org from above: Core Boardroom in
the middle because every domain pack inherits from it, Health and Pharma as
wings, Life Sciences below.

### Getting around

| | |
| :--- | :--- |
| Scroll / pinch | zoom, anchored where the pointer is |
| Drag | pan |
| `+` `−` `0` | zoom in, out, fit everything |
| Arrow keys | pan |
| `−` `+` **Fit** buttons | bottom right, with the zoom percentage |

It opens at a readable zoom rather than fitting all 44 seats, because fitting
everything on a laptop makes the names too small to read. **Fit** gives the
overview when you want it. Zoom far enough out and the cards drop to status
markers — the same reason a map hides street names.

### What you are looking at

Each desk shows a name and what that person is doing:

| | |
| :--- | :--- |
| **idle** | nothing assigned |
| **queued** | order received, not started |
| **busy** | working — the desk pulses |
| **delivered** | finished, with how long it took |
| **failed** | something broke; hover for the reason |

Dashed amber lines join seats with declared disagreements.

The pulse comes from a real job record, not from "we sent a request a moment
ago". If a desk is pulsing, that seat is genuinely mid-generation.

### Giving orders

Click any desk to assign a task. On the floor, orders are **asynchronous** — the
order lands instantly and the desk starts working. Assign to several people at
once and watch them finish at different times.

Hovering a desk gives **Talk** and **Assign** without leaving the floor.

---

## History — finding old work

Switch with **History** in the header. Two tabs, because there are two different
questions.

### Sessions

Every conversation you have had, most recently touched first, with its seat
count, task progress, and pack dots. Click one to read the whole transcript and
the tasks it produced. **Resume** puts it back in the Console with its composer,
so you can pick the thread up rather than starting over.

### Tasks

Every task ever assigned, across all sessions — because *"what did I ask for, and
did I get it?"* is a question that spans sessions and cannot be answered from
inside one. Filter by **outstanding** or **delivered**; clicking a task opens the
session it came from.

A task counts as **delivered** only when there is a message fulfilling it, never
from a separate status field. A flag can disagree with reality; a link to the
actual answer cannot.

### What survives a refresh

Whichever session was open reopens automatically. Only its id is stored locally —
the content is refetched from the server, so you never read a stale copy of a
conversation that has since moved on. If a session was deleted elsewhere, the app
quietly forgets it rather than showing you an error for something you did.

The Floor also survives: it reads live job records, so every desk shows that
seat's last known state after a reload.

---

## Reading the output honestly

**Cost is an estimate.** It is marked `est.` because it is modelled from token
assumptions, not billed usage. Treat it as an order of magnitude.

**A degraded turn says so.** If no model answered, you get *"No model answered
this turn"* with the reason — never a plausible-looking briefing that isn't one.
If you see that, check Settings.

**Zero tension is a real warning.** *"No seat in this room is on record as
disagreeing with another. The debate will confirm what you already think."* Add
someone who will push back.

**A model-picked room is labelled.** When `Seated by: model` appears, no keyword
rule matched and an LLM chose from the roster. It is still constrained to real
seats and still gets the tension check — but the room is less predictable than a
rule-matched one, and you should read the roster before trusting it.

---

## What it will not do

Consilium advises healthcare **businesses**. It does not practise medicine.

Ask it about an individual patient and it declines and redirects. It will not
produce diagnosis, treatment recommendations, or triage; it is not a medical
device; it is not designed to receive PHI. Coding guidance is educational and
flags upcoding risk rather than optimising around it.

The **Advisory only** badge next to the seat count comes from the same policy
object that constrains every advisor prompt, so what you are told and what the
models are told cannot drift apart. It's compact by design — hover it (or read
it via a screen reader) for the full wording — because the actual guardrail is
enforced server-side in every prompt regardless of whether this text is on
screen; the badge is the disclosure of that boundary, not the boundary itself.
A second badge appears next to it if any pack's ladder or guardrail policy
fails to resolve, which is a configuration bug worth fixing rather than
something to route around.

---

## Troubleshooting

| Symptom | Cause |
| :--- | :--- |
| *"Could not reach the backend"* | Backend not running. `./start.sh backend` |
| Every turn says "No model answered" | No working credential. Settings → Test connection |
| **Test connection** fails with 401 | The key is present but wrong. This is exactly what that button is for |
| *"No rule matched this brief"* | Nothing matched **and** no model was reachable. Configure a provider, or add seats by hand |
| A desk is stuck **busy** after a restart | Should not happen — jobs *and* rounds are reaped at boot as `ServerRestart`. If it does, that is a bug |
| **Stop** doesn't end the round instantly | Expected. It ends after the current speaker's turn, not mid-generation |
| A round is stuck **running** with no progress | The backend restarted mid-round without reaping it, or its background thread died silently — check `logs/backend/` for the round's id |
| Ollama shows "not reachable" | Ollama is not running. `ollama serve` |
| No traces in Langfuse | Either it's not set up (`./scripts/setup-langfuse.sh`) or its containers aren't running (`cd ../langfuse && docker compose up -d`) — the app runs fine either way, tracing just goes silent |

Logs: `logs/backend/` and `logs/frontend/`.

---

## Extending it

Everything that changes often is data.

**Add a persona** — drop `backend/app/personas/<pack>/<id>.md` with `Name:`,
`Role:`, `Tone:` in the first six lines, and add a manifest entry with `tier`,
`tags`, and `conflicts_with`. A seat needs a distinct failure mode, real number
sense, and a declared conflict; without all three it belongs in someone else's
prompt. Update `DOMAIN_PACKS` in `tests/test_persona_packs.py`.

**Add a routing rule** — an entry in `backend/app/routing_rules.json`, plus a
brief in `ROUTING_CASES` in `tests/test_org_api.py` proving it fires. The suite
fails if a rule has no such brief. Watch your regex: alternatives written outside
a capture group are the classic silent-miss.

**Add a model** — an entry in `backend/app/providers.json` with its price and
tier. Prices drive the console's cost readout, so a wrong number here is a wrong
number shown to a user with a dollar sign on it.

**Add a pack** — a directory with a `pack.json` declaring `phase_ladder`,
`guardrails`, and `inherits`. Both referenced ids must exist or the pack loads
`degraded`, and a test enforces that every pack on disk is covered.

---

Architecture: [`architecture.md`](architecture.md)
