"""Merge one SSH config file's host stanzas into another, a stanza at a time.

An import brings a teammate's hosts into the operator's own file. Overwriting the file would take
the operator's comments, ordering, local-only hosts and ``IdentityFile`` lines with it, so the
merge works per stanza instead:

- a stanza is identified by its exact ``Host`` patterns;
- one that is not here yet is added (above the trailing ``Host *`` block, as ``add_host`` does);
- one that is here and says the same is left byte for byte;
- one that is here and differs keeps the local version unless the bundle is preferred, and then
  its body is replaced while its comments and its ``IdentityFile`` lines stay;
- one whose patterns only partly overlap a local stanza is a conflict, reported and not applied;
- wildcard stanzas and ``Match`` blocks apply to many hosts at once and are never touched.

In ``replace`` mode the incoming text becomes the file, but each host keeps its local
``IdentityFile`` lines: keys are this machine's business, and a bundle never carries them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .sshedit import _split

WILDCARD_CHARS = "*?!"
#: Keywords that describe this machine, not the host: never compared, and kept from the local file.
LOCAL_KEYWORDS = frozenset({"identityfile", "certificatefile"})


@dataclass(frozen=True, slots=True)
class Stanza:
    start: int  # its first own line: the comments directly above the Host line are its own
    head: int  # the Host or Match line
    end: int  # one past its last own line
    keyword: str  # "host" or "match"
    patterns: tuple[str, ...]

    @property
    def is_shared(self) -> bool:
        """A wildcard or Match block: it applies to many hosts and is left to the operator."""
        return self.keyword == "match" or any(ch in pattern for pattern in self.patterns for ch in WILDCARD_CHARS)


@dataclass(slots=True)
class StanzaChange:
    patterns: tuple[str, ...]
    #: "add", "update", "same", "kept-local", "conflict", "skipped" or "remove".
    action: str
    note: str = ""

    @property
    def label(self) -> str:
        return " ".join(self.patterns)


def _lines(text: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return lines


def split_stanzas(lines: list[str]) -> tuple[list[str], list[Stanza]]:
    """The lines before the first stanza, and every stanza with the lines it owns."""
    heads = [(i, *_split(line)) for i, line in enumerate(lines) if _split(line)[0] in {"host", "match"}]
    starts: list[int] = []
    for k, (head, _keyword, _value) in enumerate(heads):
        floor = heads[k - 1][0] + 1 if k else 0
        start = head
        while start > floor and lines[start - 1].strip().startswith("#"):
            start -= 1
        starts.append(start)
    stanzas = [
        Stanza(
            start=starts[k],
            head=head,
            end=starts[k + 1] if k + 1 < len(heads) else len(lines),
            keyword=keyword,
            patterns=tuple(value.split()),
        )
        for k, (head, keyword, value) in enumerate(heads)
    ]
    return lines[: starts[0] if starts else len(lines)], stanzas


def normalised(block: list[str], ignore: frozenset[str] = LOCAL_KEYWORDS) -> tuple[tuple[str, str], ...]:
    """What a stanza says, ignoring comments, blank lines, layout, order and local keywords."""
    out = []
    for line in block:
        keyword, value = _split(line)
        if keyword and keyword not in ignore:
            out.append((keyword, " ".join(value.split())))
    return tuple(sorted(out))


def _trailing_blank(block: list[str]) -> int:
    count = 0
    for line in reversed(block):
        if line.strip():
            break
        count += 1
    return count


def _body(block: list[str]) -> list[str]:
    """A stanza without the blank lines at its end."""
    return block[: len(block) - _trailing_blank(block)] if _trailing_blank(block) < len(block) else []


def _with_local(incoming: list[str], incoming_head: int, local: list[str], local_head: int, keep: frozenset[str]) -> list[str]:
    """The incoming stanza's Host line and body under the local stanza's own comments, with the
    local lines for ``keep`` keywords in place of any the incoming one had."""
    lead = local[:local_head]
    body = [line for line in _body(incoming[incoming_head:]) if _split(line)[0] not in keep]
    kept = [line for line in local[local_head + 1 :] if _split(line)[0] in keep]
    return lead + body + kept + ["\n"] * _trailing_blank(local)


def _first_shared_start(lines: list[str]) -> int | None:
    """Where the first wildcard stanza (with its comments) begins: new hosts go above it."""
    _, stanzas = split_stanzas(lines)
    return next((s.start for s in stanzas if s.keyword == "host" and s.is_shared), None)


def _insert(lines: list[str], block: list[str]) -> list[str]:
    block = _body(block)
    cut = _first_shared_start(lines)
    if cut is None:
        separator = ["\n"] if lines and lines[-1].strip() else []
        return lines + separator + block
    before = lines[:cut]
    if before and before[-1].strip():
        before = [*before, "\n"]
    return before + block + ["\n"] + lines[cut:]


def merge_text(
    local: str,
    incoming: str,
    *,
    mode: str = "merge",
    prefer: str = "local",
    keep_local: frozenset[str] = LOCAL_KEYWORDS,
) -> tuple[str, list[StanzaChange]]:
    """The local file with the incoming stanzas merged in, and what happened to each."""
    if mode not in {"merge", "replace"}:
        raise ValueError(f"unknown merge mode {mode!r}")
    if prefer not in {"local", "bundle"}:
        raise ValueError(f"unknown preference {prefer!r}")
    ours = _lines(local)
    theirs = _lines(incoming)
    _, local_stanzas = split_stanzas(ours)
    _, incoming_stanzas = split_stanzas(theirs)
    by_patterns = {s.patterns: s for s in local_stanzas if s.keyword == "host"}
    by_alias = {p: s for s in local_stanzas if s.keyword == "host" for p in s.patterns}
    changes: list[StanzaChange] = []

    def block(lines: list[str], s: Stanza) -> list[str]:
        return lines[s.start : s.end]

    if mode == "replace":
        out = list(theirs)
        for s in reversed(incoming_stanzas):  # bottom up, so earlier indexes stay valid
            mine = by_patterns.get(s.patterns) if s.keyword == "host" else None
            if mine is None:
                changes.append(StanzaChange(s.patterns, "add"))
                continue
            theirs_block = block(theirs, s)
            same = normalised(block(ours, mine)) == normalised(theirs_block)
            changes.append(StanzaChange(s.patterns, "same" if same else "update"))
            kept = [line for line in ours[mine.head + 1 : mine.end] if _split(line)[0] in keep_local]
            if kept:
                body = [line for line in _body(theirs_block) if _split(line)[0] not in keep_local]
                out[s.start : s.end] = body + kept + ["\n"] * _trailing_blank(theirs_block)
        changes.reverse()
        incoming_patterns = {s.patterns for s in incoming_stanzas}
        changes.extend(StanzaChange(s.patterns, "remove") for s in local_stanzas if s.patterns not in incoming_patterns)
        return "".join(out), changes

    replacements: list[tuple[Stanza, list[str]]] = []
    additions: list[list[str]] = []
    for s in incoming_stanzas:
        theirs_block = block(theirs, s)
        if s.is_shared:
            twin = next((m for m in local_stanzas if m.keyword == s.keyword and m.patterns == s.patterns), None)
            if twin is not None and normalised(block(ours, twin)) == normalised(theirs_block):
                changes.append(StanzaChange(s.patterns, "same"))
            else:
                changes.append(StanzaChange(s.patterns, "skipped", "applies to many hosts: left for you to add by hand"))
            continue
        mine = by_patterns.get(s.patterns)
        if mine is not None:
            if normalised(block(ours, mine)) == normalised(theirs_block):
                changes.append(StanzaChange(s.patterns, "same"))
            elif prefer == "bundle":
                replacements.append(
                    (mine, _with_local(theirs_block, s.head - s.start, block(ours, mine), mine.head - mine.start, keep_local))
                )
                changes.append(StanzaChange(s.patterns, "update"))
            else:
                changes.append(StanzaChange(s.patterns, "kept-local", "differs here; the local version is kept"))
            continue
        overlapping = sorted({" ".join(by_alias[p].patterns) for p in s.patterns if p in by_alias})
        if overlapping:
            changes.append(StanzaChange(s.patterns, "conflict", f"shares a name with the local stanza `Host {overlapping[0]}`"))
            continue
        additions.append([line for line in theirs_block if _split(line)[0] not in keep_local])
        changes.append(StanzaChange(s.patterns, "add"))

    out = list(ours)
    for mine, new_block in sorted(replacements, key=lambda item: item[0].start, reverse=True):
        out[mine.start : mine.end] = new_block
    for new_block in additions:
        out = _insert(out, new_block)
    return "".join(out), changes
