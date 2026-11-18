# HermesT3Bridge

HermesT3Bridge connects Telegram forum topics to local T3 Code projects through Hermes Agent. Set up a topic for a repository, send a coding request in plain text, and follow the work from Telegram.

- Each development topic has a repository and a primary T3 thread. The first request creates a Git worktree. Later requests continue that thread.
- Replying to a T3 card targets its specific thread, even when the topic contains newer work.
- The bot shows progress updates, the branch and elapsed time in a Telegram card. It edits the same card with the result and recent progress.
- Android APKs referenced by T3 can arrive as Telegram file attachments.
- A topic can also be set up for ordinary conversation with Hermes.

## Requirements

- Hermes Agent 0.23.2 or newer with its Telegram gateway configured.
- T3 Code running on the same machine, with its CLI, provider and model available.
- Git and a repository with a commit available as a worktree base.
- A Telegram group with Topics enabled and the bot able to receive topic creation events and regular group messages. The Windows pilot used a bot configured as a group administrator.

The integration was verified on Windows with Hermes Agent 0.23.2, T3 CLI 0.1.10 and Codex.

## Install from a local checkout

1. Copy `plugin.yaml`, `__init__.py`, `projects.py`, `artifacts.py` and `settings.example.json` into `plugins/t3-bridge` under your Hermes profile. `HermesT3Bridge` is the repository name and `t3-bridge` is the plugin ID. The Windows pilot uses `%LOCALAPPDATA%\hermes` as its profile.
2. Copy `settings.example.json` to `settings.json` in that plugin directory. Set the values for your host and Telegram account.
3. Enable the plugin, validate it, and restart the gateway.

```sh
hermes plugins enable t3-bridge
hermes plugins doctor --ci t3-bridge
hermes gateway restart
```

| Setting | Value |
| --- | --- |
| `telegram_owner` | Numeric Telegram user ID allowed to send T3 requests. |
| `telegram_group_ids` | IDs of the Telegram forum groups to enable. |
| `t3_command` | Absolute path to the T3 CLI. On Windows this can point to the `t3.cmd` launcher. |
| `project_dir` | Absolute path to the default local Git repository. |
| `base_ref` | Worktree start ref for the default repository, for example `origin/main`. |
| `provider`, `model` | T3 provider instance ID and model slug. |
| `projects_root` | Optional directory to search for local repositories. It defaults to the parent of `project_dir`. |

Hermes manages the Telegram token, and T3 manages provider authentication. Keep `settings.json` local.

## Use a topic

1. As the configured owner, create a topic and reply `dev` to set up a development project or `chat` for ordinary conversation. To set up an existing topic, send `setup topic`. The bot currently displays prompts in Russian. The Russian equivalents are `разработка`, `чат` and `настроить топик`.
2. For development, send a local repository name, an absolute folder path, or a GitHub URL. For GitHub URLs, the plugin reuses a local repository with the matching origin when available. It clones missing repositories into `.hermes-github` under the projects directory. Private repositories use the host's existing Git credentials.
3. Send a task as a normal message. For example, ask the agent to find where a feature starts and identify the relevant files.
4. Reply to a result card to continue that thread. Reply with `/dev_status` for its status or `/dev_link` for its T3 link. Outside development topics, `/dev <request>` uses the default project.

Topic bindings and delivery records are stored in `t3-bridge.sqlite3` in the Hermes profile.

### APK attachments

If a completed T3 response includes a local APK as a Markdown link or an inline code path, the bot can send it as a document. The path must resolve to a file inside the run's worktree.

The hosted Telegram Bot API accepts up to 50 MB per document. Larger APKs are sent in numbered byte parts, such as `app-debug.apk.001` and `app-debug.apk.002`. The last attachment includes Windows joining instructions. Joining the parts restores the original APK bytes. [Telegram Bot API](https://core.telegram.org/bots/api#senddocument)

## Development

Run the Hermes plugin validator from this checkout.

```sh
hermes plugins doctor --ci .
```

The plugin uses the Hermes Telegram gateway to deliver messages and the T3 CLI to start worktrees, continue threads and read results. [Hermes plugins](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins/) · [T3 outside agents](https://github.com/pingdotgg/t3code/blob/main/docs/user/outside-agents.md)

[MIT License](LICENSE)
