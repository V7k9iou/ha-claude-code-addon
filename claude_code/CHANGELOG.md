# Changelog

## 0.1.0 — initial release

- OpenSSH server (port 22222 by default) with password and key-based auth
- Claude Code installed via npm; `linux-arm64` and `linux-x64` supported
- Persistent SSH host keys in `/data/ssh/`
- Persistent Claude config in `/data/claude/` (`CLAUDE_CONFIG_DIR`)
- HA Core REST + Supervisor API access (`hassio_role: manager`)
- Helper scripts: `ha-state`, `ha-call-service`, `ha-list-entities`,
  `ha-check-config`, `ha-restart`, `ha-token`
- `/workspace/` landing dir with symlinks to all mapped HA paths
- `/workspace/CLAUDE.md` operator briefing for Claude Code
- AppArmor disabled by default (TODO: ship a tightened profile)
