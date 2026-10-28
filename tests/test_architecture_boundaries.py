"""Guard the package boundaries documented by the architecture baseline."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[1] / "src" / "mailforge"
LAYERS = ("domain", "application", "infrastructure", "interfaces")
type ImportGraph = dict[str, set[str]]


def _module_name(path: Path) -> str:
    module = "mailforge." + path.relative_to(PACKAGE_ROOT).with_suffix("").as_posix().replace(
        "/", "."
    )
    return module.removesuffix(".__init__")


def _resolve_from_import(module_name: str, node: ast.ImportFrom, *, is_package: bool) -> set[str]:
    if node.level == 0:
        base = node.module or ""
    else:
        package_name = module_name if is_package else module_name.rsplit(".", 1)[0]
        package_parts = package_name.split(".")
        anchor = package_parts[: len(package_parts) - node.level + 1]
        base = ".".join(anchor)
        if node.module:
            base = f"{base}.{node.module}"

    imports = {base} if base else set()
    for alias in node.names:
        if alias.name != "*" and base:
            imports.add(f"{base}.{alias.name}")
    return imports


def _imports_from_tree(tree: ast.AST, module_name: str, *, is_package: bool = False) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(_resolve_from_import(module_name, node, is_package=is_package))
    return imported


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return _imports_from_tree(tree, _module_name(path), is_package=path.name == "__init__.py")


def _assert_no_cycles(graph: ImportGraph) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str) -> None:
        if module in visiting:
            raise AssertionError(f"import cycle detected at {module}")
        if module in visited:
            return
        visiting.add(module)
        for dependency in graph[module]:
            visit(dependency)
        visiting.remove(module)
        visited.add(module)

    for module in graph:
        visit(module)


def _package_layer(path: Path) -> str | None:
    relative = path.relative_to(PACKAGE_ROOT)
    return relative.parts[0] if relative.parts[0] in LAYERS else None


def test_modern_package_does_not_import_legacy_areas() -> None:
    legacy_names = {"MAILER", "MSORTER", "SORTER"}

    for path in PACKAGE_ROOT.rglob("*.py"):
        imported = _imports(path)
        assert not any(
            imported_name == legacy_name or imported_name.startswith(f"{legacy_name}.")
            for imported_name in imported
            for legacy_name in legacy_names
        ), path


def test_layer_dependencies_only_point_inward_or_sideways() -> None:
    allowed = {
        "domain": {"domain"},
        "application": {"domain", "application", "infrastructure"},
        "infrastructure": {"domain", "infrastructure"},
        "interfaces": set(LAYERS),
    }

    for path in PACKAGE_ROOT.rglob("*.py"):
        layer = _package_layer(path)
        if layer is None:
            continue
        imported_layers = {
            imported.removeprefix("mailforge.").split(".", 1)[0]
            for imported in _imports(path)
            if imported.startswith("mailforge.")
        }
        assert imported_layers <= allowed[layer], (path, imported_layers - allowed[layer])


def test_package_has_no_import_cycles() -> None:
    modules = {_module_name(path) for path in PACKAGE_ROOT.rglob("*.py")}
    graph: ImportGraph = {module: set() for module in modules}
    for path in PACKAGE_ROOT.rglob("*.py"):
        module = _module_name(path)
        for imported in _imports(path):
            if imported in modules and imported != module:
                graph[module].add(imported)

    _assert_no_cycles(graph)


def test_import_parser_handles_aliases_packages_and_relative_modules() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "import MAILER.main as legacy",
                "from mailforge import interfaces",
                "from mailforge.application import campaigns",
                "from ..interfaces import cli",
            )
        )
    )

    imported = _imports_from_tree(tree, "mailforge.application.worker")

    assert "MAILER.main" in imported
    assert "mailforge.interfaces" in imported
    assert "mailforge.application.campaigns" in imported
    assert "mailforge.interfaces.cli" in imported

    package_imports = _imports_from_tree(
        ast.parse("from .interfaces import cli"), "mailforge", is_package=True
    )
    assert "mailforge.interfaces.cli" in package_imports


def test_cycle_guard_rejects_a_real_cycle() -> None:
    graph: ImportGraph = {"a": {"b"}, "b": {"a"}}

    try:
        _assert_no_cycles(graph)
    except AssertionError as error:
        assert "import cycle detected" in str(error)
    else:
        raise AssertionError("cycle guard accepted a two-module cycle")


def test_cycle_guard_ignores_package_reexport_self_edge() -> None:
    module = "mailforge.application"
    graph: ImportGraph = {
        module: set(),
        "mailforge.application.campaigns": set(),
    }
    imported = _imports_from_tree(ast.parse("from . import campaigns"), module, is_package=True)
    graph[module] = {
        dependency for dependency in imported if dependency in graph and dependency != module
    }

    _assert_no_cycles(graph)
    assert graph[module] == {"mailforge.application.campaigns"}
