"""Invoke a hosted Foundry agent via the Microsoft Agent Framework."""

import asyncio
import os

from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential as SyncCred
from azure.identity.aio import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.core.exceptions import ResourceNotFoundError

from agent_framework.foundry import FoundryAgent

load_dotenv()


def latest_version(project_endpoint: str, agent_name: str) -> str:
    with SyncCred() as cred, AIProjectClient(endpoint=project_endpoint, credential=cred) as p:
        try:
            return str(p.agents.get(agent_name)["versions"]["latest"]["version"])
        except ResourceNotFoundError:
            available = sorted(a.name for a in p.agents.list())
            raise SystemExit(
                f"Agent '{agent_name}' not found. Available:\n  - " + "\n  - ".join(available)
            )


async def main() -> None:
    project_endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    agent_name = os.environ["NEW_AGENT_NAME"]
    version = os.environ.get("FOUNDRY_AGENT_VERSION") or latest_version(project_endpoint, agent_name)
    prompt = os.environ.get("USER_PROMPT", "Multiply 9 and 4")

    async with DefaultAzureCredential() as cred:
        agent = FoundryAgent(
            project_endpoint=project_endpoint,
            agent_name=agent_name,
            agent_version=version,
            credential=cred,
        )
        print(f"\n[Agent] {agent_name} v{version}\nUser: {prompt}\n")
        result = await agent.run(messages=prompt, stream=False)
        print(f"Agent: {result.text}\n")


if __name__ == "__main__":
    asyncio.run(main())
