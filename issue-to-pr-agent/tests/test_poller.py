from __future__ import annotations

import asyncio

from issue_to_pr_agent.models import IssueTask
from issue_to_pr_agent.poller import IssuePoller


def test_poller_submits_only_new_issue_revisions() -> None:
    issues = [
        IssueTask(
            delivery_id=f"poll-{index}",
            repository="owner/repository",
            number=index,
            title="Fix",
            body="Expected behavior",
            author="octocat",
            author_association="OWNER",
        )
        for index in (1, 2)
    ]

    class GitHub:
        @staticmethod
        def list_candidate_issues() -> list[IssueTask]:
            return issues

    class Worker:
        async def submit(self, issue: IssueTask) -> bool:
            return issue.number == 1

    poller = IssuePoller(GitHub(), Worker(), 60)  # type: ignore[arg-type]

    assert asyncio.run(poller.scan_once()) == 1
