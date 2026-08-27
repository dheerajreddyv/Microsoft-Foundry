# Per-user filtered retrieval with Foundry IQ

`foundry_iq_blob_demo.py` answers questions from a blob-backed knowledge base while
guaranteeing that each signed-in user can only ever retrieve **their own** documents.

Documents live in blob storage at `hier/<user_id>/<mms_id>/<msg_id>/<file>`. The indexer
lifts those path segments into filterable fields, and every retrieval carries an OData
filter pinned to the signed-in user.

## Why a function tool, and not the knowledge base MCP endpoint

A knowledge base exposes an MCP endpoint, and it is tempting to hand that straight to an
agent. It cannot do per-user filtering:

- Its only tool, `knowledge_base_retrieve`, takes `{"queries": [...]}` and sets
  `additionalProperties: false` — there is no filter argument to supply.
- The agent authenticates with its own identity, not the end user's, so the service has no
  idea who is asking.

The `retrieve` REST action *does* accept `filterAddOn`. Wrapping it in a **function tool**
means the tool call round-trips back to this process — which is the one place that knows
who signed in. The filter is therefore applied by code you control.

## The trust boundary

```
tool schema seen by the model :  query, mms_id?, msg_id?
injected here, never by model :  user_id eq '<signed-in UPN>'
```

`user_id` is deliberately **not** a tool parameter, so the model cannot set it, widen it,
or be talked into changing it. `mms_id` and `msg_id` may be suggested by the model, but
are accepted only if they appear in that user's own documents — an invented or injected
value is rejected rather than pasted into the filter. Both can only ever narrow the
result set; the `user_id` clause is always present.

Single quotes are escaped, so a value like `x' or user_id ne '` cannot terminate its OData
string literal and break out of the filter.

## Resources

The script uses these and **creates none of them**. `teardown` deletes only the agent.

| Resource | Name |
| --- | --- |
| Index | `ks-azureblob-483-index` |
| Knowledge source | `ks-azureblob-483-searchindex-ks` (`searchIndex` kind) |
| Knowledge base | `knowledgebase849` |
| Agent | `blob-docs-assistant` (created by `agent`) |

The knowledge source must be **`searchIndex` kind**. `filterAddOn` exists only on
`SearchIndexKnowledgeSourceParams`; sending it to an `azureBlob`-kind source fails with
`400 Property 'filterAddOn' is not allowed`. A knowledge source's kind is immutable, so a
blob-kind source has to be wrapped by a `searchIndex` source pointed at the same generated
index — which is what `ks-azureblob-483-searchindex-ks` is.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Then create `.env`:

```ini
SEARCH_ENDPOINT=https://your-search-service.search.windows.net
# Optional. Leave blank to use DefaultAzureCredential (recommended).
SEARCH_API_KEY=

PROJECT_ENDPOINT=https://your-resource.services.ai.azure.com/api/projects/your-project
AGENT_MODEL=gpt-5.4
```

Optional overrides, only if your resources are named differently:

```ini
BLOB_INDEX_NAME=ks-azureblob-483-index
BLOB_KNOWLEDGE_SOURCE_NAME=ks-azureblob-483-searchindex-ks
BLOB_KNOWLEDGE_BASE_NAME=knowledgebase849
BLOB_AGENT_NAME=blob-docs-assistant
AZURE_TENANT_ID=
```

`SEARCH_ENDPOINT` and `PROJECT_ENDPOINT` are required by every command. A variable written
as `NAME=` with no value counts as unset, not as an empty string.

## Commands

```powershell
python foundry_iq_blob_demo.py docs       # what is indexed, per user
python foundry_iq_blob_demo.py retrieve   # filters applied, no agent involved
python foundry_iq_blob_demo.py agent      # create or update the prompt agent
python foundry_iq_blob_demo.py chat       # sign in, then chat as yourself
python foundry_iq_blob_demo.py verify     # assertions; --with-agent to include agent turns
python foundry_iq_blob_demo.py teardown   # delete ONLY the agent
```

