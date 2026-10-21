"""Command denylist for Try-it plans. Generated commands are checked before they're shown
as runnable, and again right before they run. A hit refuses the whole plan, with reasons.

This is a guard rail, not a sandbox: the commands still only ever run inside a
throwaway container or temp dir, after the user confirms.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    why: str


def _r(name: str, pattern: str, why: str) -> Rule:
    return Rule(name, re.compile(pattern, re.I), why)


_SH = r"(?:ba|z|da|k|fi)?sh|python[0-9.]*|perl|ruby|node"
RULES: tuple[Rule, ...] = (
    _r(
        "sudo",
        r"(?:^|[\s;&|(`])(?:sudo|doas|su)(?=\s|$)",
        "asks for root; never needed to try a tool",
    ),
    _r(
        "pipe-to-shell",
        rf"(?:curl|wget|fetch|iwr|invoke-webrequest)\b[^|;&]*\|\s*(?:sudo\s+)?(?:{_SH})\b",
        "pipes a download straight into a shell (curl | sh)",
    ),
    _r(
        "shell-from-download",
        rf"(?:{_SH})\s+(?:-c\s+)?[\"']?(?:<\(|\$\()\s*(?:curl|wget)",
        "runs a downloaded script without showing it",
    ),
    _r(
        "decode-to-shell",
        rf"base64\s+(?:-d|--decode)[^|]*\|\s*(?:{_SH})\b",
        "decodes hidden text and runs it",
    ),
    _r("eval", r"(?:^|[\s;&|])eval\s", "eval hides what actually runs"),
    _r(
        "credentials",
        r"(?:\.ssh|\.aws|\.gnupg|\.netrc|\.pypirc|\.npmrc|\.docker/config|\.kube/config|"
        r"\.config/gcloud|\.git-credentials|id_rsa|id_ed25519|/etc/shadow|/etc/passwd|"
        r"keychain|\.env\b)",
        "touches credential or secret files",
    ),
    _r(
        "home-or-user-paths",
        r"(?:^|[\s=\"'(])(?:~/|~(?=\s|$)|\$HOME|\$\{HOME\}|/Users/|/home/|[A-Za-z]:\\\\Users)",
        "reaches into your home directory or other users' files",
    ),
    _r("parent-dir", r"(?:^|[\s=\"'/])\.\.(?:/|\\|\s|$)", "climbs out of the sandbox directory"),
    _r(
        "global-install",
        r"\b(?:npm|pnpm|yarn)\s+(?:i|install|add)\b[^;&|]*\s(?:-g|--global)\b|"
        r"\bpip[0-9.]*\s+install\b[^;&|]*\s(?:--user|--break-system-packages)\b|"
        r"\b(?:brew|apt|apt-get|yum|dnf|pacman|zypper|apk|port|choco|winget|scoop|snap)\s|"
        r"\b(?:pipx|uv\s+tool|cargo|go|gem)\s+install\b",
        "installs system-wide or into your user account instead of the sandbox",
    ),
    _r(
        "system-change",
        r"\b(?:launchctl|systemctl|crontab|defaults\s+write|osascript|mkfs|diskutil|shutdown|"
        r"reboot|chown|setx|reg\s+add|schtasks)\b|\bdd\s+if=|>\s*/dev/(?!null)|:\(\)\s*\{",
        "changes system settings, services or disks",
    ),
    _r("chmod-wide", r"\bchmod\s+(?:-R\s+)?(?:777|a\+w|o\+w)", "makes files world-writable"),
    _r(
        "upload",
        r"\bcurl\b[^;&|]*\s(?:-d|--data\S*|-F|--form|-T|--upload-file)\s|"
        r"\b(?:nc|ncat|netcat|scp|sftp|ssh|rsync)\s",
        "sends data off the machine or opens a remote shell",
    ),
    _r("env-dump", r"\b(?:printenv|env)\s*(?:$|[|>])|\bset\s*\|", "dumps environment variables"),
    _r(
        "background",
        r"\bnohup\b|\bdisown\b|(?<![&])&\s*$",
        "leaves processes running after the test",
    ),
)


@dataclass
class Verdict:
    ok: bool
    problems: list[str]

    def explain(self) -> str:
        if self.ok:
            return "No denylisted patterns found."
        return "Refused: " + "; ".join(self.problems)


def _rm_outside(cmd: str) -> bool:
    """`rm -r`/`rm -f` aimed at an absolute path, home, a parent dir or a bare glob."""
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        return "rm " in cmd  # unparseable and mentions rm: refuse
    for i, tok in enumerate(tokens):
        if tok.rsplit("/", 1)[-1] != "rm":
            continue
        args = []
        for t in tokens[i + 1 :]:
            if t in (";", "&&", "||", "|"):
                break
            args.append(t)
        flags = "".join(a.lstrip("-") for a in args if a.startswith("-"))
        if not any(f in flags for f in "rRf") and "recursive" not in flags:
            continue
        for target in (a for a in args if not a.startswith("-")):
            if target.startswith(("/", "~", "$")) or ".." in target or target in ("*", ".", "./"):
                return True
    return False


def check_command(cmd: str) -> list[str]:
    problems: list[str] = []
    if _rm_outside(cmd):
        problems.append(
            f"`{cmd.strip()[:120]}` recursive/forced delete that can reach outside the "
            "sandbox [rm-outside]"
        )
    for rule in RULES:
        if rule.pattern.search(cmd):
            problems.append(f"`{cmd.strip()[:120]}` {rule.why} [{rule.name}]")
    return problems


def check_commands(cmds: list[str]) -> Verdict:
    problems: list[str] = []
    for c in cmds:
        if not c.strip():
            continue
        if "\n" in c.strip():
            problems.append(f"`{c.strip()[:60]}…` has embedded newlines; one command per step")
            continue
        problems.extend(check_command(c))
    return Verdict(ok=not problems, problems=problems)
