# Home Assistant Add-on: Claude Code

SSH into a long-running container that has [Claude
Code](https://www.claude.com/product/claude-code) installed, with broad
access to your Home Assistant configuration and Supervisor API. Use Claude
as an interactive operator for your HA instance — creating automations and
scenes, querying device state, generating reports, debugging integrations.

> ⚠️ This add-on grants the SSH operator (and Claude Code, acting on the
> operator's behalf) extensive access to your Home Assistant configuration
> and Supervisor. **Single-trusted-operator setups only.**

## What this gives you

- An OpenSSH server you can connect to from any machine
- Claude Code installed and ready to run (`claude` from the shell)
- Persistent login state across add-on updates (`/data/claude/`)
- Persistent SSH host keys across add-on updates (`/data/ssh/`)
- Helper scripts for the common HA REST calls: `ha-state`,
  `ha-call-service`, `ha-list-entities`, `ha-check-config`, `ha-restart`,
  `ha-token`
- A `/workspace/` landing area with symlinks to all mapped HA directories
- A `CLAUDE.md` operator briefing that gives Claude standing context about
  the HA environment, common workflows, and safety rules

See [`DOCS.md`](DOCS.md) for full setup instructions.
