# Foundry agent streaming — showing "what the agent is doing"

A minimal, working example of surfacing an agent's **reasoning** and **tool
activity** in real time, before the final answer arrives — the same effect as a
"Thought for 2s" panel in a chat UI.

Built on the Microsoft Agent Framework with a Microsoft Foundry deployment.

Three runnable variants of the same idea:

| Script | Agent lives | Shows |
|---|---|---|
| `stream_agent.py` | In your Python process | Reasoning + tool call + answer |
| `foundry_agent.py` | Stored in the Foundry project | The same, from a server-side agent |
| `workflow_stream.py` | Two agents in a workflow | The same, **per step** |

---

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env    # then edit it with your endpoint + deployment
az login                       # AzureCliCredential reads this

python stream_agent.py     # in-process agent (FoundryChatClient)
python foundry_agent.py    # stored Foundry Agent Service agent (FoundryAgent)
python workflow_stream.py  # multi-agent workflow (WorkflowBuilder)
```

### Files

| File | Purpose |
|---|---|
| `stream_agent.py` | Single agent, defined in Python (`FoundryChatClient`). Start here. |
| `foundry_agent.py` | Single agent, **stored in the Foundry project** (`FoundryAgent`). |
| `workflow_stream.py` | Two-agent **workflow** (`WorkflowBuilder`), streaming every step. |
| `.env.example` | Template for the environment variables. |
| `requirements.txt` | Pinned, verified dependency set. |
| `README.md` | This file. |

All three print the same thing and share the same `Printer` renderer. They are
**self-contained and runnable on their own** — none imports from the others, so
`Printer` is duplicated in each and you can copy any single file out of this
folder and it still works.

Which to read for what:

| Question | Script |
|---|---|
| How do I render reasoning and tool calls at all? | `stream_agent.py` |
| How do I do it against an agent managed in the Foundry portal? | `foundry_agent.py` |
| How do I do it across a multi-step pipeline? | `workflow_stream.py` |

### Configuration

| Variable | Meaning |
|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | Project-scoped endpoint, `https://<resource>.services.ai.azure.com/api/projects/<project>` |
| `FOUNDRY_MODEL` | The **model deployment name**, not the model family |
| `FOUNDRY_AGENT_NAME` | Optional; stored agent name used by `foundry_agent.py` (default `reasoning-demo-agent`) |

Use the **project-scoped** endpoint form. `stream_agent.py` also accepts the
bare account endpoint, but `foundry_agent.py` calls the Agents API, which is
served under `/api/projects/<project>` and returns **404** otherwise.

Use a **reasoning-capable deployment** (o-series / gpt-5 class) if you want
thinking output. Verified here against `gpt-5.4`.

### Dependencies

| Package | Needed by |
|---|---|
| `agent-framework-core` | All three scripts (`Agent`, `Content`, `WorkflowBuilder`) |
| `agent-framework-foundry` | All three (`FoundryChatClient`, `FoundryAgent`) |
| `azure-ai-projects` | `foundry_agent.py` only — publishing the stored agent definition |
| `azure-identity` | All three (`AzureCliCredential`) |
| `python-dotenv` | All three (`.env` loading) |

---

## What you will see

```
Thinking...
**Seeking Amsterdam weather and recommendations**

I need to respond to the user's request about the weather and clothing
suggestions for Amsterdam today. It seems like I should use the weather tool...
  Thought for 3s

[tool] calling get_weather
[tool] {"location":"Amsterdam"} -> It is 18 degrees C, cloudy with light rain in Amsterdam.

Thinking...
**Preparing recommendations for Amsterdam**

I need to suggest what to wear for 18°C with cloudy light rain, considering
layers and comfortable shoes...
  Thought for 4s

[answer] In Amsterdam today, wear **light layers**:
...
```

`Thinking...` opens when summary text first arrives, the text streams in dim
underneath, and `Thought for Ns` closes the block when the stream moves on.

When a turn reasons but returns no readable summary, you get this instead:

```
Thought for 3s (no summary returned)
```

