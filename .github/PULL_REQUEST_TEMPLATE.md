**What this changes, and why**


**Checks**
- [ ] `uv run pytest` and `uv run ruff check src tests` pass
- [ ] If `web/src` changed: `pnpm typecheck && pnpm build`, and the rebuilt
      `src/sshfleet/web/static` is committed (CI fails otherwise)
- [ ] A user-visible change has a line in `CHANGELOG.md`
- [ ] Nothing in `src/` names a particular service, port or host (see CONTRIBUTING)
