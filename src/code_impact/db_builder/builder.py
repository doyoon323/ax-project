"""Build the SQLite index for one commit."""

from __future__ import annotations

from code_impact.db_builder.ast_parser import index_python_commit
from code_impact.db_builder.git_reader import GitRepository
from code_impact.db_builder.records import ChangedFunction
from code_impact.db_builder.sqlite_db import AnalysisDatabase


def build_commit_index(
    repository: GitRepository,
    database: AnalysisDatabase,
    ref: str,
) -> str:
    commit = repository.resolve_ref(ref)
    functions, calls = index_python_commit(repository, commit)
    changed_ranges = repository.changed_python_ranges(commit)

    changed_functions: list[ChangedFunction] = []
    for function in functions:
        if function.is_test:
            continue
        matching_lines = [
            changed_range.start_line
            for changed_range in changed_ranges
            if changed_range.file_path == function.file_path
            and changed_range.start_line <= function.end_line
            and changed_range.end_line >= function.start_line
        ]
        if matching_lines:
            changed_functions.append(
                ChangedFunction(
                    function=function,
                    changed_line=min(matching_lines),
                )
            )

    database.replace_index(commit, functions, calls, changed_functions)
    return commit
