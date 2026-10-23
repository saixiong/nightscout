"""Command line: init, run, serve, setup, schedule. Thin on purpose; the work lives in core."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .core.config import load_config, save_config, starter_config
from .core.paths import default_config_path


def _pretty(p: Path | str) -> str:
    """~ instead of the home directory, so pasted output doesn't leak usernames."""
    s, home = str(p), str(Path.home())
    return "~" + s[len(home) :] if s.startswith(home) else s


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_init(args: argparse.Namespace) -> int:
    path: Path = args.config
    if path.exists() and not args.force:
        print(f"Config already exists at {_pretty(path)}. Use --force to overwrite.")
        cfg = load_config(path)
    else:
        projects = [str(Path(p).expanduser().resolve()) for p in args.project]
        cfg = starter_config(projects)
        save_config(cfg, path)
        print(f"Wrote {_pretty(path)}. Edit it to choose your sources and projects.")
    if args.no_profile:
        return 0
    from .core.profile import build_profile_file

    if not cfg.projects:
        print(
            "No projects listed yet; add some under `projects:` and run "
            "`night-scout profile` to build your profile."
        )
        return 0
    out = build_profile_file(cfg)
    print(f"Wrote profile to {_pretty(out)}. It's plain YAML; edit it freely.")
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    from .core.profile import build_profile_file

    cfg = load_config(args.config)
    out = build_profile_file(cfg)
    print(f"Rebuilt profile at {_pretty(out)}.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .core.pipeline import run_once

    cfg = load_config(args.config)
    result = run_once(cfg, fetch_only=args.fetch_only)
    print()
    for line in result.status_lines:
        print("  " + line)
    if result.digest_path:
        print(f"\nDigest: {_pretty(result.digest_path)}")
    return 0 if result.ok else 1


def _port_free(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Like uvicorn: connections left in TIME_WAIT by a server that just stopped don't
        # count. Only a live listener makes the bind fail.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _serve(config_path: Path, port: int, open_browser: bool, page: str = "") -> int:
    import secrets
    import webbrowser

    import uvicorn

    from .web.app import create_app

    if not config_path.exists():
        # First launch: a starter config (no projects yet), so the web app can open Setup.
        save_config(starter_config([]), config_path)
        print(f"First launch: wrote a starter config to {_pretty(config_path)}.")
        page = page or "#/setup"
    if not _port_free(port):
        print(
            f"Something is already running on port {port}. If it's Night Scout, open its tab "
            "and go to Settings → Setup, or stop it and run this again "
            "(or pick another port with --port)."
        )
        return 1
    cfg = load_config(config_path)
    key = secrets.token_urlsafe(24)
    app = create_app(cfg, config_path, port=port, session_key=key)
    url = f"http://127.0.0.1:{port}/?key={key}{page}"
    what = "setup" if page == "#/setup" else "Night Scout"
    print(f"Opening {what}. This link signs your browser in:\n\n  {url}\n", flush=True)
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    return _serve(args.config, args.port, not args.no_browser)


def cmd_setup(args: argparse.Namespace) -> int:
    """The web app, opened on the setup walkthrough (current settings filled in)."""
    return _serve(args.config, args.port, not args.no_browser, page="#/setup")


def cmd_schedule(args: argparse.Namespace) -> int:
    from .core.schedule import describe, install, uninstall

    cfg = load_config(args.config)
    if args.uninstall:
        print(uninstall())
        return 0
    if args.install:
        print(install(cfg, args.config))
        return 0
    print(describe(cfg, args.config, show_plist=args.show_plist))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="night-scout",
        description="Reads the feeds you'd doom-scroll overnight and leaves you next steps.",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="path to config.yaml (default: %(default)s)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write a starter config, scan projects, build the profile")
    s.add_argument("project", nargs="*", help="project directories to scout for")
    s.add_argument("--force", action="store_true", help="overwrite an existing config")
    s.add_argument("--no-profile", action="store_true", help="skip building the profile")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("profile", help="rebuild profile.yaml from your project directories")
    s.set_defaults(func=cmd_profile)

    s = sub.add_parser("run", help="one nightly pass: fetch, judge, propose, write the digest")
    s.add_argument("--fetch-only", action="store_true", help="fetch and store, skip the model")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("serve", help="local web UI on 127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("setup", help="open the setup walkthrough in the web UI (run it any time)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("schedule", help="print (or install) the nightly job")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true", help="install the job for this user")
    g.add_argument("--uninstall", action="store_true", help="remove the installed job")
    s.add_argument("--show-plist", action="store_true", help="macOS: print the launchd plist")
    s.set_defaults(func=cmd_schedule)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
