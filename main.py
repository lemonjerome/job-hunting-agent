"""
Job Hunting Agent — Entry point.

Phase 1: LLM smoke-test.
  Verifies the Ollama Cloud connection and that minimax-m2.7 responds.

Phase 7+: Full LangGraph graph will be invoked here.

Usage:
    python main.py
"""

import asyncio
from pathlib import Path

from config import get_llm
from langchain_core.messages import HumanMessage

RESUME_PATH = Path(__file__).parent / "resume.md"


async def smoke_test_llm() -> None:
    """Phase 1: confirm Ollama Cloud + minimax-m2.7 is reachable."""
    print("=== LLM Smoke Test ===")
    llm = get_llm()

    if RESUME_PATH.exists():
        resume_text = RESUME_PATH.read_text(encoding="utf-8")[:2000]  # first 2k chars
        prompt = (
            "You are reviewing a resume. In 3 bullet points, summarise "
            "the candidate's top skills based on this resume:\n\n" + resume_text
        )
    else:
        prompt = "Reply with exactly: 'Ollama Cloud connection successful.'"

    print(f"Sending prompt to {llm.model} at {llm.base_url} ...")
    response = await llm.ainvoke([HumanMessage(content=prompt)])
    print("\n--- LLM Response ---")
    print(response.content)
    print("--- End ---\n")


async def main() -> None:
    await smoke_test_llm()

    # Phase 7: uncomment once all agents are implemented
    # from graph.graph import graph
    # result = await graph.ainvoke({})
    # print(result)


if __name__ == "__main__":
    asyncio.run(main())
