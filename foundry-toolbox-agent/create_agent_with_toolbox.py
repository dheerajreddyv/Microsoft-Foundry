"""Create / update a hosted Foundry prompt agent with a Foundry Toolbox attached.

Steps:
  1. Ensure a managed project connection points at the toolbox MCP endpoint
     using the project managed identity (ARM REST).
  2. Publish an agent version whose tools list references that connection.
"""

import os

import requests
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.core.exceptions import ResourceNotFoundError

load_dotenv()

CONN_API_VERSION = "2025-06-01"
DATA_API_VERSION = "2025-11-15-preview"


def _connection_parent_arm_id(project_endpoint: str, data_token: str) -> str:
    """Return the ARM id of '.../projects/<proj>/connections' for this project
    by inspecting any existing connection (the SDK doesn't expose this path)."""
    r = requests.get(
        f"{project_endpoint}/connections?api-version={DATA_API_VERSION}",
        headers={"Authorization": f"Bearer {data_token}"},
    )
    r.raise_for_status()
    items = r.json().get("value") or []
    if not items:
        raise SystemExit("No connections exist in this project; create one in the portal first.")
    detail = requests.get(
        f"{project_endpoint}/connections/{items[0]['name']}?api-version={DATA_API_VERSION}",
        headers={"Authorization": f"Bearer {data_token}"},
    ).json()
    return detail["id"].rsplit("/", 1)[0]


def ensure_connection(project_endpoint: str, toolbox: str, conn_name: str) -> None:
    cred = DefaultAzureCredential()
    data_token = cred.get_token("https://ai.azure.com/.default").token
    arm_token = cred.get_token("https://management.azure.com/.default").token

    parent = _connection_parent_arm_id(project_endpoint, data_token)
    arm_id = f"{parent}/{conn_name}"
    body = {"properties": {
        "category": "RemoteTool",
        "target": f"{project_endpoint}/toolboxes/{toolbox}/mcp?api-version=v1",
        "authType": "ProjectManagedIdentity",
        "audience": "https://ai.azure.com",
        "isSharedToAll": False,
        "metadata": {"type": "custom_MCP"},
    }}
    r = requests.put(
        f"https://management.azure.com{arm_id}?api-version={CONN_API_VERSION}",
        headers={"Authorization": f"Bearer {arm_token}", "Content-Type": "application/json"},
        json=body,
    )
    r.raise_for_status()
    print(f"[1/2] Connection '{conn_name}' -> toolbox '{toolbox}' ({r.status_code})")


def publish_agent(project_endpoint: str, agent_name: str, model: str,
                  instructions: str, toolbox: str, conn_name: str) -> str:
    toolbox_tool = {
        "type": "mcp",
        "server_label": "foundry_toolbox",
        "server_url": f"{project_endpoint}/toolboxes/{toolbox}/mcp?api-version=v1",
        "require_approval": "never",
        "project_connection_id": conn_name,
    }

    with (
        DefaultAzureCredential() as cred,
        AIProjectClient(endpoint=project_endpoint, credential=cred) as project,
    ):
        try:
            existing = project.agents.get(agent_name)["versions"]["latest"]["definition"]
            tools = [t for t in (existing.get("tools") or [])
                     if not (t.get("type") == "mcp" and "/toolboxes/" in (t.get("server_url") or ""))]
            definition = {
                "kind": "prompt",
                "model": existing.get("model") or model,
                "instructions": instructions or existing.get("instructions") or "",
                "tools": tools + [toolbox_tool],
            }
            action = "updated"
        except ResourceNotFoundError:
            definition = {"kind": "prompt", "model": model,
                          "instructions": instructions, "tools": [toolbox_tool]}
            action = "created"

        new = project.agents.create_version(agent_name, definition=definition)
        version = str(new["version"])
        print(f"[2/2] Agent '{agent_name}' {action} -> v{version} (model={definition['model']})")
        return version


def main() -> None:
    project_endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
    agent_name = os.environ["NEW_AGENT_NAME"]
    model = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
    toolbox = os.environ["TOOLBOX_NAME"]
    conn_name = os.environ.get("TOOLBOX_CONNECTION_NAME", f"{toolbox.lower()}-conn")
    instructions = os.environ.get(
        "AGENT_INSTRUCTIONS",
        "You are a helpful assistant with access to a Foundry toolbox. "
        "Use tool_search to find a tool and call_tool to invoke it.",
    )

    ensure_connection(project_endpoint, toolbox, conn_name)
    publish_agent(project_endpoint, agent_name, model, instructions, toolbox, conn_name)


if __name__ == "__main__":
    main()
