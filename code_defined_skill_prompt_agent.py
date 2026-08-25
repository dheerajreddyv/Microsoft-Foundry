# Copyright (c) Microsoft. All rights reserved.
"""Code-defined Agent Skills on a Foundry Prompt Agent.

Shows the three ways to define a skill in code: static resources, a dynamic
resource (``@skill.resource``) and a script (``@skill.script``).

The agent must already declare the skill tools -- publish them once with
``python provision_skill_tools.py``.

Env: FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_AGENT_NAME, FOUNDRY_AGENT_VERSION (optional).

Run ``az login``, then::

    python code_defined_skill_prompt_agent.py
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


async def main() -> None:
    """Ask the agent a conversion question using the skill."""
    async with AzureCliCredential() as credential:
        # FoundryAgent *is* the agent, so the provider and middleware attach to
        # it directly. Instructions come from the stored agent version.
        async with FoundryAgent(
            project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
            agent_name=os.environ["FOUNDRY_AGENT_NAME"],
            agent_version=os.environ.get("FOUNDRY_AGENT_VERSION") or None,
            credential=credential,
            context_providers=[SkillsProvider(unit_converter_skill)],
            middleware=[ToolApprovalMiddleware(auto_approval_rules=[SkillsProvider.all_tools_auto_approval_rule])],
        ) as agent:
            response = await agent.run(
                "How many kilometers is a marathon (26.2 miles)? And how many pounds is 75 kilograms?",
                session=agent.create_session(),
            )
            print(f"Agent: {response}")


if __name__ == "__main__":
    asyncio.run(main())
