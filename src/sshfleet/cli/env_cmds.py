"""``sshfleet env …`` — register, inspect and test environments."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from .. import envsetup
from ..config import ConfigError
from ..config import loader as config_loader
from ..context import build_context
from ..inventory import build_inventory
from .output import Exit, console, emit_json, err_console, fail, info, table

app = typer.Typer(no_args_is_help=True, help="Register and inspect environments (SSH config files).")


@app.command("add")
def add(
    name: Annotated[str, typer.Option("--name", "-n", help="Environment name, e.g. production.")],
    ssh_config: Annotated[Path, typer.Option("--ssh-config", "-c", help="SSH config file listing the hosts.")],
    key: Annotated[Path | None, typer.Option("--key", "-k", help="Identity file path (recorded and stat-ed; never read).")] = None,
    short: Annotated[str | None, typer.Option("--short", help="Short label used in tables.")] = None,
    color: Annotated[str | None, typer.Option("--color", help="Colour for the environment chip.")] = None,
    bastion: Annotated[list[str] | None, typer.Option("--bastion", help="Bastion alias (repeatable). Default: auto-detect from ProxyJump.")] = None,
    project: Annotated[str | None, typer.Option("--project", help="The project it belongs to. Create it first with `sshfleet project add`.")] = None,
    replace: Annotated[bool, typer.Option("--replace", help="Overwrite an existing environment of the same name.")] = False,
    test: Annotated[bool, typer.Option("--test/--no-test", help="Open the bastion connection after saving.")] = True,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Add an environment from an SSH config file."""
    context = build_context(write_ssh_config=False)
    try:
        info_preview = envsetup.preview(ssh_config, context.config)
        env_cfg = envsetup.add_environment(
            context.config,
            name=name,
            ssh_config_path=ssh_config,
            key_path=key,
            short=short,
            color=color,
            bastions=list(bastion) if bastion else None,
            project=project,
            replace=replace,
        )
    except ConfigError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc

    key_note = None
    if key:
        check = envsetup.check_key(key)
        if not check.ok:
            key_note = f"{check.path}: {'; '.join(check.problems)} — fix with `{check.fix}`"

    config_loader.save(context.config, context.paths.config_file)
    context.remember(f"environment {name} registered")
    context.inventory = build_inventory(context.config)
    context.mux.inventory = context.inventory
    ssh_config_path = context.mux.write_ssh_config()

    hosts = [h for h in context.inventory.hosts(name)]
    bastions = context.inventory.environment(name).bastions
    result: dict[str, object] = {
        "environment": env_cfg.name,
        "hosts": len(hosts),
        "bastions": bastions,
        "alias_prefix": info_preview.prefix,
        "ssh_config": str(ssh_config_path),
        "starred": sum(1 for h in hosts if h.main),
        "key_warning": key_note,
    }

    if test and bastions:
        results = asyncio.run(context.mux.warm_bastions(name))
        result["bastion_test"] = {a: {"ok": r.ok, "error": r.message, "seconds": round(r.elapsed, 2)} for a, r in results.items()}

    context.audit.append("env.add", env=name, target=str(ssh_config), status="ok")

    # Registered either way; but a test that could not reach a bastion is a failed test, and a
    # script running `env add --test` has to be able to tell.
    tested = result.get("bastion_test")
    unreachable = isinstance(tested, dict) and any(not outcome["ok"] for outcome in tested.values())

    if as_json:
        emit_json(result, ok=not unreachable)
        raise typer.Exit(Exit.FAILED if unreachable else Exit.OK)

    console.print(f"[green]registered[/green] environment [bold]{name}[/bold]: {len(hosts)} hosts, "
                  f"prefix {info_preview.prefix or '(none)'}, bastions: {', '.join(bastions) or '(none)'}")
    console.print(f"generated ssh config: {ssh_config_path}")
    if key_note:
        err_console.print(f"[yellow]key warning[/yellow] {key_note}")
    test_results = result.get("bastion_test")
    if isinstance(test_results, dict):
        for alias, outcome in test_results.items():
            mark = "[green]ok[/green]" if outcome["ok"] else f"[red]{outcome['error']}[/red]"
            console.print(f"  bastion {alias}: {mark} ({outcome['seconds']}s)")
    if unreachable:
        raise typer.Exit(Exit.FAILED)


