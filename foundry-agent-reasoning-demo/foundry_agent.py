"""Stream reasoning and tool activity from a stored Foundry Agent Service agent.

Self-contained: run this file on its own.

`stream_agent.py` builds the agent in-process with `FoundryChatClient`: the model,
instructions, tools and reasoning options all live in this Python code.

`FoundryAgent` instead connects to an agent that is **stored in the Foundry
project**. The definition lives on the server, so it has to be created there
first -- including the tool schema, because `FoundryAgent` cannot send tool
declarations at call time. Python still executes the tool locally: the service
only asks for the call, this process runs the function and returns the result.

The streaming contract is identical either way.

Setup:
    pip install -r requirements.txt
    copy .env.example .env   (then edit it)
    az login
    python foundry_agent.py
"""

import asyncio
import logging
import os
import sys
import time
from typing import Annotated

from agent_framework import AgentResponseUpdate, tool
from agent_framework.foundry import FoundryAgent
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import FunctionTool, PromptAgentDefinition, Reasoning
from azure.identity.aio import AzureCliCredential
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles mangle degree signs etc.

DIM = "\033[2m"
RESET = "\033[0m"

AGENT_NAME = os.getenv("FOUNDRY_AGENT_NAME", "reasoning-demo-agent")
INSTRUCTIONS = "You are a helpful assistant. Use tools when needed."

# Expected here: the framework warns that it cannot send tool declarations for a
# stored agent. That is fine -- the schema is published in the definition below.
logging.getLogger("agent_framework.foundry").setLevel(logging.ERROR)


class Printer:
    """Prints one streamed chunk at a time, split by content type.

    `update.contents` is a list of `agent_framework.Content` objects, and `.type`
    says which kind each one is. That discriminator is what drives the UI:
    everything except "text" is the agent working, not the answer.
    """

    def __init__(self) -> None:
        self.label: str | None = None
        self.tool_args: dict[str, str] = {}
        self.thinking_started: float | None = None
        self.thinking_shown = False

    def _label(self, label: str) -> None:
        """Print a label once per phase; chunks within a phase just append."""
        if self.label != label:
            self._end_thinking()
            print(f"\n{label}", end="", flush=True)
            self.label = label

    def _end_thinking(self) -> None:
        """Close the thinking block with its duration, like 'Thought for 2s'."""
        if self.thinking_started is None:
            return
        elapsed = time.monotonic() - self.thinking_started
        if self.thinking_shown:
            print(f"\n{DIM}  Thought for {elapsed:.0f}s{RESET}", flush=True)
        else:
            # The model reasoned but returned no readable summary for this turn.
            # Say so explicitly, otherwise it looks like the code dropped it.
            print(f"\n{DIM}Thought for {elapsed:.0f}s (no summary returned){RESET}", flush=True)
        self.thinking_started = None
        self.thinking_shown = False

    def handle(self, update: AgentResponseUpdate) -> None:
        for content in update.contents:
            match content.type:
                case "text_reasoning":
                    # Timing starts on the empty encrypted placeholder, because
                    # that already proves the model is reasoning. The header is
                    # only printed once actual summary text shows up.
                    if self.thinking_started is None:
                        self.thinking_started = time.monotonic()
                    if content.text:
                        if not self.thinking_shown:
                            self.thinking_shown = True
                            self.label = "thinking"
                            print(f"\n{DIM}Thinking...{RESET}", flush=True)
                        print(f"{DIM}{content.text}{RESET}", end="", flush=True)

                case "function_call":
                    # A call spans several chunks sharing one call_id; the name
                    # repeats and arguments arrive as partial JSON fragments.
                    if content.call_id not in self.tool_args:
                        self.tool_args[content.call_id] = ""
                        self._label(f"[tool] calling {content.name}")
                    if content.arguments:
                        self.tool_args[content.call_id] += str(content.arguments)

                case "function_result":
                    args = self.tool_args.pop(content.call_id, "")
                    self._label(f"[tool] {args} -> {content.result}")

                case "text":
                    self._label("[answer] ")
                    print(content.text, end="", flush=True)

    def finish(self) -> None:
        self._end_thinking()
        print()


@tool
def get_weather(location: Annotated[str, "City name, e.g. 'Amsterdam'"]) -> str:
    """Get the current weather for a given city."""
    return f"It is 18 degrees C, cloudy with light rain in {location}."


async def ensure_agent(endpoint: str, credential: AzureCliCredential) -> str:
    """Publish the agent definition and return the version to run.

    Everything the model needs is stored here, server-side: the deployment, the
    instructions, the tool schema, and the reasoning settings. Reasoning is what
    produces the "thinking" chunks, so it must be configured on this definition
    -- passing it at call time has no effect for a stored agent.

    Each run publishes a new version, which keeps the stored definition in sync
    with this file.
    """
    async with AIProjectClient(endpoint=endpoint, credential=credential) as project:
        version = await project.agents.create_version(
            agent_name=AGENT_NAME,
            definition=PromptAgentDefinition(
                model=os.environ["FOUNDRY_MODEL"],
                instructions=INSTRUCTIONS,
                reasoning=Reasoning(effort="high", summary="detailed"),
                tools=[
                    FunctionTool(
                        name=get_weather.name,
                        description=get_weather.description,
                        parameters=get_weather.parameters(),
                        strict=False,
                    )
                ],
            ),
        )
        print(f"published agent {AGENT_NAME} v{version.version}")
        return version.version


async def main() -> None:
    # Must be the project-scoped endpoint (.../api/projects/<project>): the
    # Agents API lives under the project, not the account root.
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]

    async with AzureCliCredential() as credential:
        version = await ensure_agent(endpoint, credential)

        agent = FoundryAgent(
            project_endpoint=endpoint,
            agent_name=AGENT_NAME,
            agent_version=version,
            credential=credential,
            # Declared server-side above; passed here only so the framework can
            # run the Python function when the service requests the call.
            tools=[get_weather],
        )

        printer = Printer()
        async for update in agent.run(
            "What should I wear in Amsterdam today?", stream=True
        ):
            printer.handle(update)
        printer.finish()


if __name__ == "__main__":
    asyncio.run(main())
