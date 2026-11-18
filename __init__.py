"""Connect Telegram forum topics to local T3 Code projects."""

import asyncio
from collections import defaultdict
from contextlib import closing, contextmanager
from html import escape
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, time
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

from .artifacts import document_parts, find_apks, part_caption, without_apk_links
from .projects import ProjectError, prepare_project


LOG = logging.getLogger(__name__)
CLI_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:/.-]{0,159}\Z")
JOB_NUMBER = re.compile(r"#?([0-9]{1,12})\Z")
LEGACY_CARD = re.compile(r"\A[^·\r\n]{1,140} · #([0-9]{1,12}) · ")
MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
MARKDOWN.add_render_rule("image", lambda renderer, tokens, index, options, env: escape(tokens[index].content))
OWNER_ONLY = "Команда доступна владельцу в личном чате и подключённых топиках Telegram."
MODE_QUESTION = "Для чего этот топик — для разработки или обычного общения? Ответь «разработка» (dev) или «чат» (chat)."
REPO_QUESTION = "Пришли ссылку GitHub, название локального репозитория или абсолютный путь к нему на машине с Hermes."
CHAT_CHOICE = re.compile(r"\b(?:чат\w*|общени\w*|уч[её]б\w*|рес[её]рч\w*|chat)\b", re.I)
DEV_CHOICE = re.compile(r"\b(?:разработ\w*|кодинг\w*|dev(?:elopment)?)\b", re.I)
POLL_INTERVAL = 4
CLI_ENV = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "USERPROFILE", "USERNAME",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "TEMP", "TMP", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
    "LANG", "LC_ALL", "T3CODE_HOME",
}


class T3Error(Exception):
    """An unsuccessful or unrecognized T3 CLI response."""


