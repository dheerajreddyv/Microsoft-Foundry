# Foundry Isolated Knowledge-Base Chat App

A minimal Flask app demonstrating **per-user data isolation** on top of an
Azure AI Search "Foundry IQ" knowledge base, with real Microsoft Entra ID
login and an in-app file upload feature.

This app exists to prove one core point: **the Foundry Portal chat UI
cannot do per-request access control** (it has no way to inject a
per-caller filter into `retrieve` calls), so any production scenario that
needs "user A can only see user A's documents" must front the knowledge
base with a small trusted server like this one.

## Two versions in this folder

| File | Purpose |
|---|---|
| `app_simple.py` | **The version actively used for running/testing.** Same behavior and identical isolation guarantees as `app.py`, but short: comments trimmed to one-liners, repeated request-building collapsed into a single `_search_post()` helper, and unused code removed. **This README's code walkthrough below uses `app_simple.py`.** |
| `app.py` | The original, heavily-commented version. Keeps extra defensive comments explaining *why* each choice was made - useful as deeper background reading, but functionally identical to `app_simple.py`. |

Both were verified to produce identical results for the same real test
data (file discovery, permission checks, filter strings, and answers).

---

## 1. What this app does

1. A user signs in with their **real Microsoft Entra ID account** (no
   passwords are stored or checked by this app itself).
2. The app looks up which files belong to that signed-in user by querying
   the search index, filtered on their own identity - the file list is
   **never typed in by the user**, it's discovered server-side.
3. The user picks one or more of their own files and asks a question.
4. The app calls Azure AI Search's Knowledge Base `retrieve` API, passing a
   `filterAddOn` built entirely from the **trusted server-side identity**
   (never from anything the browser could tamper with).
5. The user can also **upload a new file** through the app; it lands in
   blob storage under their own identity, and a search indexer run makes
   it searchable within about a minute.

---

## 2. Architecture

```
Browser (user)
   |
   |  1. GET /login  -> redirect to Microsoft Entra ID sign-in
   |  2. Entra ID authenticates the user (password + MFA), redirects back
   |     with an authorization code
   v
Flask app (app_simple.py)
   |  3. Exchanges the code for an ID token (MSAL), reads the verified
   |     `preferred_username` (UPN) claim -> this becomes `user_id`
   |  4. Stores {user_id} in the server-side Flask session
   |
   |  5. GET/POST /chat
   |       -> list_user_files(user_id)        [Azure AI Search /docs/search]
   |       -> files_are_permitted(user_id, ..) [re-derives allow-list server-side]
   |       -> build_multi_filter(user_id, ..)  [OData filter string]
   |       -> retrieve(question, filter)       [Knowledge Base /retrieve]
   |
   |  6. POST /upload
   |       -> upload_user_file(user_id, ...)   [Azure Blob Storage]
   |       -> trigger_indexer()                [Azure AI Search /indexers/run]
   v
Azure AI Search                      Azure Blob Storage
  hier2-kb        (Knowledge Base)     <your-storage-account> / hier container
  hier2-ks        (Knowledge Source)   <user_id>/<mms_id>/<msg_id>/<file>
  hier2-index     (search index)
  hier2-indexer   (blob -> index)
  hier2-datasource
```

### Azure resources used

| Resource | Name | Purpose |
|---|---|---|
| Search service | `<your-search-service>` | Hosts the index, knowledge base, indexer |
| Knowledge Base | `hier2-kb` | Agentic retrieval (LLM query planning + semantic rerank) over `hier2-index` |
| Knowledge Source | `hier2-ks` | Points the KB at `hier2-index` (kind: `searchIndex`) |
| Index | `hier2-index` | Fields: `id`, `content`, `metadata_storage_name`, `metadata_storage_path`, `user_id`, `mms_id`, `msg_id` (last three are `filterable`) |
| Indexer | `hier2-indexer` | Populates the index from blob storage; manual run only (no schedule) |
| Data source | `hier2-datasource` | Points the indexer at the `hier` blob container |
| Storage account | `<your-storage-account>` | Blob container `hier`; **key-based auth disabled**, Entra ID/RBAC only |
| Entra App Registration | `foundry-chat-app` | Used for user sign-in (Authorization Code Flow via MSAL) |

