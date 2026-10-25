# OpenClaw workspace files for the fleet-analyst agent

These files are injected into the agent's system prompt on every turn
(OpenClaw reads them from `~/.openclaw/workspace/`). They replace the generic
onboarding persona with short, specific instructions.

Why the instructions live in AGENTS.md and not only in the skill:
the agent has no `read` tool (on purpose: it must not read config tokens or keys),
and in testing the local model never used the `fleet-analytics` skill. It invented
`sqlite3 .../fleet.db` instead, which the exec allowlist blocked. AGENTS.md is
always in the prompt, so the model sees the one command it may run.

Install (back up the generated files first):

```bash
mkdir -p ~/.openclaw/workspace-backup
cp ~/.openclaw/workspace/*.md ~/.openclaw/workspace-backup/
cp ~/openclaw-drone-analytics/openclaw/workspace/{AGENTS,SOUL,IDENTITY,USER}.md ~/.openclaw/workspace/
```

The repo copy is the source of truth; copy again after editing.
