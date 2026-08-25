# Copyright (c) Microsoft. All rights reserved.
"""Progressive disclosure with two skills on a Foundry Prompt Agent.

``test-lazy-skill`` hides two values in its body that are absent from its
description, so a correct answer proves the body was fetched on demand rather
than preloaded. Every skill tool call is printed, showing which skills were
opened and which were left untouched.

The agent must already declare the skill tools -- publish them once with
``python provision_skill_tools.py``.

Env: FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_AGENT_NAME, FOUNDRY_AGENT_VERSION (optional).

Run ``az login``, then::

    python code_defined_skill_two_skills.py                    # interactive
    python code_defined_skill_two_skills.py "your question"    # one-shot
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


# Skill 1 — instructions, a static resource, a dynamic resource and a script.
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


@unit_converter_skill.resource(name="conversion-policy", description="Conversion formatting and rounding policy")
def conversion_policy(**kwargs: Any) -> str:
    """Return the current conversion policy."""
    return dedent("""\
        # Conversion Policy

        **Decimal places:** 4
        **Format:** Always show both the original and converted values with units
    """)


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


# Skill 2 — the description gives nothing away; the values live in the body.
lazy_skill = InlineSkill(
    frontmatter=SkillFrontmatter(
        name="test-lazy-skill", description="Provides internal reference information about the myISP platform."
    ),
    instructions=dedent("""\
        # Internal Reference

        The secret internal project codename is DELTA-7.
        The AMS SCA staging PIN is 4892.

        When asked for the codename, answer exactly: DELTA-7
        When asked for the PIN, answer exactly: 4892
    """),
)

SKILLS = [unit_converter_skill, lazy_skill]


class TracingSkillsProvider(SkillsProvider):
    """A ``SkillsProvider`` that prints every skill tool call."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.opened: set[str] = set()

    async def _load_skill(self, skills: Any, skill_name: str) -> str:
        print(f"  [skill] load_skill({skill_name})")
        self.opened.add(skill_name)
        return await super()._load_skill(skills, skill_name)

    async def _read_skill_resource(self, skills: Any, skill_name: str, resource_name: str, **kwargs: Any) -> Any:
        print(f"  [skill] read_skill_resource({skill_name} -> {resource_name})")
        return await super()._read_skill_resource(skills, skill_name, resource_name, **kwargs)

    async def _run_skill_script(self, skills: Any, skill_name: str, script_name: str, args: Any = None, **kwargs: Any) -> Any:
        print(f"  [skill] run_skill_script({skill_name} -> {script_name})")
        return await super()._run_skill_script(skills, skill_name, script_name, args, **kwargs)


async def main() -> None:
    """Answer prompts from the command line or an interactive loop."""
    prompts = sys.argv[1:]
    provider = TracingSkillsProvider(SKILLS, disable_load_skill_approval=True)

    # L1: only names and descriptions reach the system prompt; bodies do not.
    print(SkillsProvider._create_instructions(None, SKILLS))
    print("=" * 70)

    async with AzureCliCredential() as credential:
        async with FoundryAgent(
            project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
            agent_name=os.environ["FOUNDRY_AGENT_NAME"],
            agent_version=os.environ.get("FOUNDRY_AGENT_VERSION") or None,
            credential=credential,
            context_providers=[provider],
            middleware=[ToolApprovalMiddleware(auto_approval_rules=[SkillsProvider.all_tools_auto_approval_rule])],
        ) as agent:

            async def ask(prompt: str) -> None:
                """Send one prompt in a fresh session and report what was opened."""
                provider.opened = set()
                response = await agent.run(prompt, session=agent.create_session())
                untouched = [s.frontmatter.name for s in SKILLS if s.frontmatter.name not in provider.opened]
                print(f"Agent: {response}")
                print(f"  [skill] untouched: {untouched or 'none'}\n")

            for prompt in prompts:
                print(f"\nUser: {prompt}")
                await ask(prompt)
            if prompts:
                return

            print("Ask a question, or 'exit' to quit.")
            while True:
                prompt = (await asyncio.to_thread(input, "\nUser> ")).strip()
                if prompt.lower() in {"exit", "quit"}:
                    return
                if prompt:
                    await ask(prompt)


if __name__ == "__main__":
    asyncio.run(main())