---

## 3. How the isolation / filtering logic works (the important part)

### 3.1 Where `user_id` comes from

`user_id` is **never** read from a form field, query string, or cookie the
client could edit. It comes from exactly one place: the `preferred_username`
claim inside the **ID token Microsoft Entra ID issues after a real,
verified sign-in** (`get_a_token()` -> `acquire_token_by_auth_code_flow()`).
That value is stored server-side in the Flask session and is the *only*
thing every later filter is built from.

```python
claims = result.get("id_token_claims", {})
user_id = claims.get("preferred_username") or claims.get("upn") or claims.get("oid")
session["user"] = {"user_id": user_id}
```

### 3.2 How files map to a user in the index

Every document in `hier2-index` has three filterable fields: `user_id`,
`mms_id`, `msg_id`. These are populated automatically by `hier2-indexer`
using **`extractTokenAtPosition` field mappings** on
`metadata_storage_path` (the blob's full path), splitting on `/`:

```
hier / <user_id> / <mms_id> / <msg_id> / <filename>
        position4   position5  position6
```

So a blob physically stored at:

```
hier/dvalluru@MngEnvMCAP987044.onmicrosoft.com/mmsA01/msgA01/notes.txt
```

is indexed with `user_id = "dvalluru@MngEnvMCAP987044.onmicrosoft.com"`,
`mms_id = "mmsA01"`, `msg_id = "msgA01"`. **The folder structure in blob
storage IS the source of truth for ownership** - there's no separate ACL
table to keep in sync.

### 3.3 Discovering a user's own files (`list_user_files`)

```python
def list_user_files(user_id):
    docs = _search_post(f"/indexes('{INDEX_NAME}')/docs/search", {
        "search": "*", "filter": f"user_id eq '{user_id}'",
        "select": "mms_id,msg_id,metadata_storage_name", "top": 1000,
    }).get("value", [])
    ...
```

This always filters on the **signed-in user's own `user_id`** - the
function takes `user_id` as a parameter, but every caller in the app
passes `session["user"]["user_id"]`, never anything from the request. The
returned list is what's shown as selectable checkboxes on `/chat` - the
user never types a file path, so there's nothing to tamper with there.

### 3.4 Building the query filter (`build_multi_filter`)

```python
def build_multi_filter(user_id, files):
    clauses = []
    for f in files:
        clauses.append(
            f"(mms_id eq '{f['mms_id']}' and msg_id eq '{f['msg_id']}' "
            f"and metadata_storage_name eq '{f['file_name']}')"
        )
    return f"user_id eq '{user_id}' and ({' or '.join(clauses)})"
```

Produces something like:

```
user_id eq 'alice' and ((mms_id eq 'mms001' and msg_id eq 'msg001' and metadata_storage_name eq 'report.txt') or (mms_id eq 'mms002' and msg_id eq 'msg002' and metadata_storage_name eq 'notes.txt'))
```

Every clause is **AND-ed with `user_id eq '<signed-in user>'` at the top
level** - even if the inner `or` list were somehow manipulated to name
files belonging to someone else, the outer `user_id` clause still
mathematically excludes them from the result set. This is the core
isolation invariant of the whole app.

### 3.5 Defense in depth: `files_are_permitted`

Before ever calling `retrieve`, the server **re-derives** the caller's
allowed file list from the index (step 3.3) and checks every file the
browser selected is in that fresh list:

```python
def files_are_permitted(user_id, files):
    allowed = {(f["mms_id"], f["msg_id"], f["file_name"]) for f in list_user_files(user_id)}
    return all((f["mms_id"], f["msg_id"], f["file_name"]) in allowed for f in files)
```

This means even a tampered POST body (e.g. a hand-crafted checkbox value
naming another user's `mms_id`/`msg_id`/filename) is rejected **before**
any search call is made - the request never reaches `retrieve` at all,
it's answered with "Access denied" directly.

### 3.6 Calling the Knowledge Base (`retrieve`)

```python
body = {
    "messages": [{"role": "user", "content": [{"type": "text", "text": question}]}],
    "knowledgeSourceParams": [{
        "knowledgeSourceName": KNOWLEDGE_SOURCE,
        "kind": "searchIndex",
        "filterAddOn": filter_add_on,   # <- from build_multi_filter, above
    }],
}
POST {SEARCH_ENDPOINT}/knowledgeBases('hier2-kb')/retrieve
```

`filterAddOn` is injected server-side into every call. The Foundry Portal
chat UI has no equivalent parameter for a caller-supplied filter, which is
exactly why it's unsafe for multi-tenant/isolated scenarios - this app is
the trusted intermediary that always adds it.

### 3.7 Fallback for vague prompts

The KB's `retrieve` uses **agentic/semantic retrieval** - an LLM plans a
search query from the prompt and reranks results. Vague prompts (e.g.
"get the details") give it little to work with and can return an empty
answer even though the permitted document exists. When that happens, the
app falls back to a **direct index search using the exact same
`filterAddOn`** (never widened, never client-influenced) so the user still
sees their permitted content:

```python
if answer.strip() in ("", "[]", "(no answer text returned)"):
    docs = _search_post(f"/indexes('{INDEX_NAME}')/docs/search", {
        "search": "*", "filter": filter_add_on, "select": "content,metadata_storage_name", "top": 20,
    }).get("value", [])
    parts = [f"[{d.get('metadata_storage_name', 'file')}]: {d.get('content', '').strip()}" for d in docs if d.get("content")]
    if parts:
        answer = "\n".join(parts) + "\n\n(Shown via direct lookup - try a more specific question next time.)"
```

This fallback can never leak another user's data - it reuses the identical
filter string that was just sent to the KB.

### 3.8 Upload isolation (`upload_user_file`)

```python
def upload_user_file(user_id, mms_id, msg_id, filename, file_stream):
    safe_name = secure_filename(filename) or "upload.txt"
    blob_name = f"{user_id}/{mms_id}/{msg_id}/{safe_name}"
    _blob_client.get_container_client(STORAGE_CONTAINER).upload_blob(name=blob_name, data=file_stream, overwrite=True)
    return blob_name
```

`user_id` here is always `session["user"]["user_id"]` - the route handler
never reads it from the form - so a user can only ever create blobs (and
therefore index documents) under their **own** identity prefix. Since
ownership is derived purely from the path (section 3.2), this is
sufficient to guarantee a newly uploaded file is only ever visible to its
uploader.

`mms_id`/`msg_id` **are** taken from the upload form (the user picks a
new or existing folder name), so they're validated against a strict
allow-list regex before use:

```python
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
```

This prevents two classes of problems that only became possible once
these values are free-typed instead of always coming from trusted
server-side discovery:
- **Path traversal** in the blob name (e.g. `../../otheruser`).
- **OData filter injection** in `build_multi_filter`/`list_user_files`
  (e.g. a value like `x' or user_id ne '` breaking out of the quoted
  filter clause).

---

## 4. Authentication (Microsoft Entra ID)

- Uses MSAL's **Confidential Client, Authorization Code Flow**.
- `/login` starts the flow with `prompt=select_account`, so it always shows
  an account picker/credential prompt rather than silently reusing an
  existing SSO session (important on Entra-joined machines/VMs, which
  otherwise auto-sign-in via the device's Primary Refresh Token).
- `/getAToken` exchanges the returned code for an ID token and reads
  `preferred_username` as the trusted `user_id`.
- **No token cache is persisted.** Only `id_token_claims` are needed once
  at login; MSAL's serializable token cache is intentionally *not* wired
  into the Flask session, because it can push the session cookie past the
  browser's ~4KB limit, causing silent cookie drops and login loops.
- `/logout` clears the local session **and** redirects through Entra ID's
  own logout endpoint, ending the browser's Entra session too.

Required environment variables: `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`,
`ENTRA_TENANT_ID`, plus a redirect URI registered on the app
(`/getAToken`) for whatever host/port it runs on. See section 6 for how
these are supplied via a local `.env` file.

---

## 5. File upload & indexing pipeline

1. User fills in `mms_id`, `msg_id` (new or existing), and picks a file on
   `/chat`.
2. `POST /upload` validates the IDs (section 3.8), then calls
   `upload_user_file()`, which writes the blob to
   `hier/<user_id>/<mms_id>/<msg_id>/<filename>` using
   `DefaultAzureCredential`.
3. `trigger_indexer()` kicks off `hier2-indexer` (best-effort - if a run
   is already in progress, the request is simply ignored).
4. Once the indexer finishes (usually well under a minute for a single
   file), the new document appears in `hier2-index` with `user_id`/
   `mms_id`/`msg_id` derived from its path, and shows up next time
   `list_user_files()` runs.

**Important constraint:** the storage account's network firewall only
allows traffic from its VNet / private endpoints, and key-based auth is
disabled account-wide. This means:
- Uploads only succeed when the app runs somewhere with **network access**
  to the storage account (e.g. a VM on the same VNet) - not from an
  arbitrary machine on the internet.
- Whatever identity `DefaultAzureCredential` resolves to (a VM's managed
  identity, or a developer's `az login` locally) must be granted the
  **Storage Blob Data Contributor** role on the storage account.

---

## 6. Running the app

Both scripts call `load_dotenv()` at startup (via `python-dotenv`), so
the simplest way to run either one - including from VS Code's Run/Debug
button or integrated terminal - is to keep a `.env` file in this folder
with the real values already filled in:

```
AZURE_SEARCH_ADMIN_KEY=<search service admin key>
ENTRA_CLIENT_ID=f1876b09-8487-4c7b-9f53-92fa2b6dc95f
ENTRA_CLIENT_SECRET=<app registration client secret>
ENTRA_TENANT_ID=aa125f77-bd37-4043-a8f9-9cff6ea29ef9
FLASK_SECRET_KEY=<any random string>
```

Then just:

```powershell
pip install flask requests msal azure-identity azure-storage-blob python-dotenv

python app_simple.py
```

Run `python app.py` instead if you want the fully-commented version -
both listen on the same port (5000), so only run one at a time.

Then open `http://localhost:5000` (or `http://127.0.0.1:5000`) and click
**Sign in with Microsoft**.

> If you'd rather not use a `.env` file, the same variables can be set
> per-terminal-session with `$env:VAR = "value"` before running - this is
> how the app is configured on the shared test VM, where they're set as
> machine-level variables since the app runs there continuously as a
> background scheduled task (`FoundryChatApp`).
>
> ⚠️ `.env` and `_client_secret.txt` both contain real secrets - do not
> commit either to source control.

---

## 7. Known limitations / things to keep in mind

- This is a **demonstration app**, not hardened for production: the
  Entra client secret currently lives in a local file
  (`_client_secret.txt`) rather than a secret store like Key Vault - move
  it before any real deployment.
- The Knowledge Base's semantic retrieval can be inconsistent on very
  short, single-line test documents and very generic prompts; the
  fallback in section 3.7 mitigates this for testing, but real production
  content (longer, more substantive documents) should rerank more
  reliably without needing the fallback.
- `hier2-indexer` has no schedule - new uploads only become searchable
  after `trigger_indexer()` runs (triggered automatically by `/upload`) and
  completes; there can be a short delay (seconds to ~1 minute).