`retrieve` with no arguments runs a built-in comparison across both users, with no sign-in.
Give it a query to run one yourself — that path signs you in, so add `--user` to skip the
browser while testing:

```powershell
python foundry_iq_blob_demo.py retrieve "what is the bonus?" --user admin --mms-id mms001
```

Typical `chat` session:

```
you> what is my bonus for mms002?
  [tool] search_my_documents({"query": "bonus", "mms_id": "mms002"})
  [filter] user_id eq 'admin@mngenvmcap987044.onmicrosoft.com' and mms_id eq 'mms002'  -> 1 result(s)

agent> Your bonus for mms002 is 8000 USD.
```

## Identity

`chat` signs the user in through Entra ID with `InteractiveBrowserCredential` and reads
the UPN from the issued token — so the identity comes from Entra, not from a command-line
argument, and a caller cannot assert an identity they do not hold.

Nothing is cached. Every run requests `prompt=select_account`, so the account picker
appears and you can sign in as a different user each time.

`--user <alias>` is a **testing override**, not a login. It prints a warning. There is
deliberately no default value: omitting it signs you in rather than silently falling back
to a privileged account.

The signed-in identity scopes retrieval only. Azure AI Search is still called with the
application's own credential, which is how a real backend behaves — end users are not
granted data-plane roles on the index.

A signed-in user with no indexed documents is not an error: the filter still pins them to
their own `user_id`, so they simply see nothing. Failing open would be the bug.

## Things worth knowing

**`user_id` is the full UPN.** It comes from the blob path, so it is
`admin@mngenvmcap987044.onmicrosoft.com`, not `admin`. Filtering on the short alias
matches zero documents.

**OData `eq` is case-sensitive**, and Entra may return a UPN in different casing than the
blob path stored (`dvalluru@MngEnvMCAP987044.onmicrosoft.com`). After sign-in the script
snaps the UPN to the value the index actually holds, comparing case-insensitively.

**`intents`, not `messages`.** `knowledgebase849` was created with
`retrievalReasoningEffort: minimal`, which rejects `messages` outright:
*"Messages input not supported when 'minimal' reasoning effort is requested. Use intents
input instead."* `KnowledgeRetrievalSemanticIntent` is accepted at any reasoning effort,
so the script uses it unconditionally — one code path, no fallback.

**`outputMode: extractiveData`** means the response text is a JSON array of
`{ref_id, content}` rather than prose. The script flattens it before handing it to the
model, and passes prose through unchanged if the knowledge base is ever switched to
`answerSynthesis`.

**Allow-lists are read from the index** at query time, filtered by `user_id`, rather than
hard-coded — so one user's allow-list can never contain another user's `mms_id`, and the
script keeps working as documents are added.

**`strict=False` on the function tool.** Under strict schema rules every property must be
listed in `required`, which would force the model to always send `mms_id` and `msg_id`.

## Verification

```powershell
python foundry_iq_blob_demo.py verify --with-agent
```

Checks that:

1. admin sees both of their own bonuses
2. admin sees no one else's documents
3. dvalluru sees their own document
4. dvalluru cannot see bonus documents
5. narrowing by `mms_id` keeps the `user_id` clause
6. narrowing returns only that one document
7. another user's `mms_id` is rejected
8. an OData injection through `mms_id` is rejected

and, with `--with-agent`:

9. the agent answers admin's bonus question
10. the agent leaks nothing to dvalluru, including under a prompt-injection attempt

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `400 Property 'filterAddOn' is not allowed` | The knowledge source is `azureBlob` kind. Filtering needs a `searchIndex`-kind source. |
| `400 ... Use intents input instead` | The knowledge base uses `minimal` reasoning effort and was sent `messages`. |
| `Unknown mms_id '...' for this user` | Working as intended — the value is not in that user's documents. |
| Retrieval returns nothing for a valid user | Check the casing of `user_id`, and that the alias is the full UPN. |
| `Missing environment variable(s)` | `SEARCH_ENDPOINT` and `PROJECT_ENDPOINT` are required by every command. |
