# Copyright (c) Microsoft. All rights reserved.
"""One-time setup: declare the three skill tools on a Foundry Prompt Agent.

A Prompt Agent can only call tools stored in its own definition -- the service
rejects requests carrying both an ``agent_reference`` and ``tools``. The tools
are generic dispatchers keyed by ``skill_name``, so run this once per agent,
not once per skill. Only schemas are published; skill content stays local.

Env: FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_AGENT_NAME, FOUNDRY_MODEL_NAME.

Run ``az login``, then::

    python provision_skill_tools.py
"""

from __future__ import annotations

import asyncio
import os

from agent_framework import SkillsProvider
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import FunctionTool, PromptAgentDefinition
from azure.core.exceptions import ResourceNotFoundError
from azure.identity.aio import AzureCliCredential
from dotenv import load_dotenv

load_dotenv()

INSTRUCTIONS = "You are a helpful assistant that can convert units."


def build_skill_tools() -> list[FunctionTool]:
    """Return Foundry declarations for the three skill tools.

    Schemas come from ``SkillsProvider`` itself, so they cannot drift from what
    the client dispatches. They do not depend on which skills exist, hence the
    empty list.
    """
    provider = SkillsProvider([])
    return [
        FunctionTool(name=t.name, description=t.description or "", parameters=t.parameters(), strict=False)
        for t in SkillsProvider._create_tools(provider, [])
    ]


async def main() -> None:
    """Ensure the agent's latest version declares the skill tools."""
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    agent_name = os.environ["FOUNDRY_AGENT_NAME"]
    model = os.environ.get("FOUNDRY_MODEL_NAME", "gpt-4o-mini")

    tools = build_skill_tools()
    expected = {t.name for t in tools}

    async with AzureCliCredential() as credential:
        async with AIProjectClient(endpoint=endpoint, credential=credential) as client:
            try:
                agent = await client.agents.get(agent_name=agent_name)
                version = agent.versions.latest.version
                latest = await client.agents.get_version(agent_name=agent_name, agent_version=version)
                declared = {getattr(t, "name", None) for t in (latest.definition.get("tools") or [])}
                if expected.issubset(declared):
                    print(f"Agent '{agent_name}' version {version} already declares the skill tools.")
                    print(f"FOUNDRY_AGENT_VERSION={version}")
                    return
                model = latest.definition.get("model") or model
            except ResourceNotFoundError:
                print(f"Agent '{agent_name}' not found - creating it.")

            created = await client.agents.create_version(
                agent_name=agent_name,
                definition=PromptAgentDefinition(model=model, instructions=INSTRUCTIONS, tools=tools),
            )

    print(f"Created agent '{agent_name}' version {created.version}.")
    print(f"FOUNDRY_AGENT_VERSION={created.version}")


if __name__ == "__main__":
    asyncio.run(main())
