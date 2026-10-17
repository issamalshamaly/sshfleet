"""A real shell in the browser: ssh in a pseudo-terminal, bridged over a WebSocket.

The same multiplexed connection the rest of the tool uses carries the session, so the prompt
appears immediately. Text goes both ways as binary frames; resize arrives as a small JSON frame.

Both directions are driven by the event loop and neither may block it. A pty holds only a few
kilobytes each way, so a paste larger than that cannot be handed over in one call: the remote
has to consume it, and it can only do that if we keep reading what it writes back meanwhile.
Writing straight through would stop us reading, which stops the remote consuming, which stops
the write from ever finishing — a deadlock that takes the whole server with it, since every
other request shares this loop. So input is buffered here and fed in as the pty accepts it.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import pty
import signal
import struct
import termios
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ...containers.quick import UnknownAction, command_for
from ...containers.runtime import ForbiddenCommand
from ...shell import ssh_command
from ..security import websocket_token_ok

router = APIRouter()


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0))


@router.websocket("/ws/terminal/{alias}")
async def terminal(websocket: WebSocket, alias: str):
    if not websocket_token_ok(websocket):
        await websocket.close(code=4401)
        return
    state = websocket.app.state.web
    context = state.context
    try:
        host = context.inventory.host(alias)
    except KeyError:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    cols = int(websocket.query_params.get("cols") or 120)
    rows = int(websocket.query_params.get("rows") or 32)

    # A quick action is named, never sent: the browser passes an id from the catalogue and the
    # command is built here, so what a session can run is what that catalogue allows. Resolved
    # after the accept so a refusal can be read in the terminal instead of failing a handshake.
    action_id = websocket.query_params.get("action") or ""
    container = websocket.query_params.get("container") or ""
    remote: str | None = None
    action = None
    if action_id:
        try:
            action, remote = command_for(host, action_id, container)
        except (UnknownAction, ForbiddenCommand) as exc:
            await websocket.send_bytes(f"\r\n\x1b[31m{exc}\x1b[0m\r\n".encode())
            await websocket.close(code=4400)
            return

    # A paused host gets no connection of any kind, a shell included.
    paused = context.inventory.paused_reason(alias)
    if paused is not None:
        await websocket.send_bytes(f"\r\n\x1b[33m{alias}: {paused}\x1b[0m\r\n".encode())
        await websocket.close(code=4409)
        return

    # Warm the connection first so the shell attaches to the shared master.
    if alias not in context.mux.alive:
        result = await context.mux.warm(
            alias, timeout=host.connect_timeout_s or context.config.refresh.connect_timeout_s
        )
        if not result.ok:
            await websocket.send_bytes(f"\r\n\x1b[31m{alias}: {result.message}\x1b[0m\r\n".encode())
            await websocket.close(code=4502)
            return

    if action is None:
        context.audit.append("ssh.open", env=host.env, host=alias, status="ok", note="web terminal")
    else:
        # A quick action runs a command on a server, so it is recorded the way the restart is:
        # who, where, and the exact line, before it runs.
        context.audit.append(
            "container.quick",
            env=host.env,
            host=alias,
            target=container,
            command=remote,
            status="started",
            note=f"web terminal · {action.id}",
        )

    started = time.monotonic()
    master_fd, slave_fd = pty.openpty()
    # Non-blocking in both directions: see the module docstring for what a blocking write costs.
    os.set_blocking(master_fd, False)
    _set_winsize(master_fd, rows, cols)
    argv = ssh_command(context.mux.ssh_config, alias, remote) if remote else ssh_command(context.mux.ssh_config, alias)
    env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")

    def _attach_tty() -> None:
        # Become a session leader ourselves, then adopt the pty by its own descriptor. Under
        # uvloop the fork hook runs preexec_fn before libuv's setsid and before stdio is wired
        # to the pty, so start_new_session=True (EPERM) and fd 0 (ENOTTY) both fail there.
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        preexec_fn=_attach_tty,
        env=env,
        close_fds=True,
    )
    os.close(slave_fd)

    loop = asyncio.get_running_loop()
    outgoing: asyncio.Queue[bytes | None] = asyncio.Queue()

    def on_readable() -> None:
        try:
            data = os.read(master_fd, 65536)
        except BlockingIOError:
            return  # readable, then not: nothing to do, and certainly not end of file
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(master_fd)
            outgoing.put_nowait(None)
            return
        outgoing.put_nowait(data)

    loop.add_reader(master_fd, on_readable)

    #: Input the pty has not taken yet. A paste arrives in one frame but leaves over many.
    pending = bytearray()

    def pump_in() -> None:
        """Hand the pty as much as it will take now, and ask to be called again for the rest."""
        while pending:
            try:
                written = os.write(master_fd, pending)
            except BlockingIOError:
                written = 0
            except OSError:
                pending.clear()
                break
            if written <= 0:
                # add_writer replaces any callback already registered for this fd.
                loop.add_writer(master_fd, pump_in)
                return
            del pending[:written]
        loop.remove_writer(master_fd)

    def feed(data: bytes) -> None:
        pending.extend(data)
        pump_in()

    async def pump_out() -> None:
        while True:
            chunk = await outgoing.get()
            if chunk is None:
                break
            await websocket.send_bytes(chunk)

    sender = asyncio.create_task(pump_out())
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            text = message.get("text")
            if data:
                feed(data)
            elif text:
                if text.startswith("{"):
                    with contextlib.suppress(json.JSONDecodeError, KeyError, ValueError):
                        control = json.loads(text)
                        if control.get("type") == "resize":
                            _set_winsize(master_fd, int(control["rows"]), int(control["cols"]))
                        continue
                feed(text.encode())
            if proc.returncode is not None:
                break
    except WebSocketDisconnect:
        pass
    finally:
        with contextlib.suppress(Exception):
            loop.remove_reader(master_fd)
        with contextlib.suppress(Exception):
            loop.remove_writer(master_fd)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGHUP)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=3)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender
        with contextlib.suppress(OSError):
            os.close(master_fd)
        with contextlib.suppress(Exception):
            await websocket.close()
        if action is not None:
            # Its start is on the record; so is how it ended, whether it ran out or was closed.
            ended = proc.returncode
            context.audit.append(
                "container.quick",
                env=host.env,
                host=alias,
                target=container,
                status="ok" if ended == 0 else "ended",
                duration_ms=int((time.monotonic() - started) * 1000),
                note=f"web terminal · {action.id}" + (f" · exit {ended}" if ended is not None else ""),
            )
