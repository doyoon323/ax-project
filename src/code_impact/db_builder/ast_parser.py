"""Extract top-level Python functions and their direct call relationships."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from code_impact.db_builder.git_reader import GitRepository
from code_impact.db_builder.records import CallRecord, FunctionRecord


@dataclass(frozen=True)
class _CallReference:
    local_name: str
    module_alias: str | None
    line: int


@dataclass
class _ParsedModule:
    functions: list[FunctionRecord]
    calls_by_function: dict[str, list[_CallReference]]
    imported_symbols: dict[str, str]
    imported_modules: dict[str, str]


class _CallCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.calls: list[_CallReference] = []

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            self.calls.append(
                _CallReference(
                    local_name=node.func.id,
                    module_alias=None,
                    line=node.lineno,
                )
            )
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            self.calls.append(
                _CallReference(
                    local_name=node.func.attr,
                    module_alias=node.func.value.id,
                    line=node.lineno,
                )
            )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Nested functions are outside the MVP scope.
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return


def _module_name(file_path: str) -> str:
    parts = file_path.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _parse_module(file_path: str, source: str) -> _ParsedModule:
    tree = ast.parse(source, filename=file_path)
    module_name = _module_name(file_path)
    imported_symbols: dict[str, str] = {}
    imported_modules: dict[str, str] = {}

    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local_name = alias.asname or alias.name
                imported_symbols[local_name] = f"{node.module}::{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".")[0]
                imported_modules[local_name] = alias.name

    functions: list[FunctionRecord] = []
    calls_by_function: dict[str, list[_CallReference]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        function_id = f"{module_name}::{node.name}"
        record = FunctionRecord(
            function_id=function_id,
            module_name=module_name,
            function_name=node.name,
            file_path=file_path,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            is_test=file_path.startswith("tests/") or node.name.startswith("test_"),
        )
        functions.append(record)

        collector = _CallCollector()
        for statement in node.body:
            collector.visit(statement)
        calls_by_function[function_id] = collector.calls

    return _ParsedModule(
        functions=functions,
        calls_by_function=calls_by_function,
        imported_symbols=imported_symbols,
        imported_modules=imported_modules,
    )


def index_python_commit(
    repository: GitRepository,
    commit: str,
) -> tuple[list[FunctionRecord], list[CallRecord]]:
    """Parse a commit snapshot without checking it out."""
    parsed_modules: dict[str, _ParsedModule] = {}
    all_functions: list[FunctionRecord] = []

    for file_path in repository.list_python_files(commit):
        parsed = _parse_module(file_path, repository.show_file(commit, file_path))
        parsed_modules[file_path] = parsed
        all_functions.extend(parsed.functions)

    known_function_ids = {function.function_id for function in all_functions}
    calls: list[CallRecord] = []

    for parsed in parsed_modules.values():
        local_module = parsed.functions[0].module_name if parsed.functions else ""
        for caller_id, references in parsed.calls_by_function.items():
            for reference in references:
                if reference.module_alias:
                    imported_module = parsed.imported_modules.get(reference.module_alias)
                    if not imported_module:
                        continue
                    callee_id = f"{imported_module}::{reference.local_name}"
                elif reference.local_name in parsed.imported_symbols:
                    callee_id = parsed.imported_symbols[reference.local_name]
                else:
                    callee_id = f"{local_module}::{reference.local_name}"

                if callee_id not in known_function_ids:
                    continue
                caller = next(item for item in all_functions if item.function_id == caller_id)
                calls.append(
                    CallRecord(
                        caller_id=caller_id,
                        callee_id=callee_id,
                        file_path=caller.file_path,
                        line=reference.line,
                    )
                )

    return all_functions, calls
