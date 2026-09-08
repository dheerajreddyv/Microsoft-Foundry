# Foundry agent streaming — showing "what the agent is doing"

A minimal, working example of surfacing an agent's **reasoning** and **tool
activity** in real time, before the final answer arrives — the same effect as a
"Thought for 2s" panel in a chat UI.

Built on the Microsoft Agent Framework with a Microsoft Foundry deployment.

---

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env    # then edit it with your endpoint + deployment
az login                       # AzureCliCredential reads this

python stream_agent.py
```

### Files

| File | Purpose |
|---|---|
| `stream_agent.py` | The entire demo — agent setup, streaming loop, renderer. |
| `.env.example` | Template for the two required environment variables. |
| `requirements.txt` | Pinned, verified dependency set. |
| `README.md` | This file. |

### Configuration

`.env` supplies two variables, both read natively by `FoundryChatClient`:

| Variable | Meaning |
|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | Project endpoint, e.g. `https://<project>.services.ai.azure.com` |
| `FOUNDRY_MODEL` | The **model deployment name**, not the model family |

Use a **reasoning-capable deployment** (o-series / gpt-5 class) if you want
thinking output. Verified here against `gpt-5.4`.

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
| `effort` | `low` / `medium` / `high` | How hard the model thinks |
| `summary` | `auto` / `concise` / `detailed` | How much of that thinking is returned |

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

Nothing in `stream_agent.py` *generates* the thinking — it only displays what the
service sends. Three layers:

| Layer | Where |
|---|---|
| **You request it** | `default_options={"reasoning": {...}}` in `stream_agent.py` |
| **Framework converts SSE → `Content`** | `agent_framework_openai/_chat_client.py` |
| **You display it** | `case "text_reasoning"` in `Printer.handle` |

The Foundry client subclasses the OpenAI Responses client;
`agent_framework_foundry/_chat_client.py:300` simply delegates to
`super()._parse_chunk_from_openai(...)`. The real mapping lives here:

| Line | OpenAI SSE event | Produces |
|---|---|---|
| **2953** | `response.reasoning_summary_text.delta` | **the streaming thinking text you see** |
| 2964 | `response.reasoning_summary_text.done` | full text; fallback only if no deltas arrived |
| 2927 | `response.reasoning_text.delta` | raw reasoning, for models that expose it |
| 2939 | `response.reasoning_text.done` | same, done event |
| 3167 | reasoning item with no visible text | `text=""` + `protected_data` marker |
| 3336 | encrypted-only item at completion | same empty marker |

The comment at lines 3164–3165 of that file states it directly: *"Reasoning item
with no visible text (e.g. encrypted reasoning). Always emit an empty marker so
co-occurrence detection can occur."*

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

## Version compatibility

`agent-framework-core` and `agent-framework-foundry` are versioned
**independently** — do not assume they share a number.

Verified working, unchanged, on both:

| core | foundry | Status |
|---|---|---|
| 1.15.0 | 1.11.0 | Verified against a live deployment |
| 1.8.0 | 1.8.0 | Verified against a live deployment |

The unified `Content` / `.type` API, `Agent`, `AgentResponseUpdate`, and
`Agent.run(stream=True)` all exist in both releases, so this script needs no
changes between them.

Two caveats:

- **On 1.8.0 you must `pip install aiohttp` yourself.** That release does not
  declare it, and `azure-ai-projects` fails to import without it:
  `ModuleNotFoundError: No module named 'aiohttp'`.
- Separate `TextContent` / `TextReasoningContent` / `FunctionCallContent`
  classes belong to releases **older than 1.8.0**. On 1.8.0+ they do not exist
  and importing them raises `ImportError`.

---

## Troubleshooting

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

Keep the per-`call_id` argument buffer and the lazy header, and treat ordering as
free-form — those three details are what separate a clean panel from a flickering
one.
