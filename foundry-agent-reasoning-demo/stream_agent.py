"""Stream a Foundry agent and show what it is doing before the final answer.

Setup:
    pip install -r requirements.txt
    copy .env.example .env   (then edit it)
    az login
    python stream_agent.py
"""

import asyncio
import os
import sys
import time
from typing import Annotated

from agent_framework import Agent, AgentResponseUpdate
from agent_framework.foundry import FoundryChatClient
from azure.identity.aio import AzureCliCredential
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles mangle degree signs etc.

DIM = "\033[2m"
RESET = "\033[0m"


def get_weather(location: Annotated[str, "City name, e.g. 'Amsterdam'"]) -> str:
    """Get the current weather for a given city."""
    return f"It is 18 degrees C, cloudy with light rain in {location}."


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


async def main() -> None:
    async with AzureCliCredential() as credential:
        agent = Agent(
            client=FoundryChatClient(
                project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
                model=os.environ["FOUNDRY_MODEL"],
                credential=credential,
            ),
            instructions="You are a helpful assistant. Use tools when needed.",
            tools=[get_weather],
            # Needed to receive "text_reasoning"; reasoning models only.
            # The model only emits a summary when it actually reasons -- trivial
            # prompts may produce none at all, whatever the effort setting.
            default_options={"reasoning": {"effort": "high", "summary": "detailed"}},
        )

        printer = Printer()
        async for update in agent.run("What should I wear in Amsterdam today and also what are all the places should i visit?", stream=True):
            printer.handle(update)
        printer.finish()


if __name__ == "__main__":
    asyncio.run(main())