@app.command("preview")
def preview_cmd(
    ssh_config: Annotated[Path, typer.Argument(help="SSH config file to inspect.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Parse an SSH config file and show what registering it would produce."""
    context = build_context(write_ssh_config=False)
    try:
        result = envsetup.preview(ssh_config, context.config)
    except (ConfigError, FileNotFoundError) as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc

    if as_json:
        emit_json(
            {
                "hosts": result.aliases,
                "summary": result.summary,
                "prefix": result.prefix,
                "bastions": result.bastions,
                "suggested": {"name": result.suggested_name, "short": result.suggested_short, "color": result.suggested_color},
                "identity_files": result.key_candidates,
            }
        )
        return

    summary = result.summary
    console.print(
        f"[bold]{ssh_config}[/bold]: {summary['hosts']} hosts, prefix {result.prefix or '(none)'}, "
        f"{summary['without_identity_file']} without IdentityFile"
    )
    console.print(f"bastions: {', '.join(result.bastions) or '(none)'}")
    console.print(f"users: {summary['users']}")
    console.print(f"identity files referenced: {', '.join(result.key_candidates) or '(none)'}")
    rows = [[alias] for alias in result.aliases]
    console.print(table("Hosts", ["alias"], rows))
    console.print(f"suggested: name={result.suggested_name} short={result.suggested_short} color={result.suggested_color}")


@app.command("list")
def list_envs(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List registered environments."""
    context = build_context(write_ssh_config=False)
    data = []
    for env in context.config.environments:
        try:
            hosts = context.inventory.hosts(env.name)
            bastions = context.inventory.environment(env.name).bastions
        except KeyError:
            hosts, bastions = [], []
        data.append(
            {
                "name": env.name,
                "short": env.short,
                "color": env.color,
                "ssh_config": str(env.ssh_config_path),
                "key_path": str(env.key_path) if env.key_path else None,
                "project": env.project,
                "hosts": len(hosts),
                "bastions": bastions,
            }
        )

    if as_json:
        emit_json(data)
        return
    if not data:
        info("no environments registered — add one with `sshfleet env add --name … --ssh-config …`")
        return
    console.print(
        table(
            "Environments",
            ["name", "short", "project", "hosts", "bastions", "ssh config"],
            [[d["name"], d["short"], d["project"], d["hosts"], ", ".join(d["bastions"]), _home_relative(d["ssh_config"])] for d in data],
        )
    )


def _home_relative(path: str) -> str:
    """``~/.ssh/config.d/prod`` rather than the full home path, which pushed the other columns
    into a sliver of the table."""
    home = str(Path.home())
    return "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path


@app.command("test")
def test_env(
    name: Annotated[str, typer.Argument(help="Environment name.")],
    host: Annotated[str | None, typer.Option("--host", help="Also test this specific host alias.")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Open the bastion connections for an environment and report what ssh resolved."""
    context = build_context()
    try:
        environment = context.inventory.environment(name)
    except KeyError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc

    async def run() -> dict[str, object]:
        results = await context.mux.warm_bastions(environment.name)
        payload: dict[str, object] = {
            "bastions": {a: {"ok": r.ok, "error": r.message, "seconds": round(r.elapsed, 2)} for a, r in results.items()}
        }
        target = host or next((h.alias for h in environment.hosts if h.proxy_jump), None)
        if target:
            outcome = await context.mux.warm(target)
            payload["host"] = {
                "alias": target,
                "ok": outcome.ok,
                "error": outcome.message,
                "seconds": round(outcome.elapsed, 2),
            }
        return payload

    payload = asyncio.run(run())
    ok = all(b["ok"] for b in payload["bastions"].values()) if payload.get("bastions") else False  # type: ignore[union-attr]

    if as_json:
        emit_json(payload, ok=ok)
        raise typer.Exit(Exit.OK if ok else Exit.FAILED)

    for alias, outcome in payload["bastions"].items():  # type: ignore[union-attr]
        mark = "[green]ok[/green]" if outcome["ok"] else f"[red]{outcome['error']}[/red]"
        console.print(f"bastion {alias}: {mark} ({outcome['seconds']}s)")
    host_result = payload.get("host")
    if isinstance(host_result, dict):
        mark = "[green]ok[/green]" if host_result["ok"] else f"[red]{host_result['error']}[/red]"
        console.print(f"host {host_result['alias']}: {mark} ({host_result['seconds']}s)")
    raise typer.Exit(Exit.OK if ok else Exit.FAILED)


@app.command("remove")
def remove(
    name: Annotated[str, typer.Argument(help="Environment name.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Remove an environment from the config (SSH config files are never touched)."""
    context = build_context(write_ssh_config=False)
    if not yes:
        typer.confirm(f"Remove environment {name!r} from sshfleet config?", abort=True)
    try:
        envsetup.remove_environment(context.config, name)
    except ConfigError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc
    config_loader.save(context.config, context.paths.config_file)
    context.remember(f"environment {name} removed")
    context.inventory = build_inventory(context.config)
    context.mux.inventory = context.inventory
    context.mux.write_ssh_config()
    context.audit.append("env.remove", env=name, status="ok")
    console.print(f"[green]removed[/green] environment {name}")
