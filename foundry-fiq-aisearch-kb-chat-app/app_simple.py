"""
Foundry Isolated Knowledge-Base Chat App - Simplified Edition
---------------------------------------------------------------
A short, minimal version of app.py: same core behavior (Entra ID login,
per-user file isolation, ask questions, upload new files), with the
extra defensive comments/fallback trimmed out. See app.py + README.md
for the fully-annotated, production-grade version.

Run:
    pip install flask requests msal azure-identity azure-storage-blob
    set AZURE_SEARCH_ADMIN_KEY=<key>
    set ENTRA_CLIENT_ID=<app registration client id>
    set ENTRA_CLIENT_SECRET=<app registration client secret>
    set ENTRA_TENANT_ID=<tenant id>
    python app_simple.py
Then open http://127.0.0.1:5000
"""
import os
import re
import requests
import msal
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from werkzeug.utils import secure_filename
from flask import Flask, request, session, redirect, url_for, render_template_string

load_dotenv()  # loads a local .env file if present (e.g. when run from VS Code)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-secret-change-me")

# --- Azure AI Search config -------------------------------------------------
SEARCH_SERVICE = os.environ.get("AZURE_SEARCH_SERVICE", "<your-search-service>")
SEARCH_ENDPOINT = f"https://{SEARCH_SERVICE}.search.windows.net"
API_VERSION = "2025-11-01-preview"
KNOWLEDGE_BASE = os.environ.get("AZURE_SEARCH_KNOWLEDGE_BASE", "hier2-kb")
KNOWLEDGE_SOURCE = os.environ.get("AZURE_SEARCH_KNOWLEDGE_SOURCE", "hier2-ks")
INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX", "hier2-index")
INDEXER_NAME = os.environ.get("AZURE_SEARCH_INDEXER", "hier2-indexer")
ADMIN_KEY = os.environ.get("AZURE_SEARCH_ADMIN_KEY", "")

# --- Blob storage config (for uploads) --------------------------------------
STORAGE_ACCOUNT = os.environ.get("AZURE_STORAGE_ACCOUNT", "<your-storage-account>")
STORAGE_ACCOUNT_URL = f"https://{STORAGE_ACCOUNT}.blob.core.windows.net"
STORAGE_CONTAINER = os.environ.get("AZURE_STORAGE_CONTAINER", "hier")
_blob_client = None
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # mms_id/msg_id charset

# --- Entra ID (Microsoft login) config --------------------------------------
ENTRA_CLIENT_ID = os.environ.get("ENTRA_CLIENT_ID", "")
ENTRA_CLIENT_SECRET = os.environ.get("ENTRA_CLIENT_SECRET", "")
ENTRA_TENANT_ID = os.environ.get("ENTRA_TENANT_ID", "")
ENTRA_AUTHORITY = f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}"
ENTRA_SCOPE = ["User.Read"]
REDIRECT_PATH = "/getAToken"


def _msal_app():
    # No token cache is kept - only the ID token claims are needed once at
    # sign-in, and persisting MSAL's cache in the session cookie can push it
    # past the browser's ~4KB limit and silently break login.
    return msal.ConfidentialClientApplication(
        ENTRA_CLIENT_ID, authority=ENTRA_AUTHORITY, client_credential=ENTRA_CLIENT_SECRET
    )