class Bridge:
    def __init__(self, directory):
        self.directory = directory
        self.tasks = set()
        self.submissions = defaultdict(asyncio.Lock)
        self.preparing = set()

    def background(self, operation):
        task = asyncio.create_task(operation)
        self.tasks.add(task)
        task.add_done_callback(self.task_done)

    def task_done(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            LOG.error("Background T3 bridge operation failed", exc_info=task.exception())

    def settings(self):
        try:
            return json.loads((self.directory / "settings.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise T3Error("Локальные настройки T3 недоступны.") from error

    @contextmanager
    def database(self):
        path = self.directory.parent.parent / "t3-bridge.sqlite3"
        with closing(sqlite3.connect(path, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            with db:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS jobs (
                        id INTEGER PRIMARY KEY,
                        topic TEXT NOT NULL,
                        message TEXT NOT NULL,
                        thread TEXT,
                        run TEXT,
                        started_at REAL,
                        finished_at REAL,
                        UNIQUE (topic, message)
                    )
                """)
                fields = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
                for field in ("started_at", "finished_at"):
                    if field not in fields:
                        db.execute(f"ALTER TABLE jobs ADD COLUMN {field} REAL")
                db.execute("""
                    CREATE TABLE IF NOT EXISTS telegram_replies (
                        topic TEXT NOT NULL,
                        message TEXT NOT NULL,
                        job INTEGER NOT NULL REFERENCES jobs(id),
                        PRIMARY KEY (topic, message)
                    )
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS topics (
                        topic TEXT PRIMARY KEY,
                        title TEXT,
                        mode TEXT NOT NULL DEFAULT 'choose',
                        project_dir TEXT,
                        base_ref TEXT,
                        primary_job INTEGER REFERENCES jobs(id)
                    )
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS telegram_artifacts (
                        job INTEGER NOT NULL REFERENCES jobs(id),
                        path TEXT NOT NULL,
                        part INTEGER NOT NULL,
                        message TEXT,
                        PRIMARY KEY (job, path, part)
                    )
                """)
                yield db

    def origin(self, source):
        if source is None or getattr(source.platform, "value", None) != "telegram":
            return None
        settings = self.settings()
        owner = str(settings["telegram_owner"])
        if source.is_bot or str(source.user_id) != owner:
            return None
        if not source.message_id or not str(source.message_id).isdigit():
            return None
        chat = str(source.chat_id)
        if chat == owner:
            topic = source.thread_id or ""
        elif chat.startswith("-") and chat in map(str, settings.get("telegram_group_ids", [])):
            # Telegram may omit the topic ID for General. Treat it as topic 1 in a forum.
            topic = str(source.thread_id or "1")
            if not topic.isdigit():
                return None
        else:
            return None
        return f"{chat}:{topic}", str(source.message_id)

    def forum_origin(self, source):
        origin = self.origin(source)
        if origin and str(source.chat_id).startswith("-") and str(source.thread_id or "1") != "1":
            return origin
        return None

    def topic_settings(self, topic):
        with self.database() as db:
            return db.execute("SELECT * FROM topics WHERE topic = ?", (topic,)).fetchone()

    def add_topic(self, topic, title):
        with self.database() as db:
            return db.execute(
                "INSERT OR IGNORE INTO topics(topic, title) VALUES (?, ?)", (topic, title)
            ).rowcount > 0

    def telegram_handlers(self, app, adapter):
        from telegram.ext import MessageHandler, filters

        async def created(update, context):
            from gateway.config import Platform
            from gateway.session import SessionSource

            message = update.effective_message
            if message is None or not message.chat.is_forum or message.from_user is None:
                return
            name = message.forum_topic_created.name
            source = SessionSource(
                platform=Platform.TELEGRAM, chat_id=str(message.chat.id), chat_type="group",
                user_id=str(message.from_user.id), is_bot=message.from_user.is_bot,
                thread_id=str(message.message_thread_id or message.message_id),
                message_id=str(message.message_id), chat_topic=name,
            )
            origin = self.forum_origin(source)
            if origin is None or not adapter._is_user_authorized_from_message(message):
                return
            if self.add_topic(origin[0], name):
                await self.say(source, f"Топик «{name}».\n\n{MODE_QUESTION}")

        app.add_handler(MessageHandler(filters.StatusUpdate.FORUM_TOPIC_CREATED, created), group=18)
        LOG.info("Telegram project topic handler installed")

    @staticmethod
    async def say(source, text):
        from hermes_cli.plugin_messages import _gateway_loop

        gateway, _ = _gateway_loop()
        adapter = gateway._delivery_adapter_for(source) if gateway is not None else None
        if adapter is None:
            return
        result = await adapter.send(source.chat_id, text, metadata=gateway._thread_metadata_for_source(source))
        if not result.success:
            LOG.warning("Telegram topic setup message delivery was not confirmed")

    async def setup_project(self, source, topic, text):
        try:
            project, base = await asyncio.to_thread(prepare_project, text, self.settings())
            try:
                await asyncio.to_thread(self.cli, "list", project_dir=project)
            except T3Error:
                await asyncio.to_thread(self.cli, "register-project", project_dir=project)
                await asyncio.to_thread(self.cli, "list", project_dir=project)
            with self.database() as db:
                db.execute(
                    "UPDATE topics SET mode = 'development', project_dir = ?, base_ref = ? "
                    "WHERE topic = ? AND mode = 'repository'", (project, base, topic),
                )
            await self.say(source, f"Подключил {Path(project).name}. Основа рабочей ветки — {base}. "
                                   "Напиши первое поручение обычным сообщением.")
        except (ProjectError, T3Error, OSError) as error:
            message = str(error) if isinstance(error, (ProjectError, T3Error)) else "Не удалось открыть каталог репозитория."
            await self.say(source, f"{message}\n\n{REPO_QUESTION}")
        finally:
            self.preparing.discard(topic)

    async def topic_message(self, *, platform, source, message_id, text, **_):
        if platform != "telegram" or not (text := text.strip()):
            return None
        from gateway.session import SessionSource

        source = SessionSource.from_dict({**source, "message_id": source.get("message_id") or message_id})
        origin = self.forum_origin(source)
        if origin is None:
            return None
        topic = origin[0]
        state = self.topic_settings(topic)
        configure = text.casefold().strip(" .!?") in {"настроить топик", "setup topic"}
        if state is None:
            if not configure:
                return None
            self.add_topic(topic, source.chat_topic)
            return {"action": "handled", "reply": MODE_QUESTION}
        mode = state["mode"]
        if configure:
            reply = {
                "choose": MODE_QUESTION,
                "repository": REPO_QUESTION,
                "chat": "Этот топик настроен для обычного общения.",
                "development": f"Репозиторий топика — {Path(state['project_dir'] or '').name}.",
            }[mode]
            return {"action": "handled", "reply": reply}
        if mode == "chat":
            return None
        if mode == "choose":
            dev, chat = bool(DEV_CHOICE.search(text)), bool(CHAT_CHOICE.search(text))
            if dev == chat:
                return {"action": "handled", "reply": MODE_QUESTION}
            with self.database() as db:
                db.execute("UPDATE topics SET mode = ? WHERE topic = ? AND mode = 'choose'",
                           ("repository" if dev else "chat", topic))
            reply = REPO_QUESTION if dev else "Выбран обычный чат. Напиши, что хочешь обсудить."
            return {"action": "handled", "reply": reply}
        if mode == "repository":
            if topic in self.preparing:
                return {"action": "handled", "reply": "Подключаю репозиторий. Сообщу, когда он будет готов."}
            self.preparing.add(topic)
            self.background(self.setup_project(source, topic, text))
            return {"action": "handled", "reply": "Подключаю репозиторий…"}
        self.background(self.submit_topic(source, origin, text))
        return {"action": "handled"}

    async def submit_topic(self, source, origin, text):
        reply = await self.submit(source, origin, text)
        if reply:
            await self.say(source, reply)

    def cli(self, action, *args, prompt=None, project_dir=None):
        if any(arg != "-" and not CLI_VALUE.fullmatch(arg) for arg in args):
            raise T3Error("T3 вернул неподдерживаемый идентификатор или настройку.")
        settings = self.settings()
        executable = Path(settings["t3_command"])
        project = Path(project_dir or settings["project_dir"])
        if not executable.is_absolute() or not project.is_absolute():
            raise T3Error("В настройках нужны абсолютные пути к T3 и репозиторию.")
        env = {key: value for key, value in os.environ.items() if key.upper() in CLI_ENV}
        command = [str(executable), "thread", action, "--json"]
        # Arguments are fixed flags and validated IDs. Telegram text travels entirely on stdin.
        if action == "start":
            base_ref, provider, model, title = args
            command.extend(["--worktree", base_ref, "--provider", provider, "--model", model,
                            "--title", title, ".", "-"])
        elif action == "show":
            command.extend(["--messages", "24", *args])
        elif action == "register-project":
            command = [str(executable), "project", "add", "."]
        else:
            command.extend(args)
        try:
            result = subprocess.run(
                command, input=prompt, text=True, encoding="utf-8", errors="replace",
                capture_output=True, timeout=35, cwd=project, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode != 0:
                raise T3Error(f"T3 CLI вернул ошибку ({action}, код {result.returncode}).")
            if action == "register-project":
                return None
            data = json.loads(result.stdout)
            if not isinstance(data, list if action == "list" else dict):
                raise ValueError("unexpected response")
            return data
        except subprocess.TimeoutExpired as error:
            raise T3Error("T3 не подтвердил операцию за 35 секунд.") from error
        except OSError as error:
            raise T3Error("Не удалось запустить локальный T3 CLI.") from error
        except ValueError as error:
            raise T3Error("T3 вернул ответ в неизвестном формате.") from error

    def reserve(self, topic, message):
        with self.database() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO jobs(topic, message, started_at) VALUES (?, ?, ?)", (topic, message, time())
            ).rowcount
            job = db.execute(
                "SELECT * FROM jobs WHERE topic = ? AND message = ?", (topic, message)
            ).fetchone()
        return job, bool(inserted)

    def find_job(self, topic, number=None):
        with self.database() as db:
            if number is not None:
                return db.execute("SELECT * FROM jobs WHERE topic = ? AND id = ?", (topic, number)).fetchone()
            return db.execute("SELECT * FROM jobs WHERE topic = ? ORDER BY id DESC LIMIT 1", (topic,)).fetchone()

    def bind_job(self, job, thread_id, run_id):
        if not isinstance(thread_id, str) or not CLI_VALUE.fullmatch(thread_id):
            raise T3Error("T3 не вернул идентификатор треда.")
        with self.database() as db:
            db.execute("UPDATE jobs SET thread = ?, run = ? WHERE id = ?", (thread_id, run_id, job["id"]))
        return self.find_job(job["topic"], job["id"])

    def remember_message(self, job, message):
        with self.database() as db:
            db.execute(
                "INSERT OR IGNORE INTO telegram_replies(topic, message, job) VALUES (?, ?, ?)",
                (job["topic"], message, job["id"]),
            )

    def finish(self, job):
        if job["finished_at"] is None:
            with self.database() as db:
                db.execute("UPDATE jobs SET finished_at = ? WHERE id = ? AND finished_at IS NULL", (time(), job["id"]))
            return self.find_job(job["topic"], job["id"])
        return job

    @staticmethod
    def title_for(job, project):
        name = re.sub(r"[^A-Za-z0-9_-]", "-", Path(project).name).strip("-_") or "Project"
        return f"{name[:90]}-Hermes-{job['id']}"

    async def route_reply(self, *, event, gateway, **_):
        message = str(event.reply_to_message_id or "")
        text = (event.text or "").strip()
        if not event.allow_gateway_control or not message.isdigit() or not text:
            return None
        command = "dev_reply"
        if event.is_command():
            command = (event.get_command() or "").replace("-", "_")
            if command not in {"dev_reply", "dev_status", "dev_link"}:
                return None
            text = event.get_command_args().strip()
            if command == "dev_reply" and (not text or text.startswith("#")):
                return None
            if command != "dev_reply" and text:
                return None
        elif getattr(event.message_type, "value", None) != "text":
            return None
        origin = self.origin(event.source)
        if origin is None:
            return None
        topic, _ = origin
        with self.database() as db:
            job = db.execute("""
                SELECT id FROM jobs WHERE topic = ? AND message = ?
                UNION ALL
                SELECT job FROM telegram_replies WHERE topic = ? AND message = ?
                LIMIT 1
            """, (topic, message, topic, message)).fetchone()
        if job is None:
            # Cards published before this index existed can only be recovered from our own bot.
            parent = getattr(event.raw_message, "reply_to_message", None)
            sender = getattr(parent, "from_user", None)
            match = LEGACY_CARD.match(getattr(parent, "text", "") or "")
            if match and getattr(sender, "is_bot", False):
                adapter = gateway._delivery_adapter_for(event.source)
                bot = getattr(adapter, "_bot", None)
                if bot is not None and str(sender.id) == str(bot.id):
                    job = self.find_job(topic, int(match[1]))
                    if job is not None:
                        self.remember_message(job, message)
        if job is not None:
            return {"action": "rewrite", "text": f"/{command} #{job['id']} {text}".rstrip()}
        return None

    @staticmethod
    def card(job, heading, body, thread=None, *, updates=(), live=False):
        thread = thread or {}
        name = str(thread.get("title") or job["thread"] or "Ожидает запуска")[:140]
        branch = str(thread.get("branch") or "Ожидает данных от T3")[:140]
        title = f"<b>Тред</b> · {escape(name)}"
        git = f"<b>Ветка</b> · <code>{escape(branch)}</code>"
        footer = [title, git]
        if job["started_at"] is not None:
            seconds = max(0, int((job["finished_at"] or time()) - job["started_at"]))
            minutes, rest = divmod(seconds, 60)
            duration = f"{minutes}m{rest:02}s" if minutes else f"{seconds}s"
            footer.append(f"<b>Время</b> · {duration}")
        body = without_apk_links(body)
        rich_body = MARKDOWN.render(body)
        short = body
        if updates:
            recent = []
            remaining = 3000
            for item in reversed(updates):
                item = without_apk_links(item)
                if len(item) > remaining:
                    if not recent:
                        recent.append(f"{item[:remaining - 1].rstrip()}…")
                    break
                recent.append(item)
                remaining -= len(item) + 2
            recent.reverse()
            history = "<hr>".join(MARKDOWN.render(item) for item in recent)
            if live:
                rich_body = history
                short = "\n\n".join(recent)
            else:
                rich_body += f"<details><summary>Ход работы</summary>{history}</details>"
        if len(short.encode("utf-16-le")) > 6600:
            short = short.encode("utf-16-le")[:6500].decode("utf-16-le", errors="ignore").rstrip()
            short += "\n… Полный ответ можно открыть через /dev_link."
        rich = f"<h3>{escape(heading)}</h3>{rich_body}<hr><footer>{'<br>'.join(footer)}</footer>"
        legacy = f"<b>{escape(heading)}</b>\n\n{escape(short)}\n\n" + "\n".join(footer)
        return rich, legacy

    def progress(self, job, thread=None, updates=()):
        elapsed = max(0, time() - (job["started_at"] or time()))
        dots = "." * (1 + int(elapsed / POLL_INTERVAL) % 3)
        return self.card(job, f"Выполняю{dots}", "Результат появится здесь.", thread, updates=updates, live=True)

    @staticmethod
    def assistant_messages(job, snapshot):
        return [
            message["text"].strip() for message in snapshot.get("messages", [])
            if message.get("role") == "assistant" and message.get("runId") == job["run"]
            and (message.get("text") or "").strip()
        ]

    def render(self, job, snapshot):
        thread = snapshot["thread"]
        run_id = job["run"]
        active = thread.get("activeRunId")
        latest = thread.get("latestRunId")
        status = thread.get("status")
        current_run = run_id in (active, latest) and run_id is not None
        if run_id is None:
            heading, body = "Ход принят", "Состояние можно посмотреть в треде T3."
            return self.card(self.finish(job), heading, body, thread), True
        answers = self.assistant_messages(job, snapshot)
        if current_run and thread.get("pendingRequestCount", 0):
            body = "T3 ждёт подтверждения или ответа. Ответь на эту карточку командой /dev_link, чтобы открыть тред."
            return self.card(self.finish(job), "Нужно твоё решение", body, thread, updates=answers), True
        if current_run and status in {"error", "failed", "cancelled", "interrupted"}:
            return self.card(self.finish(job), "Ход завершён", f"Статус T3 — `{status}`.", thread, updates=answers), True
        running = active == run_id or (latest == run_id and status in {"running", "queued", "starting"})
        if answers and not running:
            answer = answers[-1]
            if len(answer) > 16000:
                answer = answer[:15800].rstrip() + "\n\n… Полный ответ можно открыть через /dev_link."
            return self.card(self.finish(job), "Результат", answer, thread, updates=answers[:-1]), True
        if current_run and active is None and status == "completed":
            return self.card(self.finish(job), "Ход завершён", "T3 завершил ход без текстового ответа.", thread), True
        return self.progress(job, thread, answers), False

    async def snapshot(self, job):
        return await asyncio.to_thread(self.cli, "show", job["thread"])

    async def send_apks(self, job, snapshot, send):
        from telegram.error import BadRequest, Forbidden

        thread = snapshot["thread"]
        if not job["run"] or thread.get("activeRunId") == job["run"]:
            return None
        if thread.get("latestRunId") == job["run"] and thread.get("status") != "completed":
            return None
        answers = self.assistant_messages(job, snapshot)
        if not answers:
            return None
        apks, mentioned = find_apks(answers[-1], thread.get("worktreePath"))
        if not apks:
            return "APK не найден по указанному пути в рабочей папке T3." if mentioned else None

        for apk in apks:
            with TemporaryDirectory(prefix="hermes-t3-apk-") as directory:
                parts = await asyncio.to_thread(document_parts, apk, Path(directory))
                for number, part in enumerate(parts, 1):
                    key = (job["id"], str(apk), number)
                    with self.database() as db:
                        claimed = db.execute(
                            "INSERT OR IGNORE INTO telegram_artifacts(job, path, part) VALUES (?, ?, ?)", key,
                        ).rowcount
                        if not claimed:
                            row = db.execute(
                                "SELECT message FROM telegram_artifacts WHERE job = ? AND path = ? AND part = ?", key,
                            ).fetchone()
                            if row["message"] is None:
                                return "Telegram ещё не подтвердил загрузку APK."
                            continue
                    try:
                        message = await send(part, part_caption(apk, number, len(parts)))
                        if not str(message).isdigit():
                            raise T3Error("Telegram не подтвердил документ.")
                    except (BadRequest, Forbidden) as error:
                        with self.database() as db:
                            db.execute(
                                "DELETE FROM telegram_artifacts WHERE job = ? AND path = ? AND part = ?", key,
                            )
                        LOG.warning("T3 job %s: APK part %s was rejected (%s)", job["id"], number, type(error).__name__)
                        return "Telegram отклонил загрузку APK."
                    except Exception as error:
                        LOG.warning("T3 job %s: APK part %s not confirmed (%s)", job["id"], number, type(error).__name__)
                        return "Telegram не подтвердил загрузку APK."
                    with self.database() as db:
                        db.execute(
                            "UPDATE telegram_artifacts SET message = ? WHERE job = ? AND path = ? AND part = ?",
                            (str(message), *key),
                        )
                    self.remember_message(job, str(message))
                    LOG.info("T3 job %s: Telegram APK part %s confirmed for message %s", job["id"], number, message)
        return None

    async def publish_apks(self, source, job, snapshot, target):
        from hermes_cli.plugin_messages import _gateway_loop
        from telegram import InputFile, ReplyParameters

        gateway, _ = _gateway_loop()
        adapter = gateway._delivery_adapter_for(source) if gateway is not None else None
        bot = getattr(adapter, "_bot", None)
        if bot is None:
            return
        metadata = gateway._thread_metadata_for_source(source)
        routing = adapter._compute_single_send_routing(
            source.chat_id, target[0] if target else str(source.message_id),
            metadata, adapter._metadata_thread_id(metadata),
        )
        if routing is None:
            return
        reply_id, topic_kwargs = routing

        async def send(path, caption):
            with path.open("rb") as stream:
                sent = await bot.send_document(
                    chat_id=int(source.chat_id), document=InputFile(stream, filename=path.name, read_file_handle=False),
                    caption=caption, reply_parameters=ReplyParameters(message_id=reply_id) if reply_id else None,
                    write_timeout=120, read_timeout=45, **topic_kwargs,
                )
            if not sent.document or sent.document.file_size != path.stat().st_size:
                raise T3Error("Telegram не подтвердил размер документа.")
            return sent.message_id

        try:
            problem = await self.send_apks(job, snapshot, send)
        except OSError:
            problem = "Не удалось прочитать APK для отправки."
        if problem:
            await self.say(source, problem)

    async def publish(self, source, job, card, target=None, *, quiet=False):
        from hermes_cli.plugin_messages import _gateway_loop

        gateway, _ = _gateway_loop()
        adapter = gateway._delivery_adapter_for(source) if gateway is not None else None
        bot = getattr(adapter, "_bot", None)
        if bot is None:
            return None
        rich, legacy = card
        payload = {"chat_id": int(source.chat_id), "link_preview_options": {"is_disabled": True}}
        if target is None:
            metadata = gateway._thread_metadata_for_source(source)
            routing = adapter._compute_single_send_routing(
                source.chat_id, str(source.message_id), metadata, adapter._metadata_thread_id(metadata),
            )
            if routing is None:
                return None
            reply_id, thread_kwargs = routing
            payload.update({key: value for key, value in thread_kwargs.items() if value is not None})
            if reply_id is not None:
                payload["reply_parameters"] = {"message_id": reply_id}
            use_rich = not getattr(adapter, "_rich_send_disabled", False)
        else:
            message, use_rich = target
            payload["message_id"] = int(message)
        formats = (True, False) if use_rich else (False,)
        for use_rich in formats:
            method = "editMessageText" if target else "sendRichMessage" if use_rich else "sendMessage"
            content = {"rich_message": {"html": rich}} if use_rich else {"text": legacy, "parse_mode": "HTML"}
            try:
                result = await asyncio.wait_for(bot.do_api_request(method, api_kwargs={**payload, **content}), 20)
            except Exception as error:
                if target and "message is not modified" in str(error).lower():
                    return target
                if use_rich and adapter._is_rich_fallback_error(error):
                    if adapter._is_rich_capability_error(error):
                        adapter._rich_send_disabled = True
                    LOG.info("T3 job %s: %s rejected Rich HTML; using standard HTML (%s)",
                             job["id"], method, type(error).__name__)
                    continue
                LOG.warning("T3 job %s: %s delivery was not confirmed (%s)", job["id"], method, type(error).__name__)
                return None
            if target:
                message = target[0]
            else:
                message = result.get("message_id") if isinstance(result, dict) else getattr(result, "message_id", None)
                if not str(message).isdigit():
                    LOG.warning("T3 job %s: %s returned no Telegram message ID", job["id"], method)
                    return None
                message = str(message)
                self.remember_message(job, message)
            if not quiet:
                LOG.info("T3 job %s: Telegram %s confirmed for message %s (%s HTML)",
                         job["id"], method, message, "Rich" if use_rich else "standard")
            return message, use_rich
        return None

    async def watch(self, source, job, target, snapshot=None):
        started = last_snapshot = monotonic()
        try:
            while monotonic() - last_snapshot < 600:
                await asyncio.sleep(POLL_INTERVAL if monotonic() - started < 600 else 12)
                try:
                    snapshot = await self.snapshot(job)
                    last_snapshot = monotonic()
                except T3Error:
                    pass
                if snapshot is None:
                    card, finished = self.progress(job), False
                else:
                    card, finished = self.render(job, snapshot)
                updated = await self.publish(source, job, card, target, quiet=not finished) if target or finished else None
                target = updated or target
                if finished:
                    await self.publish_apks(source, job, snapshot, target)
                    return
            body = "Ответь на карточку командой /dev_status, чтобы обновить состояние."
            thread = snapshot["thread"] if snapshot else None
            await self.publish(source, job, self.card(job, "T3 ещё работает", body, thread), target)
        except (KeyError, OSError, ValueError, sqlite3.Error):
            LOG.exception("T3 status check failed for local job %s", job["id"])

    async def submit(self, source, origin, prompt, previous=None):
        topic, message = origin
        async with self.submissions[topic]:
            return await self.launch(source, topic, message, prompt, previous)

    async def recover_primary(self, state):
        topic, project = state["topic"], state["project_dir"]
        job = self.find_job(topic, state["primary_job"])
        if job["thread"]:
            return job
        title = self.title_for(job, project)
        entries = await asyncio.to_thread(self.cli, "list", project_dir=project)
        for entry in entries:
            if entry.get("title") == title:
                return self.bind_job(job, entry.get("threadId"), entry.get("latestRunId"))
        return None

    async def launch(self, source, topic, message, prompt, previous):
        state = self.topic_settings(topic)
        primary = state if state is not None and state["mode"] == "development" else None
        if previous is not None:
            previous = self.find_job(topic, previous["id"])
            if not previous["thread"]:
                return await self.status_for(source, previous)
        elif primary is not None and primary["primary_job"] is not None:
            try:
                previous = await self.recover_primary(primary)
            except T3Error as error:
                return f"Не удалось проверить тред топика. {error}"
            if previous is None:
                with self.database() as db:
                    db.execute("UPDATE topics SET primary_job = NULL WHERE topic = ?", (topic,))
        elif state is not None and state["mode"] in {"choose", "repository"}:
            return MODE_QUESTION if state["mode"] == "choose" else REPO_QUESTION

        job, is_new = self.reserve(topic, message)
        if not is_new:
            return await self.status_for(source, job)
        settings = self.settings()
        project = primary["project_dir"] if primary else settings["project_dir"]
        base = primary["base_ref"] if primary else settings["base_ref"]
        if previous is None:
            if primary is not None:
                with self.database() as db:
                    db.execute("UPDATE topics SET primary_job = ? WHERE topic = ?", (job["id"], topic))
            args = ("start", base, settings["provider"], settings["model"], self.title_for(job, project))
        else:
            args = ("send", previous["thread"], "-")

        operation = asyncio.create_task(asyncio.to_thread(self.cli, *args, prompt=prompt, project_dir=project))
        try:
            known_thread = {"title": self.title_for(job, project), "branch": f"База · {base}"} if previous is None else None
            target = await self.publish(source, job, self.progress(job, known_thread))
            while target and not operation.done():
                done, _ = await asyncio.wait({operation}, timeout=POLL_INTERVAL)
                if not done:
                    updated = await self.publish(source, job, self.progress(job, known_thread), target, quiet=True)
                    target = updated or target
            result = await operation
            thread_id = previous["thread"] if previous is not None else result.get("threadId")
            job = self.bind_job(job, thread_id, result.get("runId"))
        except T3Error as error:
            body = f"{error} Проверь T3 перед повторным запуском."
            card = self.card(self.finish(job), "T3 не подтвердил поручение", body, known_thread)
            if await self.publish(source, job, card, target):
                return None
            return body

        snapshot = None
        try:
            snapshot = await self.snapshot(job)
            card, finished = self.render(job, snapshot)
        except T3Error:
            card, finished = self.progress(job, known_thread), False
        updated = await self.publish(source, job, card, target, quiet=not finished and bool(target))
        target = updated or target
        if not finished:
            self.background(self.watch(source, job, target, snapshot))
        else:
            self.background(self.publish_apks(source, job, snapshot, target))
        if target is None:
            return "T3 принял поручение. Проверить результат можно через /dev_status."
        return None

    async def status_for(self, source, job):
        if job is None:
            return "В этой теме пока нет такого поручения. Запуск: /dev задача."
        if not job["thread"]:
            card = self.card(job, "Запуск не подтверждён", "Проверь список в T3.")
        else:
            try:
                card, _ = self.render(job, await self.snapshot(job))
            except T3Error as error:
                card = self.card(job, "Статус недоступен", str(error))
        if await self.publish(source, job, card):
            return None
        return "Telegram не подтвердил доставку карточки."

    async def link(self, args, *, source=None):
        origin = self.origin(source)
        if origin is None:
            return OWNER_ONLY
        number = JOB_NUMBER.fullmatch(args.strip()) if args.strip() else None
        if args.strip() and number is None:
            return "Отправь /dev_link или ответь этой командой на карточку T3."
        job = self.find_job(origin[0], int(number[1]) if number else None)
        if job is None or not job["thread"]:
            return "В этой теме пока нет такого треда."
        try:
            link = (await self.snapshot(job))["thread"].get("link") or ""
        except T3Error as error:
            return str(error)
        if urlsplit(link).scheme not in {"https", "http", "t3", "t3code"}:
            return "T3 пока не предоставил ссылку на этот тред."
        return link

    async def start(self, args, *, source=None):
        origin = self.origin(source)
        if origin is None:
            return OWNER_ONLY
        if not args.strip():
            return "Запуск задания: /dev задача. Статус: /dev_status. Уточнение: /dev_reply текст."
        return await self.submit(source, origin, args.strip())

    async def status(self, args, *, source=None):
        origin = self.origin(source)
        if origin is None:
            return OWNER_ONLY
        number = JOB_NUMBER.fullmatch(args.strip()) if args.strip() else None
        if args.strip() and number is None:
            return "Отправь /dev_status или ответь этой командой на карточку T3."
        return await self.status_for(source, self.find_job(origin[0], int(number[1]) if number else None))

    async def reply(self, args, *, source=None):
        origin = self.origin(source)
        if origin is None:
            return OWNER_ONLY
        prompt = args.strip()
        number = None
        if prompt.startswith("#"):
            head, _, prompt = prompt.partition(" ")
            match = JOB_NUMBER.fullmatch(head)
            if match is None:
                return "Отправь /dev_reply с текстом или напиши ответ на карточку T3."
            number = int(match[1])
        if not prompt.strip():
            return "Отправь /dev_reply с текстом или напиши ответ на карточку T3."
        previous = self.find_job(origin[0], number)
        if previous is None:
            return await self.status_for(source, previous)
        return await self.submit(source, origin, prompt.strip(), previous)


def register(ctx):
    bridge = Bridge(Path(__file__).resolve().parent)
    ctx.register_command("dev", bridge.start, "Запустить поручение в T3", "задача")
    ctx.register_command("dev-status", bridge.status, "Посмотреть поручение T3")
    ctx.register_command("dev-reply", bridge.reply, "Уточнить поручение в том же треде T3", "текст")
    ctx.register_command("dev-link", bridge.link, "Получить ссылку на тред T3")
    ctx.register_hook("pre_gateway_dispatch", bridge.route_reply)
    ctx.register_hook("post_gateway_admission", bridge.topic_message)
    ctx.register_telegram_handler(bridge.telegram_handlers)
