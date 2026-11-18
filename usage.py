"""Read and format the local Codex account's subscription windows."""

import asyncio
import json
from math import ceil
import os
from pathlib import Path
import shutil
import subprocess
from time import time


class UsageError(Exception):
    """Codex did not provide usable account limits."""


def codex_command(configured):
    command = configured or shutil.which("codex")
    if not command and os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        command = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    if not command or not Path(command).is_absolute() or not Path(command).is_file():
        raise UsageError("Codex CLI не найден. Укажи абсолютный путь в codex_command.")
    return str(command)


async def exchange(process, request):
    process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
    await process.stdin.drain()
    request_id = request.get("id")
    if request_id is None:
        return None
    while line := await process.stdout.readline():
        message = json.loads(line)
        if isinstance(message, dict) and message.get("id") == request_id:
            if isinstance(message.get("result"), dict):
                return message["result"]
            break
    raise UsageError("Codex не выдал лимиты. Проверь вход в аккаунт ChatGPT в локальном Codex CLI.")


async def read_rate_limits(command, environment):
    process = None
    try:
        async with asyncio.timeout(12):
            process = await asyncio.create_subprocess_exec(
                command, "app-server", "--listen", "stdio://",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=Path.home(), env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            await exchange(process, {
                "id": 1, "method": "initialize",
                "params": {"clientInfo": {"name": "hermes-t3-bridge", "version": "0.5.0"}},
            })
            await exchange(process, {"method": "initialized", "params": {}})
            return await exchange(process, {"id": 2, "method": "account/rateLimits/read", "params": {}})
    except TimeoutError as error:
        raise UsageError("Codex не успел ответить. Попробуй /usage ещё раз.") from error
    except (OSError, ValueError) as error:
        raise UsageError("Не удалось прочитать лимиты через локальный Codex CLI.") from error
    finally:
        if process is not None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 1)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()


def reset_in(timestamp, now):
    if type(timestamp) not in (int, float):
        return "время сброса недоступно"
    minutes = max(0, ceil((timestamp - now) / 60))
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    duration = f"{hours} ч {minutes} мин" if hours or days else f"{minutes} мин"
    if days:
        duration = f"{days} д {duration}"
    return "до сброса " + duration


def usage_message(result, *, now=None):
    rates = (result.get("rateLimitsByLimitId") or {}).get("codex") or result.get("rateLimits") or {}
    windows = {}
    for key in ("primary", "secondary"):
        window = rates.get(key)
        if isinstance(window, dict):
            duration = window.get("windowDurationMins")
            percent = window.get("usedPercent")
            if duration in (300, 10080) and type(percent) in (int, float):
                windows[duration] = window
    now = time() if now is None else now
    lines = ["Лимиты Codex"]
    for duration, title in ((300, "5 часов"), (10080, "Неделя")):
        window = windows.get(duration)
        if window is None:
            lines.append(f"{title} · данные недоступны")
        else:
            lines.append(f"{title} · использовано {window['usedPercent']:g}% · {reset_in(window.get('resetsAt'), now)}")
    return "\n".join(lines)
