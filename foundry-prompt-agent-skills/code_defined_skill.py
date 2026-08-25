# Copyright (c) Microsoft. All rights reserved.
"""Code-defined Agent Skills on a Foundry Prompt Agent, self-contained.

Based on the upstream sample, but targeting a Prompt Agent via ``FoundryAgent``
instead of a raw model deployment via ``FoundryChatClient``. Because a Prompt
Agent can only call tools stored in its own definition, this script publishes
the three skill tools itself when the agent's latest version lacks them -- so
no separate provisioning step is needed.

Skills are defined three ways: a static resource, a dynamic resource
(``@skill.resource``) and a script (``@skill.script``).

Env: FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_AGENT_NAME,
FOUNDRY_AGENT_VERSION (optional, pins a version), FOUNDRY_MODEL_NAME
(only used if a version has to be created).

Run ``az login``, then::

    python code_defined_skill.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from textwrap import dedent
from typing import Any

from agent_framework import (
    InlineSkill,
    InlineSkillResource,
    SkillFrontmatter,
    SkillsProvider,
    ToolApprovalMiddleware,
)
from agent_framework.foundry import FoundryAgent
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import FunctionTool, PromptAgentDefinition
from azure.core.exceptions import ResourceNotFoundError
from azure.identity.aio import AzureCliCredential
from dotenv import load_dotenv

load_dotenv()

# The skills provider registers its tools client-side, and FoundryAgent strips
# them because a Prompt Agent only calls tools stored in its own definition.
# That is exactly the intended setup here, so drop the per-request warning.
logging.getLogger("agent_framework.foundry").addFilter(
    lambda record: "tool declarations cannot be sent" not in record.getMessage()
)

# Replies may contain characters the default Windows console cannot encode.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

INSTRUCTIONS = "You are a helpful assistant that can convert units."


# 1. Static resource — inline content passed at construction time.
unit_converter_skill = InlineSkill(
    frontmatter=SkillFrontmatter(
        name="unit-converter", description="Convert between common units using a conversion factor"
    ),
    instructions=dedent("""\
        Use this skill when the user asks to convert between units.

        1. Review the conversion-tables resource to find the factor for the
           requested conversion.
        2. Check the conversion-policy resource for rounding and formatting rules.
        3. Use the convert script, passing the value and factor from the table.
    """),
    resources=[
        InlineSkillResource(
            name="conversion-tables",
            content=dedent("""\
                # Conversion Tables

                Formula: **result = value × factor**

                | From        | To          | Factor   |
                |-------------|-------------|----------|
                | miles       | kilometers  | 1.60934  |
                | kilometers  | miles       | 0.621371 |
                | pounds      | kilograms   | 0.453592 |
                | kilograms   | pounds      | 2.20462  |
            """),
        ),
    ],
)


# 2. Dynamic resource — generated when the model reads it.
@unit_converter_skill.resource(name="conversion-policy", description="Conversion formatting and rounding policy")
def conversion_policy(**kwargs: Any) -> str:
    """Return the current conversion policy."""
    return dedent("""\
        # Conversion Policy

        **Decimal places:** 4
        **Format:** Always show both the original and converted values with units
    """)


# 3. Script — an in-process callable the model can run.
@unit_converter_skill.script(name="convert", description="Convert a value: result = value × factor")
def convert_units(value: float, factor: float, **kwargs: Any) -> str:
    """Convert a value: result = value x factor.

    Args:
        value: The numeric value to convert.
        factor: Conversion factor from the conversion-tables resource.
        **kwargs: Unused runtime keyword arguments.

    Returns:
        JSON string with the inputs and the converted result.
    """
    return json.dumps({"value": value, "factor": factor, "result": round(value * factor, 4)})


async def resolve_version(client: AIProjectClient, agent_name: str, model: str) -> str:
    """Return an agent version that declares the three skill tools.

    Reuses the latest version when it already declares them, otherwise
    publishes a new one. The schemas come from ``SkillsProvider`` itself and are
    the same regardless of which skills exist.

    Args:
        client: Async ``AIProjectClient`` for the Foundry project.
        agent_name: Name of the Foundry Prompt Agent.
        model: Model deployment used if a version has to be created.

    Returns:
        The version to pass to ``FoundryAgent(agent_version=...)``.
    """
    tools = [
        FunctionTool(name=t.name, description=t.description or "", parameters=t.parameters(), strict=False)
        for t in SkillsProvider._create_tools(SkillsProvider([]), [])
    ]
    expected = {t.name for t in tools}

    try:
        agent = await client.agents.get(agent_name=agent_name)
        version = agent.versions.latest.version
        latest = await client.agents.get_version(agent_name=agent_name, agent_version=version)
        declared = {getattr(t, "name", None) for t in (latest.definition.get("tools") or [])}
        if expected.issubset(declared):
            return version
        model = latest.definition.get("model") or model
    except ResourceNotFoundError:
        print(f"Agent '{agent_name}' not found - creating it.")

    created = await client.agents.create_version(
        agent_name=agent_name,
        definition=PromptAgentDefinition(model=model, instructions=INSTRUCTIONS, tools=tools),
    )
    print(f"Created agent '{agent_name}' version {created.version}.")
    return created.version


async def main() -> None:
    """Ask the agent a conversion question using the skill."""
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    agent_name = os.environ["FOUNDRY_AGENT_NAME"]

    async with AzureCliCredential() as credential:
        async with AIProjectClient(endpoint=endpoint, credential=credential) as client:
            version = os.environ.get("FOUNDRY_AGENT_VERSION") or await resolve_version(
                client, agent_name, os.environ.get("FOUNDRY_MODEL_NAME", "gpt-4o-mini")
            )

            # FoundryAgent *is* the agent, so the provider and middleware attach
            # to it directly. Instructions come from the stored agent version;
            # passing them again would duplicate the developer message.
            async with FoundryAgent(
                project_client=client,
                agent_name=agent_name,
                agent_version=version,
                context_providers=[SkillsProvider(unit_converter_skill)],
                middleware=[ToolApprovalMiddleware(auto_approval_rules=[SkillsProvider.all_tools_auto_approval_rule])],
            ) as agent:
                print(f"Connected to '{agent_name}' version {version}.")
                response = await agent.run(
                    "How many kilometers is a marathon (26.2 miles)? And how many pounds is 75 kilograms?",
                    session=agent.create_session(),
                )
                print(f"Agent: {response}")


if __name__ == "__main__":
    asyncio.run(main())
