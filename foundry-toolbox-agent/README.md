# Foundry Toolbox Agent

Minimal two-script project that:

1. **Creates** a hosted Microsoft Foundry prompt agent and **attaches a
   Foundry Toolbox** to it as an MCP tool (server-side; the toolbox is
   fronted by a Foundry-managed `ProjectManagedIdentity` connection).
2. **Invokes** that agent through the **Microsoft Agent Framework**.

```
foundry-toolbox-agent/
├── create_agent_with_toolbox.py   ← step 1: create / update the agent
├── invoke_foundry_agent.py        ← step 2: call the agent via Agent Framework
├── requirements.txt
├── .env / .env.example
├── .gitignore
└── README.md
```

## Prerequisites

- Python 3.11+
- An Azure CLI login with **Foundry User** role on your Foundry project
  (`az login`)
- A Foundry **Toolbox** already created in the portal (you only need to do
  this once: **Foundry portal → Toolboxes → Add toolbox**; attach your MCP
  server to it as a tool inside the toolbox)

## Setup (one-time)

```powershell
cd foundry-toolbox-agent

# Create / activate venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies (azure-ai-agents is preview → use --pre)
pip install --pre -r requirements.txt

# Configure environment
Copy-Item .env.example .env
notepad .env   # fill in FOUNDRY_PROJECT_ENDPOINT, TOOLBOX_NAME, etc.

# Authenticate to Azure
az login
```

## 1. Create / update the hosted agent

```powershell
python .\create_agent_with_toolbox.py
```

This script:
- Creates (or updates) a managed project connection that targets your
  toolbox MCP endpoint, authenticating with the Foundry project's
  managed identity (`ProjectManagedIdentity` + audience
  `https://ai.azure.com`).
- Creates (or updates) the hosted Foundry prompt agent named
  `NEW_AGENT_NAME`. The agent's `tools` list includes an `mcp` entry
  referencing the connection above by `project_connection_id`.
- If the agent already exists, a NEW version is published that becomes
  `@latest` (existing instructions/model are preserved; the toolbox
  entry is refreshed).

Expected output:

```
[step 1/2] Connection 'testtoolbox-conn' -> toolbox 'TestToolbox' (200)
[step 2/2] Creating new agent 'ToolboxAgent'.
[step 2/2] Agent 'ToolboxAgent' -> latest is v1 (model=gpt-4.1, toolbox='TestToolbox' via connection='testtoolbox-conn').
```

## 2. Invoke the agent via Microsoft Agent Framework

```powershell
python .\invoke_foundry_agent.py
```

This uses `agent_framework.foundry.FoundryAgent` to bind to the hosted
agent and send a prompt. The toolbox lives entirely server-side; the
client only needs the agent's name (and Azure credentials).

Expected output:

```
[Agent] ToolboxAgent v1

User: Convert 40 celsius to fahrenheit

Agent: 40°C = 105°F
```

The model uses the toolbox's routing functions (`tool_search` then
`call_tool`) to invoke the right tool from your MCP server inside the
toolbox, and returns the result.

## Environment variables

| Variable | Used by | Required | Description |
|---|---|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | both | ✅ | `https://<account>.services.ai.azure.com/api/projects/<project>` |
| `NEW_AGENT_NAME` | both | ✅ | Hosted agent name (e.g. `ToolboxAgent`) |
| `TOOLBOX_NAME` | create | ✅ | Toolbox to attach (e.g. `TestToolbox`) |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | create | ✅ | Model deployment (used when creating a fresh agent) |
| `TOOLBOX_CONNECTION_NAME` | create | optional | Default: `<toolbox-lowercase>-conn` |
| `AGENT_INSTRUCTIONS` | create | optional | System prompt for fresh agents |
| `FOUNDRY_AGENT_VERSION` | invoke | optional | Pin to a specific agent version; otherwise `@latest` |
| `USER_PROMPT` | invoke | optional | Prompt sent to the agent (default: a sample temperature conversion) |

## Notes

- The auth from the agent runtime to the toolbox endpoint is handled by
  the Foundry project's managed identity. No tokens or secrets are
  stored in code or in the agent definition.
- Auth from the toolbox to the MCP server attached *inside* the toolbox
  (e.g. `TestToolboxConn`) is a separate concern, configured on that
  connection in the Foundry portal (key-based / Entra / OAuth identity
  passthrough). See the Foundry docs for details:
  https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/mcp-authentication
- `create_agent_with_toolbox.py` uses the ARM REST API to create the
  managed connection because `azure-ai-projects` doesn't yet expose
  connection creation.

## References

- [Foundry Toolbox docs](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/toolbox?pivots=python)
- [MCP authentication for Foundry agents](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/mcp-authentication)
- [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)
