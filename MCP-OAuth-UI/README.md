# MCP OAuth Agent — UI + API

A web UI that integrates with the Microsoft Foundry MCP OAuth agent. The flow is:

```
UI (Browser) → FastAPI Backend → Azure AI Agent (with MCP OAuth)
```

## Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment:** Copy `.env.example` to `.env` and fill in your values:
   ```bash
   cp .env.example .env
   ```

3. **Run the server:**
   ```bash
   python app.py
   ```

4. **Open the UI:** Navigate to `http://localhost:8000`

## How It Works

1. User types a message in the chat UI
2. UI calls `POST /api/chat` with the message
3. Backend creates a conversation and invokes the agent
4. If the MCP server requires OAuth consent:
   - API returns `status: "consent_required"` with consent links
   - UI shows a banner with a link that opens in a new tab
   - User authorizes in the browser
   - User clicks "I've completed consent — Continue"
   - UI calls `POST /api/chat/resume` to continue the conversation
5. Agent completes the task and the answer is shown in the chat

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serves the chat UI |
| `POST` | `/api/chat` | Start a new agent conversation |
| `POST` | `/api/chat/resume` | Resume after OAuth consent |

### POST /api/chat
```json
{ "message": "Use the multiply tool to compute 17 * 23" }
```

### POST /api/chat/resume
```json
{ "conversation_id": "conv_xxx" }
```

### Response (both endpoints)
```json
{
  "conversation_id": "conv_xxx",
  "response_id": "resp_xxx",
  "status": "completed | consent_required",
  "answer": "The result is 391",
  "consent_links": [{"server_label": "...", "consent_link": "https://..."}]
}
```
