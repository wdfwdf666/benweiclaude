"""Stage coding subagent edits in isolated copies and merge them deliberately."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4

from .subagents import SubagentRunner
from .tools_util import clipped_text

SKIP_COPY = {
    ".git", ".codepilot", ".venv", ".uv-cache", "node_modules", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".next", "dist", "build",
}
MAX_FILES = 2000
MAX_COPY_BYTES = 40_000_000
MAX_EDIT_BYTES = 2_000_000


def _digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _read(path: Path) -> bytes | None:
    if path.is_symlink():
        raise ValueError(f"Symbolic links are not supported: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"Expected a file: {path}")
    if path.stat().st_size > MAX_EDIT_BYTES:
        raise ValueError(f"File exceeds {MAX_EDIT_BYTES} byte edit limit: {path}")
    return path.read_bytes()


class CodingCoordinator:
    """Main-agent owned staging area; child agents never write the live project."""

    def __init__(self, workspace: Path, models, session_id_getter, model_getter,
                 event_sink=None, internal_paths=None) -> None:
        self.workspace = workspace.resolve()
        self.models = models
        self.session_id_getter = session_id_getter
        self.model_getter = model_getter
        self.event_sink = event_sink
        self.root = self.workspace / ".codepilot" / "subagents"
        self.internal_paths = set()
        for database in internal_paths or []:
            path = Path(database).resolve()
            self.internal_paths.update({path, Path(str(path) + "-wal"),
                                        Path(str(path) + "-shm"), Path(str(path) + "-journal")})

    def _session_id(self) -> str:
        value = self.session_id_getter()
        if not value:
            raise ValueError("Start or resume a session before delegating")
        return value

    def _target(self, relative: str) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
            raise ValueError("Subagent path must be workspace-relative")
        unexpanded = self.workspace / relative
        cursor = unexpanded
        while cursor != self.workspace:
            if cursor.is_symlink():
                raise ValueError(f"Symbolic links cannot be merged: {relative}")
            cursor = cursor.parent
        path = unexpanded.resolve()
        if not path.is_relative_to(self.workspace):
            raise ValueError("Subagent path leaves the workspace")
        parts = path.relative_to(self.workspace).parts
        if not parts or set(parts).intersection({".git", ".codepilot", ".venv"}):
            raise ValueError("Internal paths cannot be merged")
        name = path.name.lower()
        if name == ".env" or name.startswith(".env.") and name != ".env.example" or path.suffix.lower() in {".pem", ".key"} or name in {"id_rsa", "id_ed25519"}:
            raise ValueError("Credential files cannot be merged")
        return path

    def _scope(self, paths: list[str]) -> list[dict[str, str]]:
        if not isinstance(paths, list) or not 1 <= len(paths) <= 12:
            raise ValueError("Specify 1 to 12 writable file or directory paths")
        scope = []
        for raw in paths:
            if not isinstance(raw, str) or not raw.strip() or raw.strip() in {".", "./", ".\\"}:
                raise ValueError("Each write path must be a specific file or directory")
            candidate = Path(raw.strip())
            if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
                raise ValueError("Write paths must be workspace-relative")
            target = self._target(raw.strip())
            relative = target.relative_to(self.workspace).as_posix()
            kind = "directory" if raw.endswith(("/", "\\")) or target.is_dir() else "file"
            scope.append({"path": relative, "kind": kind})
        return list({(item["path"], item["kind"]): item for item in scope}.values())

    @staticmethod
    def _within(path: str, item: dict[str, str]) -> bool:
        return path == item["path"] or item["kind"] == "directory" and path.startswith(item["path"] + "/")

    @classmethod
    def _overlap(cls, first: dict[str, str], second: dict[str, str]) -> bool:
        return cls._within(first["path"], second) or cls._within(second["path"], first)

    def _copy_project(self, destination: Path) -> None:
        copied = 0
        total = 0
        base, work = destination / "baseline", destination / "workspace"
        base.mkdir(parents=True, exist_ok=True)
        work.mkdir(parents=True, exist_ok=True)
        for folder, dirs, files in os.walk(self.workspace, followlinks=False):
            source_dir = Path(folder)
            dirs[:] = [name for name in dirs if name.lower() not in SKIP_COPY and not (source_dir / name).is_symlink()]
            for name in files:
                source = source_dir / name
                if source.resolve() in self.internal_paths:
                    continue
                lower = name.lower()
                if source.is_symlink() or lower in {"id_rsa", "id_ed25519"}:
                    continue
                if lower == ".env" or lower.startswith(".env.") and lower != ".env.example" or source.suffix.lower() in {".pem", ".key"}:
                    continue
                size = source.stat().st_size
                if size > MAX_EDIT_BYTES:
                    continue
                copied += 1
                total += size
                if copied > MAX_FILES or total > MAX_COPY_BYTES:
                    raise ValueError("Project exceeds subagent copy limit (2000 files or 40 MB)")
                relative = source.relative_to(self.workspace)
                original = base / relative
                staged = work / relative
                original.parent.mkdir(parents=True, exist_ok=True)
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, original)
                shutil.copy2(original, staged)

    @staticmethod
    def _files(root: Path) -> set[str]:
        return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() and not path.is_symlink()}

    def _changes(self, stage: Path, scope: list[dict[str, str]]) -> list[dict]:
        baseline, work = stage / "baseline", stage / "workspace"
        changes = []
        for name in sorted(self._files(baseline) | self._files(work)):
            before, after = _read(baseline / name), _read(work / name)
            if before == after:
                continue
            if not any(self._within(name, item) for item in scope):
                raise ValueError(f"Subagent changed a file outside its assigned scope: {name}")
            self._target(name)
            changes.append({"path": name, "before_sha256": _digest(before), "after_sha256": _digest(after)})
        if len(changes) > 40:
            raise ValueError("Subagent changed more than 40 files")
        return changes

    def _save(self, stage: Path, manifest: dict) -> None:
        stage.mkdir(parents=True, exist_ok=True)
        target = stage / "manifest.json"
        temporary = stage / "manifest.tmp"
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def _run_one(self, item: dict) -> dict:
        from .tools import ToolSet

        stage_id = uuid4().hex[:12]
        stage = self.root / stage_id
        manifest = {
            "id": stage_id, "session_id": self._session_id(), "task": item["task"],
            "paths": item["scope"], "status": "running", "summary": "", "changes": [], "merged": False,
        }
        self._save(stage, manifest)
        try:
            self._copy_project(stage)
            toolset = ToolSet(stage / "workspace", self.models, self.session_id_getter,
                              self.model_getter, write_scope=item["scope"], enable_delegation=False,
                              enable_mcp=False)
            names = (
                "list_directory", "read_file", "glob_files", "grep_code", "file_info",
                "write_file", "edit_file", "append_file", "make_directory",
                "delete_file", "move_file", "copy_file", "list_skills", "read_skill",
            )
            runner = SubagentRunner(
                self.models, self.model_getter, [toolset.by_name[name] for name in names],
                self.event_sink,
                role_prompts={"implement": (
                    "你是编程子 Agent。请先阅读相关文件，再完成明确分配的代码任务。"
                    "只能修改指定路径，保持变更小而完整。不要宣称执行过测试；你没有 shell 工具。"
                    "最终用中文报告实际修改的文件、设计决定、未验证事项。"
                )},
            )
            task = item["task"] + "\n允许写入的路径：" + ", ".join(
                part["path"] + ("/" if part["kind"] == "directory" else "") for part in item["scope"]
            )
            result = runner.run_one(task, "implement", agent_id=stage_id)
            manifest["summary"] = result["summary"]
            manifest["status"] = "ready" if result["status"] == "completed" else "failed"
            if manifest["status"] == "ready":
                manifest["changes"] = self._changes(stage, item["scope"])
            self._save(stage, manifest)
        except Exception as exc:
            manifest["status"] = "failed"
            manifest["summary"] = f"{type(exc).__name__}: {exc}"
            self._save(stage, manifest)
        return self._public(manifest)

    @staticmethod
    def _public(manifest: dict) -> dict:
        return {
            "id": manifest["id"], "status": manifest["status"],
            "task": manifest["task"], "paths": [change["path"] for change in manifest["changes"]],
            "summary": clipped_text(manifest["summary"], 3000), "merged": manifest["merged"],
        }

    def run(self, tasks: list[dict]) -> list[dict]:
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 3:
            raise ValueError("Delegate 1 to 3 coding tasks at once")
        prepared = []
        for task in tasks:
            if not isinstance(task, dict) or not isinstance(task.get("task"), str) or not 1 <= len(task["task"].strip()) <= 3500:
                raise ValueError("Each coding task needs a specific task of 1 to 3500 characters")
            prepared.append({"task": task["task"].strip(), "scope": self._scope(task.get("paths"))})
        for index, first in enumerate(prepared):
            for second in prepared[index + 1:]:
                if any(self._overlap(a, b) for a in first["scope"] for b in second["scope"]):
                    raise ValueError("Parallel coding tasks have overlapping write scopes")
        results: list[dict | None] = [None] * len(prepared)
        with ThreadPoolExecutor(max_workers=len(prepared), thread_name_prefix="coding-agent") as pool:
            futures = {pool.submit(self._run_one, item): index for index, item in enumerate(prepared)}
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return results

    def _load(self, task_id: str) -> tuple[Path, dict]:
        if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{12}", task_id):
            raise ValueError("Use the complete 12-character coding task ID")
        stage = self.root / task_id
        manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
        if manifest["id"] != task_id or manifest["session_id"] != self._session_id():
            raise ValueError("Coding task belongs to another session")
        return stage, manifest

    def list(self) -> list[dict]:
        if not self.root.exists():
            return []
        rows = []
        for path in sorted(self.root.glob("*/manifest.json")):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                if manifest["session_id"] == self._session_id():
                    rows.append(self._public(manifest))
            except (KeyError, ValueError, OSError):
                continue
        return rows

    def preview(self, task_ids: list[str]) -> str:
        if not isinstance(task_ids, list) or not task_ids:
            raise ValueError("Specify at least one coding task ID")
        sections = []
        for task_id in task_ids:
            stage, manifest = self._load(task_id)
            lines = [f"Task {task_id} [{manifest['status']}] {manifest['task']}",
                     f"Summary: {manifest['summary']}"]
            for change in manifest["changes"]:
                name = change["path"]
                before = _read(stage / "baseline" / name)
                after = _read(stage / "workspace" / name)
                if _digest(before) != change["before_sha256"]:
                    raise ValueError(f"Staged baseline changed after subagent completion: {name}")
                if _digest(after) != change["after_sha256"]:
                    raise ValueError(f"Staged file changed after subagent completion: {name}")
                try:
                    diff = "".join(difflib.unified_diff(
                        (before or b"").decode("utf-8").splitlines(keepends=True),
                        (after or b"").decode("utf-8").splitlines(keepends=True),
                        fromfile=f"a/{name}", tofile=f"b/{name}",
                    ))
                except UnicodeError:
                    diff = f"Binary change: {name}"
                lines.append(diff or f"Changed: {name}")
            sections.append("\n".join(lines))
        return clipped_text("\n\n".join(sections), 16_000)

    def changed_workspace_paths(self, task_ids: list[str]) -> list[Path]:
        if not isinstance(task_ids, list) or not task_ids or len(task_ids) > 3 or len(set(task_ids)) != len(task_ids):
            raise ValueError("Specify 1 to 3 distinct coding task IDs")
        paths = []
        for task_id in task_ids:
            _, manifest = self._load(task_id)
            if manifest["status"] != "ready" or manifest["merged"] or not manifest["changes"]:
                raise ValueError(f"Coding task is not ready to merge: {task_id}")
            paths.extend(self._target(change["path"]) for change in manifest["changes"])
        if len(set(paths)) != len(paths):
            raise ValueError("Coding tasks change the same file")
        return paths

    def apply(self, task_ids: list[str]) -> str:
        self.changed_workspace_paths(task_ids)
        prepared = []
        manifests = []
        for task_id in task_ids:
            stage, manifest = self._load(task_id)
            manifests.append((stage, manifest))
            for change in manifest["changes"]:
                name = change["path"]
                target = self._target(name)
                current = _read(target)
                baseline = _read(stage / "baseline" / name)
                staged = _read(stage / "workspace" / name)
                if _digest(baseline) != change["before_sha256"]:
                    raise ValueError(f"Staged baseline changed after subagent completion: {name}")
                if _digest(current) != change["before_sha256"]:
                    raise ValueError(f"Merge conflict: project file changed since delegation: {name}")
                if _digest(staged) != change["after_sha256"]:
                    raise ValueError(f"Staged file changed after subagent completion: {name}")
                prepared.append((target, current, staged))
        applied = []
        saved = []
        try:
            for target, current, staged in prepared:
                target.parent.mkdir(parents=True, exist_ok=True)
                if staged is None:
                    target.unlink()
                else:
                    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                        temporary = Path(handle.name)
                        handle.write(staged)
                    try:
                        temporary.replace(target)
                    finally:
                        temporary.unlink(missing_ok=True)
                applied.append((target, current))
            for stage, manifest in manifests:
                manifest["merged"] = True
                self._save(stage, manifest)
                saved.append((stage, manifest))
        except Exception:
            for stage, manifest in saved:
                manifest["merged"] = False
                self._save(stage, manifest)
            for target, previous in reversed(applied):
                if previous is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(previous)
            raise
        names = [str(target.relative_to(self.workspace)) for target, _, _ in prepared]
        return "Merged coding tasks " + ", ".join(task_ids) + "; changed: " + ", ".join(names) + ". Run relevant tests and summarize results."

    def reset_merged(self, task_ids: list[str]) -> None:
        """Keep staged tasks reusable if a later checkpoint write rolls the merge back."""
        for task_id in task_ids:
            stage, manifest = self._load(task_id)
            if manifest["merged"]:
                manifest["merged"] = False
                self._save(stage, manifest)
