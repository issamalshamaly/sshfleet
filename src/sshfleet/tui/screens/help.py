"""Key bindings and what the tool will and will not do."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

HELP = """\
Fleet
  enter    host detail            s  shell on the host        S  shell in a new tab
  l        logs for this host     f  browse files             x  containers (restart)
  r        refresh this host      R  refresh everything       t  tunnels
  /        filter                 a  alerts only              0/1/2  all / first / second env
  j k g G  move                   ?  help                     q  quit

Host detail
  tabs: System, Containers, Alerts
  x  restart the selected container    l  logs    s  shell    f  files    r  refresh

Log search
  /        pattern                 enter  run        ctrl+r  re-run       esc  back
  d        write a bundle          m      main services       c  counts panel
  w        wrap                    t      UTC / local time

Files
  enter    open or download        d  download        g  go to a path
  .        show hidden             backspace  parent directory

What sshfleet will not do
  It never reads a private key: authentication is handled by ssh itself.
  It never downloads a file matching *.env*, *.pem, *.key or similar.
  It only ever restarts a container in place. There is no stop, remove, kill or prune.
  Every restart, upload, download and tunnel change is written to the audit log
  (sshfleet audit).
"""


class HelpScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "close"),
        Binding("q", "app.pop_screen", "close", show=False),
        Binding("question_mark", "app.pop_screen", "close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-body"):
            yield Static(Text(HELP))
        yield Footer()
