"""Command-line interface for the Code Impact Agent demo."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from code_impact.agent.graph import CodeImpactAgent
from code_impact.agent.llm import GeminiConfigurationError
from code_impact.demo_setup import create_demo_repository


def _setup_demo(args: argparse.Namespace) -> int:
    commits = create_demo_repository(args.output)
    print(f"Demo repository: {args.output.resolve()}")
    print(json.dumps(commits, indent=2))
    print("\nTry this question:")
    print("demo-safety-change 커밋의 영향 범위와 테스트를 알려줘.")
    return 0


def _analyze(args: argparse.Namespace) -> int:
    try:
        agent = CodeImpactAgent(args.repo, args.db)
    except GeminiConfigurationError as error:
        print(f"Gemini 설정 오류: {error}")
        return 1
    state = agent.run(args.question)

    print("Agent 실행 과정")
    for index, step in enumerate(state.get("trace", []), start=1):
        print(f"{index}. {step}")
    print()
    print(state["report"])
    return 1 if state.get("error") else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup-demo", help="Create the av-sim demo repository.")
    setup.add_argument("--output", type=Path, default=Path("demo/work/av-sim"))
    setup.set_defaults(handler=_setup_demo)

    analyze = subparsers.add_parser("analyze", help="Analyze one natural-language question.")
    analyze.add_argument("question")
    analyze.add_argument("--repo", type=Path, default=Path("demo/work/av-sim"))
    analyze.add_argument("--db", type=Path, default=Path("demo/data/code-impact.db"))
    analyze.set_defaults(handler=_analyze)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(args.handler(args))
