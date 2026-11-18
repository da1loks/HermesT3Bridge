"""Resolve the project selected for a Telegram topic and its worktree base."""

import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory


REPO = r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})"
GITHUB_URL = re.compile(r"(?:https://github\.com/|git@github\.com:)" + REPO + r"/?(?=$|[\s)>])", re.I)
SAFE_BASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:/.-]{0,159}\Z")


class ProjectError(Exception):
    """A project choice could not be prepared."""


def github_slug(text):
    match = GITHUB_URL.search(text.strip()) or re.fullmatch(REPO, text.strip(), re.I)
    if match:
        owner, repo = match[1], match[2].removesuffix(".git")
        if repo not in {"", ".", ".."}:
            return owner.lower(), repo.lower()
    return None


def git(directory, *args, timeout=12):
    executable = shutil.which("git.exe" if os.name == "nt" else "git")
    if executable is None:
        raise ProjectError("Git не найден на машине с Hermes.")
    try:
        completed = subprocess.run(
            [executable, *args], cwd=directory, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProjectError("Git не подтвердил операцию. Проверь подключение и попробуй ещё раз.") from error
    return completed.stdout.strip() if completed.returncode == 0 else None


def clone_github(root, slug):
    owner, repo = slug
    destination = root / ".hermes-github" / owner / repo
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        url = f"https://github.com/{owner}/{repo}.git"
        with TemporaryDirectory(dir=destination.parent, prefix=".incoming-") as tmp:
            if git(tmp, "clone", "--", url, "repo", timeout=120) is None:
                raise ProjectError("Не удалось клонировать репозиторий. Проверь ссылку и доступ GitHub на машине с Hermes.")
            try:
                (Path(tmp) / "repo").rename(destination)
            except OSError:
                if not destination.is_dir():
                    raise
    if github_slug(git(destination, "config", "--get", "remote.origin.url") or "") != slug:
        raise ProjectError("Каталог для клона уже занят другим репозиторием.")
    return destination


def prepare_project(text, settings):
    default_project = Path(settings["project_dir"])
    root = Path(settings.get("projects_root") or default_project.parent).resolve()
    choice = re.sub(r"^(?:локальный(?: репозиторий)?|репозиторий|проект)\s+", "", text.strip(), flags=re.I)
    choice = choice.strip("`«»\"'")
    slug = github_slug(choice)
    if slug:
        project = next(
            (path for path in (root / slug[1], default_project, *root.iterdir())
             if path.is_dir() and ((path / ".git").exists())
             and github_slug(git(path, "config", "--get", "remote.origin.url") or "") == slug),
            None,
        )
        if project is None:
            project = clone_github(root, slug)
    else:
        path = Path(choice)
        if path.is_absolute():
            project = path.resolve()
        elif len(path.parts) == 1 and path.name not in {"", ".", ".."}:
            project = root / path
        else:
            raise ProjectError("Пришли название локального репозитория, абсолютный путь или ссылку GitHub.")
    if not project.is_dir() or not (project / ".git").exists():
        raise ProjectError("Репозиторий не найден. Проверь название, путь или ссылку GitHub.")
    head = git(project, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    refs = (head, "origin/main", "origin/master", "main", "master", "HEAD")
    if project.resolve() == default_project.resolve():
        refs = (settings["base_ref"], *refs)
    for ref in refs:
        if ref and SAFE_BASE.fullmatch(ref):
            commit = git(project, "rev-parse", "--verify", f"{ref}^{{commit}}")
            if commit:
                return str(project.resolve()), commit if ref == "HEAD" else ref
    raise ProjectError("У репозитория пока нет коммита для отдельной рабочей ветки.")
