"""The Docker image copies modules by name; a module missing from the list only
shows up as an ImportError inside the container, long after the tests passed."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def copied_modules() -> set:
    text = (ROOT / "Dockerfile").read_text()
    line = next(l for l in text.splitlines() if re.match(r"COPY .*\.py", l))
    return {name for name in line.split()[1:-1] if name.endswith(".py")}


def local_imports(path: Path) -> set:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return {n for n in names if (ROOT / f"{n}.py").is_file()}


def test_every_top_level_module_is_copied_into_the_image():
    on_disk = {p.name for p in ROOT.glob("*.py")}
    missing = on_disk - copied_modules()
    assert not missing, f"add to the Dockerfile COPY line: {sorted(missing)}"


def test_every_module_the_app_imports_is_copied():
    """Follows imports from the entry points, so a stray helper file cannot hide the problem."""
    seen, todo = set(), ["app.py", "cli.py"]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo += [f"{m}.py" for m in local_imports(ROOT / name)]
    assert seen - {"datalabs_paths.py"} <= copied_modules(), sorted(seen - copied_modules())


def test_readme_names_the_bitbucket_scope_for_every_cloud_collector():
    """Adding a collector without saying which scope it needs would repeat the
    gap where the README listed GitHub and GitLab tokens but not Bitbucket's."""
    import bitbucket  # noqa: F401  (registers the collectors)
    import supplementary

    text = (ROOT / "README.md").read_text()
    section = text[text.index("### Bitbucket tokens"): text.index("Which kind of token to paste")]
    missing = [n for n in supplementary.REGISTRY["bitbucket"] if f"`{n}`" not in section]
    assert not missing, f"add these to the Bitbucket scope table in the README: {missing}"
    for scope in ("read:repository:bitbucket", "read:pullrequest:bitbucket", "read:pipeline:bitbucket",
                  "admin:repository:bitbucket", "read:webhook:bitbucket", "read:snippet:bitbucket"):
        assert scope in text.split("## Step 1: the token")[1].split("### Bitbucket tokens")[0]


def test_git_is_installed_in_the_image_for_wiki_clones_and_for_check():
    assert re.search(r"apt-get install[^\n]*\bgit\b", (ROOT / "Dockerfile").read_text().replace("\\\n", " "))
