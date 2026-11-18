"""Resolve APKs mentioned by T3 and prepare Telegram document uploads."""

from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


DOCUMENT_LIMIT = 50_000_000
PART_SIZE = 45_000_000
MARKDOWN_LINK = re.compile(r"\[([^\]\r\n]+)\]\(\s*(<[^>\r\n]+>|[^)\r\n]+)\s*\)")
INLINE_CODE = re.compile(r"`([^`\r\n]+)`")
WINDOWS_PATH = re.compile(r"^/?[A-Za-z]:[/\\]")


def apk_path(value):
    value = unquote(value.strip().removeprefix("<").removesuffix(">"))
    if not value.lower().endswith(".apk"):
        return None
    if WINDOWS_PATH.match(value):
        return value.removeprefix("/")
    try:
        uri = urlsplit(value)
    except ValueError:
        return None
    if uri.scheme == "file" and uri.netloc.lower() in {"", "localhost"}:
        return uri.path.removeprefix("/") if WINDOWS_PATH.match(uri.path) else uri.path
    if not uri.scheme and not uri.netloc and not value.startswith("\\\\"):
        return value
    return None


def without_apk_links(text):
    return MARKDOWN_LINK.sub(lambda match: match[1] if apk_path(match[2]) else match[0], text)


def find_apks(text, worktree):
    candidates = [
        *(match[2] for match in MARKDOWN_LINK.finditer(text)),
        *(match[1] for match in INLINE_CODE.finditer(text)),
    ]
    references = [path for value in candidates if (path := apk_path(value))]
    if not references or not worktree:
        return [], bool(references)
    root = Path(worktree).resolve()
    found = {}
    for reference in references:
        try:
            path = (root / reference).resolve(strict=True)
            if path.is_relative_to(root) and path.is_file() and path.stat().st_size > 0:
                found[path] = None
        except (OSError, ValueError):
            continue
    return list(found), True


def document_parts(apk, directory):
    if apk.stat().st_size <= DOCUMENT_LIMIT:
        return [apk]
    parts = []
    with apk.open("rb") as source:
        while chunk := source.read(PART_SIZE):
            part = directory / f"{apk.name}.{len(parts) + 1:03}"
            part.write_bytes(chunk)
            parts.append(part)
    return parts


def part_caption(apk, number, count):
    if count == 1:
        return f"APK · {apk.name}"
    if number < count:
        return f"{apk.name} · часть {number} из {count}"
    inputs = "+".join(f'"{apk.name}.{index:03}"' for index in range(1, count + 1))
    command = f'cmd /c copy /b {inputs} "{apk.name}"'
    # Telegram captions are limited to 1024 characters.
    instructions = (
        f"Сохрани все части в одну папку. В Windows выполни в этой папке:\n{command}"
        if len(command) < 800 and re.fullmatch(r"[A-Za-z0-9_.-]+", apk.name)
        else "Сохрани все части в одну папку и склей их в порядке номеров."
    )
    return f"{apk.name} · часть {number} из {count}\n\n{instructions}"
