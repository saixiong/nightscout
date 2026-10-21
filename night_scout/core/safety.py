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


_SH = r"(?:ba|z|da|k|fi)?sh|python[0-9.]*|perl|ruby|node|bun|deno"
_DL = r"(?:curl|wget|fetch|iwr|invoke-webrequest|aria2c|http)"
_TOK = r"(?:^|[\s;&|(`'\"=])"  # start of a shell token
RULES: tuple[Rule, ...] = (
    _r(
        "sudo",
        rf"{_TOK}(?:sudo|doas|pkexec|run0)(?=[\s'\"]|$)|(?:^|[;&|]\s*)su(?:\s|$)",
        "asks for root; never needed to try a tool",
    ),
    _r(
        "pipe-to-shell",
        rf"\b{_DL}\b.*\|\s*(?:sudo\s+)?(?:{_SH})\b",
        "pipes a download into a shell (curl | sh)",
    ),
    _r(
        "download-then-run",
        rf"\b{_DL}\b.*(?:&&|;|\|\|)\s*(?:(?:{_SH})\s+\S|\./|source\s|\.\s+\S|chmod\s)",
        "downloads a script and then runs it",
    ),
    _r(
        "shell-from-download",
        rf"(?:{_SH})\s+(?:-c\s+)?[\"']?(?:<\(|\$\()\s*{_DL}\b",
        "runs a downloaded script without showing it",
    ),
    _r(
        "decode-to-shell",
        rf"base64\s+(?:-d|-D|--decode)[^|]*\|\s*(?:{_SH})\b",
        "decodes hidden text and runs it",
    ),
    _r("eval", r"(?:^|[\s;&|])eval\s", "eval hides what actually runs"),
    _r(
        "credentials",
        r"\.ssh\b|\.aws\b|\.gnupg|\.netrc|\.pypirc|\.npmrc|\.docker/config|\.kube/config|"
        r"\.config/gcloud|\.git-credentials|\bid_rsa|\bid_ed25519|/etc/shadow|/etc/passwd|"
        r"Library/Keychains|\bsecurity\s+(?:find|dump)-|"
        r"(?:^|[\s/=\"'])\.env(?:\.[\w-]+)?(?=$|[\s\"';|>])",
        "touches credential or secret files",
    ),
    _r(
        "home-or-user-paths",
        r"(?:^|[\s=\"'(])(?:~|\$HOME\b|\$\{HOME\}|/Users/|/home/|/root\b|[A-Za-z]:\\\\Users)",
        "reaches into your home directory or other users' files",
    ),
    _r("parent-dir", r"(?:^|[\s=\"'/])\.\.(?:/|\\|\s|$)", "climbs out of the sandbox directory"),
    _r(
        "cd-outside",
        r"\b(?:cd|pushd)\s+[\"']?(?:/|~|\$)",
        "moves to a directory outside the sandbox",
    ),
    _r("find-outside", r"\bfind\s+[\"']?(?:/|~|\$)", "searches outside the sandbox"),
    _r(
        "global-install",
        r"\b(?:npm|pnpm|yarn|bun)\b[^;&|]*(?:\s-g\b|\s--global\b|--location[= ]global)|"
        r"\byarn\s+global\b|"
        r"\b(?:pip[0-9.]*|uv\s+pip)\s+install\b[^;&|]*\s(?:--user|--system|"
        r"--break-system-packages|--prefix|--root|--target|-t)\b|"
        r"\b(?:brew|apt|apt-get|yum|dnf|pacman|zypper|choco|winget|scoop|nix-env)\s+\S|"
        r"\b(?:port|snap)\s+install\b|\bapk\s+add\b|"
        r"\b(?:pipx|uv\s+tool|cargo|go|gem)\s+install\b",
        "installs system-wide or into your user account instead of the sandbox",
    ),
    _r(
        "system-change",
        r"\b(?:launchctl|systemctl|crontab|defaults\s+write|osascript|mkfs|diskutil|shutdown|"
        r"reboot|chown|setx|reg\s+add|schtasks|open\s+-a)\b|\bdd\s+if=|"
        r">\s*/dev/(?!null|stdout|stderr)|:\(\)\s*\{",
        "changes system settings, services, apps or disks",
    ),
    _r("chmod-wide", r"\bchmod\s+(?:-R\s+)?(?:777|a\+w|o\+w)", "makes files world-writable"),
    _r(
        "upload",
        r"\bcurl\b[^;&|]*\s(?:-d|--data\S*|-F|--form|-T|--upload-file)\s|"
        r"\b(?:nc|ncat|netcat|socat|scp|sftp|ssh|rsync|telnet)\s",
        "sends data off the machine or opens a remote shell",
    ),
    _r(
        "local-services",
        r"\b(?:localhost|127\.\d{1,3}\.\d{1,3}\.\d{1,3}|0\.0\.0\.0|host\.docker\.internal)\b|"
        r"\[::1?\]|169\.254\.169\.254",
        "talks to services on your machine or network",
    ),
    _r("env-dump", r"\b(?:printenv|env)\s*(?:$|[|>])|\bset\s*\|", "dumps environment variables"),
    _r(
        "background",
        r"\bnohup\b|\bdisown\b|\bsetsid\b|\bdaemonize\b|\s--daemon\b|\s--detach\b|"
        r"\s-d\s+--rm\b|(?<![&>|0-9])&(?![&>])",
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
            if (
                target.startswith(("/", "~", "$"))
                or ".." in target
                or target in ("*", ".", "./", "-")
                or "night-scout" in target
            ):
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