That is **not** a failure. See [Reasoning is non-deterministic](#reasoning-is-non-deterministic).

---

## How it works

### 1. Ask for reasoning summaries

Reasoning output is opt-in. Without this, no summary is ever produced:

```python
default_options={"reasoning": {"effort": "high", "summary": "detailed"}}
```

| Option | Values | Effect |
|---|---|---|
| `effort` | `none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max` | How hard the model thinks |
| `summary` | `auto` / `concise` / `detailed` | How much of that thinking is returned |

Not every deployment accepts every `effort` value — the list above is what the
SDK's `ReasoningEffort` literal allows; the service rejects the ones a given
model does not support. `high` is used throughout this demo.

This is always a **summary**. Raw chain-of-thought is never exposed by the
service — you get a paraphrase suitable for a "thinking…" panel, plus an
encrypted blob the model uses internally across turns.

### 2. Stream and switch on the content type

```python
async for update in agent.run(question, stream=True):
    for content in update.contents:
        match content.type:
            case "text_reasoning": ...   # content.text  -> thinking
            case "function_call":  ...   # content.name / .arguments / .call_id
            case "function_result": ...  # content.result
            case "text":           ...   # content.text  -> the answer
```

Everything except `text` is the agent *working*, not answering.

### 3. The content class

There is exactly **one** content class: `agent_framework.Content`. It is a
unified container, and the variant is identified by its `.type` string field.

```powershell
# List every valid .type value
python -c "from agent_framework._types import ContentType; import typing; print(typing.get_args(ContentType))"
```

```
text                        text_reasoning              data
uri                         error                       function_call
function_result             usage                       hosted_file
hosted_vector_store         code_interpreter_tool_call  code_interpreter_tool_result
image_generation_tool_call  image_generation_tool_result mcp_server_tool_call
mcp_server_tool_result      search_tool_call            search_tool_result
shell_tool_call             shell_tool_result           shell_command_output
function_approval_request   function_approval_response  oauth_consent_request
```

Relevant to a progress UI: `text_reasoning`, `function_call`, `function_result`,
and the `*_tool_call` / `*_tool_result` pairs for hosted tools.

---

## Where reasoning actually comes from

Nothing in these scripts *generates* the thinking — they only display what the
service sends. Three layers:

| Layer | Where |
|---|---|
| **You request it** | `default_options={"reasoning": {...}}`, or `Reasoning(...)` on a stored agent |
| **Framework converts SSE → `Content`** | `agent_framework_openai/_chat_client.py` |
| **You display it** | `case "text_reasoning"` in `Printer.handle` |

The Foundry client subclasses the OpenAI Responses client;
`agent_framework_foundry/_chat_client.py:300` simply delegates to
`super()._parse_chunk_from_openai(...)`. The real mapping lives here:

| Line | OpenAI SSE event | Produces |
|---|---|---|
| **3000** | `response.reasoning_summary_text.delta` | **the streaming thinking text you see** |
| 3011 | `response.reasoning_summary_text.done` | full text; fallback only if no deltas arrived |
| 2974 | `response.reasoning_text.delta` | raw reasoning, for models that expose it |
| 2986 | `response.reasoning_text.done` | same, done event |
| 2731 | reasoning item with no visible text | `text=""` + `protected_data` marker |
| 3214 | encrypted-only item at completion | same empty marker |

The comment above line 2731 states it directly: *"Reasoning item with no visible
text (e.g. encrypted reasoning). Always emit an empty marker so co-occurrence
detection can be done."*

Line numbers are from `agent-framework-core` **1.15.0** and shift between
releases — search for the `case "response.reasoning_summary_text.delta":` label
rather than trusting the number.

Key locations for the types themselves:

| What | Location |
|---|---|
| `Content` class | `agent_framework/_types.py:474` |
| `ContentType` literal | `agent_framework/_types.py:351` |
| `Content.from_text_reasoning()` | `agent_framework/_types.py:627` |
| `AgentResponseUpdate` (the streamed chunk) | `agent_framework/_types.py:2913` |
| Foundry client | `agent_framework_foundry/_chat_client.py` |

---

## Behaviour you must design around

### A tool call means two model turns

```
turn 1:  user prompt     -> [reasoning] -> function_call   -> usage
         (the framework executes your Python function locally)
turn 2:  + tool result   -> [reasoning] -> text answer     -> usage
```

The model cannot answer at turn 1 — it lacks the data. So it reasons about
*needing* the tool, emits the call, and stops. The framework runs the function,
appends the result, and calls the model again. `usage` marks each turn boundary.

**Reasoning therefore legitimately appears before *or* after a tool call.**
Do not model this as a fixed "think → act → answer" pipeline.

### Reasoning is non-deterministic

Observed across repeated runs of the *same* prompt against `gpt-5.4`:

| Run | Turn 1 (before tool) | Turn 2 (after tool) |
|---|---|---|
| 1 | summary text | placeholder only |
| 2 | placeholder only | placeholder only |
| 3 | summary text | none emitted |
| 4 | placeholder only | summary text |
| 5 | placeholder only | summary text |
| 6 | summary text | summary text |

Either turn, both, or neither may produce readable text. The model always
reasons — the encrypted `protected_data` blobs carry it across turns — but the
human-readable summary is generated opportunistically and **cannot be forced**.

Design accordingly: never block your UI waiting for a summary, and always keep
`function_call` / `function_result` as fallback progress signals.

### Other gotchas

- `update.text` aggregates **only** `type == "text"`. Reasoning and tool activity
  never appear there — you must iterate `update.contents`.
- The first `text_reasoning` chunk is typically an **encrypted placeholder**:
  `text == ''` with a `protected_data` blob. Print the header lazily, or you
  render an empty "thinking" block that looks like a bug.
- A tool call spans several chunks sharing one `call_id`: the name repeats and
  `arguments` streams as partial JSON (`{"`, `location`, `":"`, ...). Buffer per
  `call_id`, or you print one line per fragment.
- `text` can arrive *before* a tool call, not only at the end. Do not treat the
  first `text` content as "the answer has started, we're done".
- On Windows, `sys.stdout.reconfigure(encoding="utf-8")` prevents `18°C` from
  rendering as `18�C`.

---

## Two ways to run the same agent

`stream_agent.py` and `foundry_agent.py` produce identical output. The
difference is **where the agent is defined**.

| | `stream_agent.py` (`FoundryChatClient`) | `foundry_agent.py` (`FoundryAgent`) |
|---|---|---|
| Agent definition | In Python, per process | Stored in the Foundry project, versioned |
| Model / instructions | Passed in code | Fields of `PromptAgentDefinition` |
| Reasoning settings | `default_options={"reasoning": {...}}` at call time | `Reasoning(...)` on the stored definition |
| Tool schema | Sent with every request | Must be published in the definition |
| Tool execution | Local Python | Local Python (unchanged) |
| Shared by other apps / portal | No | Yes |

### Why the tool has to be declared twice

`FoundryAgent` cannot send tool declarations with a request — the service reads
them from the stored definition. The framework logs a warning saying so, which
`foundry_agent.py` silences because the schema *is* published:

```python
logging.getLogger("agent_framework.foundry").setLevel(logging.ERROR)
```

So the tool appears in two places, for two different jobs:

```python
@tool
def get_weather(location: Annotated[str, "City name, e.g. 'Amsterdam'"]) -> str: ...

# 1. schema, published to the service so the model knows the tool exists
FunctionTool(
    name=get_weather.name,
    description=get_weather.description,
    parameters=get_weather.parameters(),
    strict=False,
)

# 2. callable, kept client-side so this process can actually run it
FoundryAgent(..., tools=[get_weather])
```

`@tool` returns an `agent_framework.FunctionTool`, and `.parameters()` gives the
JSON schema — so the schema is derived from the Python signature, never
hand-written.

### Reasoning must be set on the definition

For a stored agent, call-time reasoning options are ignored. Configure it where
the definition lives:

```python
PromptAgentDefinition(
    model=os.environ["FOUNDRY_MODEL"],
    instructions=INSTRUCTIONS,
    reasoning=Reasoning(effort="high", summary="detailed"),
    tools=[...],
)
```

`ensure_agent()` publishes this on every run, so the stored agent never drifts
from the code. Republishing an unchanged definition reuses the same version
number rather than piling up new ones.

Everything downstream is identical: `agent.run(..., stream=True)` yields the
same `AgentResponseUpdate` objects with the same `Content.type` values, which is
why both scripts use the same `Printer` code unchanged.

---

## Doing this in a workflow

`workflow_stream.py` runs a two-agent chain — **researcher** (calls the weather
tool) → `handoff` → **stylist** (writes the advice) — and streams both agent
steps. The small `handoff` executor in the middle is explained
[below](#chained-agents-need-a-handoff-step).

```
=== researcher ===

Thinking...
**Checking Amsterdam weather**
I need to respond to the user about what to wear in Amsterdam today ...
  Thought for 4s

[tool] calling get_weather
[tool] {"location":"Amsterdam"} -> It is 18 degrees C, cloudy with light rain in Amsterdam.
[answer] Amsterdam: 18°C, cloudy with light rain.

=== stylist ===

Thinking...
**Creating weather outfit recommendations**
I'm thinking about crafting a practical outfit recommendation ...
  Thought for 11s

[answer] For 18°C, cloudy with light rain in Amsterdam, I'd recommend: ...
```

### The rendering does not change

An `AgentExecutor` running in streaming mode forwards **every**
`AgentResponseUpdate` it receives into the workflow event stream
(`_workflows/_agent_executor.py:508`, `await ctx.yield_output(update)`). So the
payload is exactly what a bare agent yields, and `Printer` works untouched.

The workflow only adds an **envelope**: `WorkflowEvent`. Unwrap it and you are
back in the same loop:

```python
async for event in workflow.run(prompt, stream=True):
    if isinstance(event.data, AgentResponseUpdate):
        printer.handle(event.data)
```

`event.executor_id` tells you which step produced the update — that is the extra
signal a workflow gives you, and what drives the `=== researcher ===` headers.

### `WorkflowEvent` is a unified class too

Like `Content`, events are one generic class with a `.type` discriminator, not a
class per event. There is no `AgentRunUpdateEvent` in this version:

```python
from agent_framework import WorkflowEventType
print(list(WorkflowEventType))
```

```
'started', 'status', 'failed', 'output', 'intermediate', 'data', 'request_info',
'warning', 'error', 'superstep_started', 'superstep_completed',
'executor_invoked', 'executor_completed', 'executor_failed',
'executor_bypassed', 'group_chat', 'handoff_sent', 'magentic_orchestrator'
```

Agent streaming updates arrive as **`intermediate`** (non-final executors) and
**`output`** (final executors). The lifecycle types are for progress UI —
`executor_invoked` / `executor_completed` are the natural hook for a step
indicator, and `superstep_started` / `superstep_completed` mark each round.

Observed event mix for one run of this workflow:

| type | executor_id | data | count |
|---|---|---|---|
| `started` / `status` / `superstep_*` | – | `None` | 11 |
| `executor_invoked` | `researcher` | `str` | 1 |
| `intermediate` | `researcher` | `AgentResponseUpdate` | 119 |
| `executor_completed` | `researcher` | `list` | 1 |
| `executor_invoked` | `handoff` | `AgentExecutorResponse` | 1 |
| `executor_completed` | `handoff` | `list` | 1 |
| `executor_invoked` | `stylist` | `AgentExecutorRequest` | 1 |
| `output` | `stylist` | `AgentResponseUpdate` | 290 |
| `executor_completed` | `stylist` | `list` | 1 |

Chunk counts vary run to run; the shape does not.

### The one setting that matters

By default only the final executor is streamed out. Non-final steps must be
opted in, or the researcher's thinking and tool call are silently swallowed:

```python
WorkflowBuilder(
    start_executor=researcher,
    output_from=[stylist],                  # final answer
    intermediate_output_from=[researcher],  # <- without this, step 1 is invisible
).add_edge(researcher, handoff).add_edge(handoff, stylist).build()
```

`intermediate_output_from` also accepts `"all"` or `"all_other"`.

### Chained agents need a handoff step

Connecting two agents directly makes the second one behave badly: it produces no
thinking and often just repeats the first agent's answer.

The cause is in `AgentExecutorResponse.full_conversation` — chaining forwards the
whole conversation, so the second agent's **last message is an assistant message
that already looks like an answer**. There is nothing left to solve, so the model
continues or restates it instead of doing its own work, and reasons about nothing.

Restate the previous result as a *user* message with an explicit task:

```python
@executor(id="handoff")
async def handoff(result: AgentExecutorResponse, ctx: WorkflowContext[AgentExecutorRequest]) -> None:
    await ctx.send_message(
        AgentExecutorRequest(
            messages=[Message("user", f"Weather report: {result.agent_response.text}\n"
                                      "What should I wear? Explain your choices.")]
        )
    )

WorkflowBuilder(...).add_edge(researcher, handoff).add_edge(handoff, stylist).build()
```

With the handoff in place both agents produce a `Thinking...` block. Asking for
*why* ("Explain your choices") also gives the model something worth reasoning
about — a terse "be brief" instruction usually yields no summary at all.

### Renderer state is per step

`Printer` tracks thinking timers and buffered tool arguments, so it is reset
when `executor_id` changes — otherwise one step's open "thinking" block would
swallow the next step's output:

```python
if event.executor_id != current:
    if current is not None:
        printer.finish()
        printer = Printer()
    current = event.executor_id
```

---

## Version compatibility

`agent-framework-core` and `agent-framework-foundry` are versioned
**independently** — do not assume they share a number.

Verified working, unchanged, on both:

| core | foundry | Status |
|---|---|---|
| 1.15.0 | 1.11.0 | Verified against a live deployment |
| 1.8.0 | 1.8.0 | Verified against a live deployment |

The unified `Content` / `.type` API, `Agent`, `AgentResponseUpdate`, and
`Agent.run(stream=True)` all exist in both releases, so `stream_agent.py` needs
no changes between them.

`foundry_agent.py` and `workflow_stream.py` were verified on **core 1.15.0 /
foundry 1.11.0** only. `foundry_agent.py` additionally needs
`azure-ai-projects>=2.3.0` for `PromptAgentDefinition` and `Reasoning`.

Two caveats:

- **On 1.8.0 you must `pip install aiohttp` yourself.** That release does not
  declare it, and `azure-ai-projects` fails to import without it:
  `ModuleNotFoundError: No module named 'aiohttp'`.
- Separate `TextContent` / `TextReasoningContent` / `FunctionCallContent`
  classes belong to releases **older than 1.8.0**. On 1.8.0+ they do not exist
  and importing them raises `ImportError`.

---

## Troubleshooting

### All scripts

| Symptom | Cause and fix |
|---|---|
| `Thought for Ns (no summary returned)` | Normal. The model reasoned but returned no summary this turn. Not a bug. |
| No thinking output at all, ever | Deployment is not a reasoning model, or `default_options={"reasoning": ...}` is missing. |
| Deployment rejects the `reasoning` option | Remove `default_options`; tool-call progress still works. |
| `ModuleNotFoundError: No module named 'aiohttp'` | On foundry 1.8.0. `pip install aiohttp`. |
| `Could not find a version ... agent-framework-foundry==1.15.0` | The two packages version independently; foundry's latest is 1.11.0. |
| `ImportError: cannot import name 'TextContent'` | Pre-1.8.0 API. Use `Content` with `.type` instead. |
| `18�C` in output | Console encoding. Already handled by `sys.stdout.reconfigure(encoding="utf-8")`. |
| One line per `args +=` fragment | Arguments stream as partial JSON. Buffer per `call_id`. |

### `foundry_agent.py`

| Symptom | Cause and fix |
|---|---|
| `ResourceNotFoundError: (404) Resource not found` | `FOUNDRY_PROJECT_ENDPOINT` is the account root. The Agents API needs the project form, `.../api/projects/<project>`. |
| `Foundry agent '...' was provided tools, but tool declarations cannot be sent` | Expected. Publish the schema in `PromptAgentDefinition.tools`; the warning is silenced in the script. |
| Agent runs but never calls the tool | The schema is missing from the stored definition. Client-side `tools=[...]` alone is not enough. |
| No reasoning at all | `Reasoning(...)` missing from the stored definition. Call-time `default_options` is ignored for a stored agent. |
| Agent version keeps incrementing | Only happens when the definition actually changes; republishing an identical one reuses the version. |

### `workflow_stream.py`

| Symptom | Cause and fix |
|---|---|
| Only the last agent's output appears | Non-final executors are not streamed by default. Set `intermediate_output_from=[...]`. |
| Second agent echoes the first and never reasons | Chaining forwards `full_conversation`, so its last message is an assistant answer. Insert the `handoff` executor. |
| Thinking from step 2 attaches to step 1's block | `Printer` state is per step. Reset it when `event.executor_id` changes. |
| Nothing matches your `isinstance` check | Only `intermediate` and `output` events carry `AgentResponseUpdate`; lifecycle events carry `None`, `str` or `list`. |

---

## Adapting this to a real UI

`Printer` writes to a terminal, but the logic maps directly to a web UI:

| Content | UI treatment |
|---|---|
| `text_reasoning` (non-empty) | Append to a collapsible "Thinking…" panel |
| `text_reasoning` (empty) | Start a timer; proves reasoning is happening |
| `function_call` | Status chip: "Calling `get_weather`…" |
| `function_result` | Mark the chip complete |
| `text` | Stream into the message bubble |
| `usage` | Turn boundary; token counts incl. `reasoning_output_token_count` |

In a workflow you get one extra layer, from the `WorkflowEvent` envelope:

| Event | UI treatment |
|---|---|
| `executor_invoked` | Mark that step active in a stepper / progress rail |
| `intermediate` + `output` | Unwrap `event.data` and render exactly as above |
| `event.executor_id` | Which step the update belongs to — key your panels on it |
| `executor_completed` | Mark the step done |
| `executor_failed` | Show the step's error without killing the whole view |

Keep the per-`call_id` argument buffer and the lazy header, and treat ordering as
free-form — those three details are what separate a clean panel from a flickering
one.
