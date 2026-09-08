"""Stream a multi-agent Workflow, showing what each step is doing.

Self-contained: run this file on its own.

The chain is: researcher (calls the weather tool) -> stylist (writes the
advice). Both are reasoning agents, so you see two "Thinking..." blocks.

Key point: an `AgentExecutor` in streaming mode forwards every
`AgentResponseUpdate` it receives into the workflow event stream, so rendering
is per-content exactly as it would be for a single agent. The workflow only adds
an outer envelope -- `WorkflowEvent` -- telling you which executor the update
came from.

Setup:
    pip install -r requirements.txt
    copy .env.example .env   (then edit it)
    az login
    python workflow_stream.py
"""

import asyncio
import os
import sys
import time
from typing import Annotated

from agent_framework import (
    Agent,
    AgentExecutorRequest,
    AgentExecutorResponse,
    AgentResponseUpdate,
    Message,
    WorkflowBuilder,
    WorkflowContext,
    executor,
)
from agent_framework.foundry import FoundryChatClient
from azure.identity.aio import AzureCliCredential
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles mangle degree signs etc.

DIM = "\033[2m"
RESET = "\033[0m"

REASONING = {"reasoning": {"effort": "high", "summary": "detailed"}}


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


@executor(id="handoff")
async def handoff(
    result: AgentExecutorResponse, ctx: WorkflowContext[AgentExecutorRequest]
) -> None:
    """Turn the researcher's answer into a fresh task for the stylist.

    Chaining two agents directly forwards `full_conversation`, so the next agent
    sees the previous answer as the last *assistant* message and often just
    continues or repeats it instead of doing its own work. Restating the result
    as a *user* message gives the stylist a real question to reason about.
    """
    await ctx.send_message(
        AgentExecutorRequest(
            messages=[
                Message(
                    "user",
                    f"Weather report: {result.agent_response.text}\n"
                    "What should I wear? Explain your choices.",
                )
            ]
        )
    )


async def main() -> None:
    async with AzureCliCredential() as credential:
        client = FoundryChatClient(
            project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
            model=os.environ["FOUNDRY_MODEL"],
            credential=credential,
        )

        researcher = Agent(
            name="researcher",
            client=client,
            instructions="Look up the weather for the city the user asks about. Report the conditions only.",
            tools=[get_weather],
            default_options=REASONING,
        )
        stylist = Agent(
            name="stylist",
            client=client,
            instructions="Recommend what to wear for the given weather, and say why.",
            default_options=REASONING,
        )

        # `intermediate_output_from` is what makes the researcher's updates
        # visible. Without it only the final executor is streamed, and the
        # first agent's thinking and tool call are silently swallowed.
        workflow = (
            WorkflowBuilder(
                start_executor=researcher,
                output_from=[stylist],
                intermediate_output_from=[researcher],
            )
            .add_edge(researcher, handoff)
            .add_edge(handoff, stylist)
            .build()
        )

        printer = Printer()
        current: str | None = None

        async for event in workflow.run("What should I wear in Amsterdam today?", stream=True):
            # Agent activity arrives as "intermediate" and "output" events whose
            # data is the same AgentResponseUpdate a bare agent would yield.
            if not isinstance(event.data, AgentResponseUpdate):
                continue

            if event.executor_id != current:
                # Close the previous step cleanly, then start a fresh renderer.
                if current is not None:
                    printer.finish()
                    printer = Printer()
                current = event.executor_id
                print(f"\n{DIM}=== {current} ==={RESET}", flush=True)

            printer.handle(event.data)

        printer.finish()


if __name__ == "__main__":
    asyncio.run(main())
