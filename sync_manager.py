from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable

from git import (
    Actor,
    GitCommandError,
    InvalidGitRepositoryError,
    NoSuchPathError,
    Repo,
)

from config import AppConfig, load_config


DEFAULT_COMMIT_AUTHOR = Actor("Codex Memory Sync", "codex-memory@local")


class SyncManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.codex_session_dir = Path(config.codex_session_dir).expanduser()
        self.local_sync_temp_dir = Path(config.local_sync_temp_dir).expanduser()
        self.git_remote_url = config.git_remote_url.strip()

    def init_repo(self) -> Repo:
        repo_path = self.local_sync_temp_dir
        git_dir = repo_path / ".git"

        if git_dir.exists():
            repo = Repo(repo_path)
            self._ensure_remote(repo)
            return repo

        if repo_path.exists() and any(repo_path.iterdir()):
            try:
                repo = Repo(repo_path)
            except (InvalidGitRepositoryError, NoSuchPathError) as exc:
                raise RuntimeError(
                    f"Target sync directory exists but is not a Git repo: {repo_path}"
                ) from exc
            self._ensure_remote(repo)
            return repo

        if self.git_remote_url:
            repo_path.parent.mkdir(parents=True, exist_ok=True)
            repo = Repo.clone_from(self.git_remote_url, repo_path)
        else:
            repo_path.mkdir(parents=True, exist_ok=True)
            repo = Repo.init(repo_path)

        self._ensure_remote(repo)
        return repo

    def pull_sessions(self) -> list[Path]:
        repo = self.init_repo()
        origin = self._require_origin(repo)
        origin.fetch()

        try:
            branch_name = self._get_remote_default_branch(repo)
        except RuntimeError:
            return self._list_repo_session_files(repo)

        remote_ref = f"origin/{branch_name}"

        if branch_name in repo.heads:
            repo.heads[branch_name].checkout()
        else:
            repo.git.checkout("-b", branch_name, remote_ref)

        repo.git.pull("--ff-only", "origin", branch_name)
        return self._list_repo_session_files(repo)

    def push_sessions(self) -> list[Path]:
        repo = self.init_repo()

        if self.git_remote_url:
            try:
                self._pull_if_possible(repo)
            except GitCommandError:
                # Allow first push to an empty remote repository.
                pass

        copied_files = self._copy_session_files()
        if not copied_files:
            return []

        repo.index.add([str(path.relative_to(self.local_sync_temp_dir)) for path in copied_files])
        staged_changes = repo.git.diff("--cached", "--name-only").strip()
        if not staged_changes:
            return copied_files

        commit_message = datetime.now().strftime("Sync Codex sessions %Y-%m-%d %H:%M:%S")
        repo.index.commit(
            commit_message,
            author=DEFAULT_COMMIT_AUTHOR,
            committer=DEFAULT_COMMIT_AUTHOR,
        )

        if self.git_remote_url:
            branch_name = self._ensure_local_branch(repo)
            repo.git.push("--set-upstream", "origin", branch_name)

        return copied_files

    def _copy_session_files(self) -> list[Path]:
        if not self.codex_session_dir.exists():
            raise FileNotFoundError(
                f"Codex session directory does not exist: {self.codex_session_dir}"
            )

        self.local_sync_temp_dir.mkdir(parents=True, exist_ok=True)
        copied_files: list[Path] = []

        for source_file in self._iter_session_files():
            relative_path = source_file.relative_to(self.codex_session_dir)
            target_file = self.local_sync_temp_dir / relative_path
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file)
            copied_files.append(target_file)

        return copied_files

    def _iter_session_files(self) -> Iterable[Path]:
        return self.codex_session_dir.rglob("*.jsonl")

    def _list_repo_session_files(self, repo: Repo) -> list[Path]:
        worktree_dir = repo.working_tree_dir
        if not worktree_dir:
            return []
        return sorted(Path(worktree_dir).rglob("*.jsonl"))

    def _ensure_remote(self, repo: Repo) -> None:
        if not self.git_remote_url:
            return

        if self._has_origin(repo):
            origin = repo.remotes.origin
            existing_urls = list(origin.urls)
            current_url = existing_urls[0] if existing_urls else ""
            if current_url != self.git_remote_url:
                origin.set_url(self.git_remote_url)
            return

        repo.create_remote("origin", self.git_remote_url)

    def _require_origin(self, repo: Repo):
        if not self._has_origin(repo):
            raise ValueError("Git remote 'origin' is not configured.")
        return repo.remotes.origin

    def _has_origin(self, repo: Repo) -> bool:
        return any(remote.name == "origin" for remote in repo.remotes)

    def _get_remote_default_branch(self, repo: Repo) -> str:
        try:
            origin_head = repo.git.symbolic_ref("refs/remotes/origin/HEAD").strip()
            return origin_head.rsplit("/", 1)[-1]
        except GitCommandError:
            origin = self._require_origin(repo)
            for ref in origin.refs:
                if ref.remote_head != "HEAD":
                    return ref.remote_head
        raise RuntimeError("Unable to determine remote default branch.")

    def _ensure_local_branch(self, repo: Repo) -> str:
        try:
            return repo.active_branch.name
        except (TypeError, ValueError):
            branch_name = "main"
            if branch_name in repo.heads:
                repo.heads[branch_name].checkout()
            else:
                repo.git.checkout("-b", branch_name)
            return branch_name

    def _pull_if_possible(self, repo: Repo) -> None:
        origin = self._require_origin(repo)
        origin.fetch()
        try:
            branch_name = self._get_remote_default_branch(repo)
        except RuntimeError:
            return

        remote_ref = f"origin/{branch_name}"
        remote_exists = any(ref.remote_head == branch_name for ref in origin.refs)
        if not remote_exists:
            return

        if branch_name in repo.heads:
            repo.heads[branch_name].checkout()
        else:
            repo.git.checkout("-b", branch_name, remote_ref)

        repo.git.pull("--ff-only", "origin", branch_name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync Codex session JSONL files with Git.")
    parser.add_argument(
        "action",
        choices=["init", "pull", "push"],
        help="Action to run against the sync repository.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Optional path to config.json. Defaults to ~/.codex-memory/config.json",
    )
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    app_config = load_config(args.config)
    manager = SyncManager(app_config)

    if args.action == "init":
        repo = manager.init_repo()
        print(f"Repository ready: {repo.working_tree_dir}")
    elif args.action == "pull":
        files = manager.pull_sessions()
        print(f"Pulled {len(files)} session file(s) into {manager.local_sync_temp_dir}")
    elif args.action == "push":
        files = manager.push_sessions()
        print(f"Pushed {len(files)} session file(s) from {manager.codex_session_dir}")
