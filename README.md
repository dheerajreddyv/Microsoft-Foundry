# Agent Skills on a Foundry Prompt Agent

Runs [Agent Skills](https://learn.microsoft.com/en-us/agent-framework/agents/skills?pivots=programming-language-python)
against a **Foundry Prompt Agent** (`FoundryAgent`) instead of a raw model
deployment (`FoundryChatClient`), which is what the upstream
[`code_defined_skill`](https://github.com/microsoft/agent-framework/tree/main/python/samples/02-agents/skills/code_defined_skill)
sample uses.

## The one constraint that shapes everything

A Prompt Agent can only call tools declared in its **stored, server-side
definition**. The service rejects any request carrying both an
`agent_reference` and `tools`:

```
400 {"code":"invalid_payload","message":"Not allowed when agent is specified.","param":"tools"}
```

So the three skill tools must be published to the agent **once**, before any
run. This is not a per-skill cost: `load_skill`, `read_skill_resource` and
`run_skill_script` are generic dispatchers keyed by `skill_name`, identical
whether you have one skill or fifty. Add, edit or remove skills freely — no new
agent version is needed.

Only the tool *schemas* are published. No skill content is uploaded, and
Foundry never executes anything: every tool call is dispatched and run inside
your Python process, and only the result is posted back.

## Scripts

| Script | Purpose |
| --- | --- |
| `provision_skill_tools.py` | One-time setup: publish the three skill tools to the agent. |
| `code_defined_skill_prompt_agent.py` | Minimal runtime sample. Assumes provisioning is done. |
| `code_defined_skill.py` | Same demo, self-contained: provisions *and* runs. |
| `code_defined_skill_two_skills.py` | Two skills, prompts at runtime, prints which skills were opened. |

All four read the agent from `.env` — nothing is hardcoded. Point them at a
different agent by editing `FOUNDRY_AGENT_NAME` alone; no code change needed.

### `provision_skill_tools.py`

Get-or-create, so it is safe to re-run. Reuses the agent's latest version if it
already declares the three tools, otherwise publishes a new version. Creates
the agent if it does not exist.

```powershell
python provision_skill_tools.py
```

```
Agent 'foundry-skills-agent' version 2 already declares the skill tools.
FOUNDRY_AGENT_VERSION=2
```

Pin the printed version in `.env` so the app never has to look it up.

The schemas come from `SkillsProvider._create_tools()` in agent-framework
itself, not from your skills or this script — which is why it passes an empty
skill list. Taking both the published schemas and the runtime dispatchers from
one source guarantees they cannot drift apart.

### `code_defined_skill_prompt_agent.py`

The recommended runtime shape: connect, attach the provider, ask a question.
Requires `provision_skill_tools.py` to have run first.

```powershell
python code_defined_skill_prompt_agent.py
```

```
Agent: 26.2 miles = 42.1647 kilometers
       75 kilograms = 165.3465 pounds
```

Demonstrates all three ways to define a skill in code:

1. **Static resource** — inline content via the `resources` parameter.
2. **Dynamic resource** — a callable attached with `@skill.resource`.
3. **Script** — a callable attached with `@skill.script`.

### `code_defined_skill.py`

Identical behaviour, but resolves the agent version itself via
`resolve_version()`, publishing the tools if the latest version lacks them.
One file, no setup step — best for reading end to end or running somewhere new.

```powershell
python code_defined_skill.py
```

```
Connected to 'foundry-skills-agent' version 2.
Agent: 26.2 miles = 42.1647 kilometers
       75 kilograms = 165.3465 pounds
```

If `FOUNDRY_AGENT_VERSION` is set it is used as-is and no lookup happens.

### `code_defined_skill_two_skills.py`

Adds a second skill, `test-lazy-skill`, whose body holds two values
(`DELTA-7`, `4892`) that appear nowhere in its description. A correct answer
therefore proves the body was fetched on demand. `TracingSkillsProvider` prints
every skill tool call and reports which skills were left untouched.

Prompts come from the command line or an interactive loop:

```powershell
python code_defined_skill_two_skills.py                     # interactive
python code_defined_skill_two_skills.py "your question"     # one-shot
python code_defined_skill_two_skills.py "q one" "q two"     # batch
```

```
User: What is the secret internal project codename?
  [skill] load_skill(test-lazy-skill)
Agent: DELTA-7
  [skill] untouched: ['unit-converter']

User: How many kilometers is 26.2 miles?
  [skill] load_skill(unit-converter)
  [skill] read_skill_resource(unit-converter -> conversion-tables)
  [skill] read_skill_resource(unit-converter -> conversion-policy)
  [skill] run_skill_script(unit-converter -> convert)
Agent: 26.2 miles = 42.1647 kilometers.
  [skill] untouched: ['test-lazy-skill']
```

Each prompt runs in a fresh session, so every turn starts from L1 with no skill
bodies carried over.

## Progressive disclosure

The four stages, and where each is visible in the output above:

| Stage | Cost | Trigger |
| --- | --- | --- |
| **1. Advertise** | ~100 tokens per skill | Always, in the system prompt |
| **2. Load** | Full `SKILL.md` body | Model calls `load_skill` |
| **3. Read resources** | One resource | Model calls `read_skill_resource` |
| **4. Run scripts** | One script result | Model calls `run_skill_script` |

Stage 1 injects **only** names and descriptions:

```xml
<available_skills>
  <skill>
    <name>test-lazy-skill</name>
    <description>Provides internal reference information about the myISP platform.</description>
  </skill>
  ...
</available_skills>
```

Everything else stays out of context until asked for. The surrounding "When a
task aligns with a skill's domain..." text is a fixed usage preamble — constant
regardless of how many skills you register, and containing no skill content.

### Why a skill takes several round-trips

Each call's input is only knowable from the previous call's output:

```
skill name -> resource names -> factor value -> script call
```

The model cannot read `conversion-tables` before `load_skill` tells it that
name exists, and cannot run `convert` before the table supplies the factor.
Three links, three round-trips. Calls are parallelised *within* a link — the two
independent resources are fetched in one turn — but never across one. That is
the trade: three turns instead of carrying every skill body up front.

## Setup

Requires Python 3.10+ and an existing Foundry project.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
az login
```

Versions in `requirements.txt` are pinned exactly, not floating. agent-framework-core
1.14.0 with agent-framework-foundry 1.11.0 sends the skills preamble as a
request-level `instructions` field, which a Prompt Agent rejects with
`400 invalid_payload - "Not allowed when agent is specified."`. The pinned
versions are the ones these scripts are verified against.

`.env` — the single source of configuration for all four scripts:

```ini
FOUNDRY_PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
# The agent every script connects to.
FOUNDRY_AGENT_NAME=foundry-skills-agent
# Optional: pin an exact version, skipping all lookup. Leave blank to use the
# agent's latest version (or let code_defined_skill.py resolve/publish one).
FOUNDRY_AGENT_VERSION=
# Only used if the agent or a new version has to be created.
FOUNDRY_MODEL_NAME=gpt-5.4
```

Switching agents is an `.env` edit: change `FOUNDRY_AGENT_NAME`, clear
`FOUNDRY_AGENT_VERSION`, and re-run `provision_skill_tools.py` to publish the
skill tools to the new agent.

Then:

```powershell
python provision_skill_tools.py            # once per agent
python code_defined_skill_prompt_agent.py  # run
```

## Notes

* **The stripped-tools warning is expected.** `FoundryAgent` removes
  client-side tool declarations before sending, then warns. The schemas are
  already on the agent, so the warning is noise; the scripts filter out that one
  message and leave every other warning visible.
* **Instructions are not passed client-side.** The stored agent version already
  carries them; passing them again adds a duplicate developer message to every
  request.
* **`function_invocation_kwargs` do not reach skill code** in agent-framework
  1.12.x — `FunctionTool.invoke` only forwards context to tools declaring a
  `FunctionInvocationContext` parameter. Put values the model should honour in a
  resource instead, as `conversion-policy` does with its decimal places.
* **An unprovisioned agent fails silently.** With no tools stored, the model
  makes no tool calls and answers from parametric knowledge — a plausible but
  unverified number, with no error raised. If skills seem ignored, check the
  agent version first.