def _search_post(path, body):
    url = f"{SEARCH_ENDPOINT}{path}?api-version={API_VERSION}"
    r = requests.post(url, headers={"api-key": ADMIN_KEY, "Content-Type": "application/json"}, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def list_user_files(user_id):
    """Every file this user_id owns, discovered from the index (never client-typed)."""
    if not ADMIN_KEY:
        return []
    docs = _search_post(f"/indexes('{INDEX_NAME}')/docs/search", {
        "search": "*", "filter": f"user_id eq '{user_id}'",
        "select": "mms_id,msg_id,metadata_storage_name", "top": 1000,
    }).get("value", [])
    seen, files = set(), []
    for d in docs:
        key = (d.get("mms_id"), d.get("msg_id"), d.get("metadata_storage_name"))
        if all(key) and key not in seen:
            seen.add(key)
            files.append({"mms_id": key[0], "msg_id": key[1], "file_name": key[2]})
    return sorted(files, key=lambda f: (f["mms_id"], f["msg_id"], f["file_name"]))


def build_multi_filter(user_id, files):
    """OData filter matching ANY selected file, always anchored to user_id."""
    clauses = [
        f"(mms_id eq '{f['mms_id']}' and msg_id eq '{f['msg_id']}' and metadata_storage_name eq '{f['file_name']}')"
        for f in files
    ]
    return f"user_id eq '{user_id}' and ({' or '.join(clauses)})"


def files_are_permitted(user_id, files):
    """Re-check every selected file against a fresh server-side lookup."""
    allowed = {(f["mms_id"], f["msg_id"], f["file_name"]) for f in list_user_files(user_id)}
    return all((f["mms_id"], f["msg_id"], f["file_name"]) in allowed for f in files)


def retrieve(question, filter_add_on):
    if not ADMIN_KEY:
        return "ERROR: AZURE_SEARCH_ADMIN_KEY is not set.", None
    data = _search_post(f"/knowledgeBases('{KNOWLEDGE_BASE}')/retrieve", {
        "messages": [{"role": "user", "content": [{"type": "text", "text": question}]}],
        "knowledgeSourceParams": [{
            "knowledgeSourceName": KNOWLEDGE_SOURCE, "kind": "searchIndex", "filterAddOn": filter_add_on,
        }],
    })
    try:
        answer = data["response"][0]["content"][0]["text"]
    except (KeyError, IndexError):
        answer = "(no answer text returned)"

    # Vague prompts can make the KB's semantic reranker return nothing even
    # though the permitted document exists; fall back to a direct search on
    # the SAME filter (never widened) so the user still gets an answer.
    if answer.strip() in ("", "[]", "(no answer text returned)"):
        docs = _search_post(f"/indexes('{INDEX_NAME}')/docs/search", {
            "search": "*", "filter": filter_add_on, "select": "content,metadata_storage_name", "top": 20,
        }).get("value", [])
        parts = [f"[{d.get('metadata_storage_name', 'file')}]: {d.get('content', '').strip()}" for d in docs if d.get("content")]
        if parts:
            answer = "\n".join(parts) + "\n\n(Shown via direct lookup - try a more specific question next time.)"
    return answer, filter_add_on


def upload_user_file(user_id, mms_id, msg_id, filename, file_stream):
    """Upload only ever lands under the CALLER'S OWN user_id."""
    global _blob_client
    if _blob_client is None:
        _blob_client = BlobServiceClient(STORAGE_ACCOUNT_URL, credential=DefaultAzureCredential())
    safe_name = secure_filename(filename) or "upload.txt"
    blob_name = f"{user_id}/{mms_id}/{msg_id}/{safe_name}"
    _blob_client.get_container_client(STORAGE_CONTAINER).upload_blob(name=blob_name, data=file_stream, overwrite=True)
    return blob_name


def trigger_indexer():
    if not ADMIN_KEY:
        return
    try:
        requests.post(f"{SEARCH_ENDPOINT}/indexers('{INDEXER_NAME}')/run?api-version={API_VERSION}",
                      headers={"api-key": ADMIN_KEY}, timeout=30)
    except requests.RequestException:
        pass


LOGIN_PAGE = """
<!doctype html><html><head><title>Login</title></head>
<body style="font-family:sans-serif;max-width:400px;margin:60px auto;text-align:center">
<h2>Sign in</h2>
{% if error %}<p style="color:red">{{ error }}</p>{% endif %}
<a href="{{ url_for('login') }}" style="display:inline-block;padding:10px 20px;background:#2f2f2f;color:#fff;border-radius:4px;text-decoration:none">Sign in with Microsoft</a>
</body></html>
"""

CHAT_PAGE = """
<!doctype html><html><head><title>Isolated KB Chat (Simple)</title></head>
<body style="font-family:sans-serif;max-width:750px;margin:40px auto">
<div style="display:flex;justify-content:space-between;align-items:center">
  <h2>Hello, {{ user_id }}</h2>
  <a href="{{ url_for('logout') }}">Log out</a>
</div>
{% if upload_message %}<p style="color:{{ 'red' if upload_error else 'green' }}">{{ upload_message }}</p>{% endif %}
<form method="post">
  <div style="border:1px solid #ddd;border-radius:6px;padding:10px;max-height:220px;overflow-y:auto">
    {% for f in files %}
    <label style="display:block;padding:4px 0">
      <input type="checkbox" name="file" value="{{ f.mms_id }}|{{ f.msg_id }}|{{ f.file_name }}">
      {{ f.mms_id }} / {{ f.msg_id }} / <code>{{ f.file_name }}</code>
    </label>
    {% else %}<i>No files found for your account.</i>{% endfor %}
  </div>
  <br>
  <input name="question" style="width:80%;padding:8px" placeholder="Ask about the selected file(s)...">
  <button type="submit" style="padding:8px 16px">Ask</button>
</form>
{% if answer %}
<div style="margin-top:20px;padding:16px;background:#f4f4f4;border-radius:6px">
  <b>Question:</b> {{ question }}<br><br><b>Answer:</b> {{ answer }}<br><br>
  <b>Filter applied server-side:</b> <code>{{ filter_used }}</code>
</div>
{% endif %}
<hr style="margin-top:30px">
<h3>Upload a new file</h3>
<form method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
  <input name="mms_id" style="padding:8px" placeholder="mms_id" required><br><br>
  <input name="msg_id" style="padding:8px" placeholder="msg_id" required><br><br>
  <input type="file" name="file" required><br><br>
  <button type="submit" style="padding:8px 16px">Upload</button>
</form>
</body></html>
"""


@app.route("/", methods=["GET"])
def index():
    if session.get("user"):
        return redirect(url_for("chat"))
    return render_template_string(LOGIN_PAGE, error=request.args.get("error"))


@app.route("/login")
def login():
    if not (ENTRA_CLIENT_ID and ENTRA_CLIENT_SECRET and ENTRA_TENANT_ID):
        return render_template_string(LOGIN_PAGE, error="Entra ID is not configured.")
    session["flow"] = _msal_app().initiate_auth_code_flow(
        ENTRA_SCOPE, redirect_uri=url_for("get_a_token", _external=True), prompt="select_account"
    )
    return redirect(session["flow"]["auth_uri"])


@app.route(REDIRECT_PATH)
def get_a_token():
    try:
        result = _msal_app().acquire_token_by_auth_code_flow(session.get("flow", {}), request.args)
    except ValueError as e:
        return redirect(url_for("index", error=f"Sign-in failed: {e}"))
    if "error" in result:
        return redirect(url_for("index", error=result.get("error_description", result["error"])))
    claims = result.get("id_token_claims", {})
    user_id = claims.get("preferred_username") or claims.get("upn") or claims.get("oid")
    session.pop("flow", None)
    session["user"] = {"user_id": user_id}
    return redirect(url_for("chat"))


@app.route("/chat", methods=["GET", "POST"])
def chat():
    if not session.get("user"):
        return redirect(url_for("index"))
    user_id = session["user"]["user_id"]
    files = list_user_files(user_id)
    answer = question = filter_used = None
    if request.method == "POST":
        question = request.form.get("question", "")
        parsed = []
        for raw in request.form.getlist("file"):
            parts = raw.split("|", 2)
            if len(parts) == 3:
                parsed.append({"mms_id": parts[0], "msg_id": parts[1], "file_name": parts[2]})
        if not parsed:
            answer, filter_used = "Please select at least one file.", "(no files selected)"
        elif not files_are_permitted(user_id, parsed):
            answer, filter_used = "Access denied: file(s) do not belong to your account.", "(rejected)"
        else:
            filter_used = build_multi_filter(user_id, parsed)
            answer, _ = retrieve(question, filter_used)
    return render_template_string(
        CHAT_PAGE, user_id=user_id, files=files, answer=answer, question=question,
        filter_used=filter_used, upload_message=request.args.get("upload_message"),
        upload_error=request.args.get("upload_error") == "1",
    )


@app.route("/upload", methods=["POST"])
def upload():
    if not session.get("user"):
        return redirect(url_for("index"))
    user_id = session["user"]["user_id"]
    mms_id, msg_id = request.form.get("mms_id", "").strip(), request.form.get("msg_id", "").strip()
    uploaded = request.files.get("file")

    def back(message, error=False):
        return redirect(url_for("chat", upload_message=message, upload_error="1" if error else "0"))

    if not _SAFE_ID_RE.match(mms_id) or not _SAFE_ID_RE.match(msg_id):
        return back("mms_id/msg_id may only contain letters, numbers, '-' and '_'.", error=True)
    if not uploaded or not uploaded.filename:
        return back("Please choose a file to upload.", error=True)
    try:
        blob_name = upload_user_file(user_id, mms_id, msg_id, uploaded.filename, uploaded.stream)
    except Exception as e:
        return back(f"Upload failed: {e}", error=True)
    trigger_indexer()
    return back(f"Uploaded '{blob_name}'. It will be searchable once the index refreshes.")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(f"{ENTRA_AUTHORITY}/oauth2/v2.0/logout?post_logout_redirect_uri={url_for('index', _external=True)}")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
