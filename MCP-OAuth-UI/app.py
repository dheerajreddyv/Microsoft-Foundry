"""
FastAPI backend that wraps the MCP OAuth Agent.
Flow: UI -> POST /api/chat -> Agent call -> returns consent_link or answer.
After user consents in browser, UI -> POST /api/chat/resume -> continues agent.
"""

import os
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition, MCPTool
from openai.types.responses.response_input_param import McpApprovalResponse

load_dotenv()

# --------------- Configuration ---------------
endpoint = os.environ["AZURE_AI_PROJECT_ENDPOINT"]
model_deployment = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
oauth_connection_id = os.environ["MCP_OAUTH_CONNECTION_ID"]
oauth_server_url = os.environ["MCP_OAUTH_SERVER_URL"]

# --------------- Shared clients (created at startup) ---------------
credential = None
project_client = None
openai_client = None
agent = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize Azure clients and create the agent on startup."""
    global credential, project_client, openai_client, agent

    credential = DefaultAzureCredential()
    project_client = AIProjectClient(endpoint=endpoint, credential=credential)
    openai_client = project_client.get_openai_client()

    mcp_tool = MCPTool(
        server_label="My-Azure-MCP-Server",
        server_url=oauth_server_url,
        require_approval="never",
        project_connection_id=oauth_connection_id,
    )

    agent = project_client.agents.create_version(
        agent_name="FoundryNew-MCP-OAuth-Agent",
        definition=PromptAgentDefinition(
            model=model_deployment,
            instructions=(
                "You are an assistant that MUST use MCP tools for all tasks the user asks about. "
                "ALWAYS call an available MCP tool to satisfy the user's request when one applies. "
                "NEVER fabricate data the MCP server should provide. "
                "Report the exact values returned by the tool without altering them. "
                "If no MCP tool is suitable, say so explicitly."
            ),
            tools=[mcp_tool],
        ),
    )
    print(f"Agent created: {agent.name} (id: {agent.id})")

    yield

    # Cleanup
    openai_client.close()
    project_client.close()
    credential.close()


app = FastAPI(title="MCP OAuth Agent API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static UI files
app.mount("/static", StaticFiles(directory="static"), name="static")


# --------------- Models ---------------
class ChatRequest(BaseModel):
    message: str


class ResumeRequest(BaseModel):
    conversation_id: str
    message: Optional[str] = "Please continue and complete the requested task using the MCP tools."


class ChatResponse(BaseModel):
    conversation_id: str
    response_id: str
    status: str  # "completed" | "consent_required"
    answer: Optional[str] = None
    consent_links: Optional[list[dict]] = None


# --------------- Helpers ---------------
def _handle_mcp_approval(response):
    """Auto-approve MCP tool calls if require_approval is set."""
    input_list = []
    for item in response.output:
        if getattr(item, "type", "") == "mcp_approval_request" and getattr(item, "id", None):
            input_list.append(
                McpApprovalResponse(
                    type="mcp_approval_response",
                    approve=True,
                    approval_request_id=item.id,
                )
            )
    if input_list:
        response = openai_client.responses.create(
            input=input_list,
            previous_response_id=response.id,
            extra_body={"agent_reference": {"name": agent.name, "type": "agent_reference"}},
            timeout=200,
        )
    return response


def _extract_consent_links(response):
    """Extract OAuth consent links from agent response."""
    links = []
    for item in response.output:
        if getattr(item, "type", "") == "oauth_consent_request":
            links.append({
                "server_label": getattr(item, "server_label", "unknown"),
                "consent_link": getattr(item, "consent_link", ""),
            })
    return links


# --------------- Routes ---------------
@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Start a new conversation with the agent. Returns consent links if OAuth is needed."""
    conversation = openai_client.conversations.create()

    response = openai_client.responses.create(
        conversation=conversation.id,
        input=req.message,
        extra_body={"agent_reference": {"name": agent.name, "type": "agent_reference"}},
        timeout=200,
    )

    # Check for OAuth consent requirement
    consent_links = _extract_consent_links(response)
    if consent_links:
        return ChatResponse(
            conversation_id=conversation.id,
            response_id=response.id,
            status="consent_required",
            consent_links=consent_links,
        )

    # Handle MCP approval if needed
    response = _handle_mcp_approval(response)

    return ChatResponse(
        conversation_id=conversation.id,
        response_id=response.id,
        status="completed",
        answer=response.output_text,
    )


@app.post("/api/chat/resume", response_model=ChatResponse)
async def resume_chat(req: ResumeRequest):
    """Resume conversation after user has completed OAuth consent in the browser."""
    response = openai_client.responses.create(
        conversation=req.conversation_id,
        input=req.message,
        extra_body={"agent_reference": {"name": agent.name, "type": "agent_reference"}},
        timeout=200,
    )

    # Check again in case multi-step consent is needed
    consent_links = _extract_consent_links(response)
    if consent_links:
        return ChatResponse(
            conversation_id=req.conversation_id,
            response_id=response.id,
            status="consent_required",
            consent_links=consent_links,
        )

    # Handle MCP approval if needed
    response = _handle_mcp_approval(response)

    return ChatResponse(
        conversation_id=req.conversation_id,
        response_id=response.id,
        status="completed",
        answer=response.output_text,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
