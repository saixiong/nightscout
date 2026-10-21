"""Command line: init, run, serve, schedule. Thin on purpose; the work lives in core."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .core.config import load_config, save_config, starter_config
from .core.paths import default_config_path


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
        print(f"Config already exists at {path}. Use --force to overwrite.")
        cfg = load_config(path)
    else:
        projects = [str(Path(p).expanduser().resolve()) for p in args.project]
        cfg = starter_config(projects)
        save_config(cfg, path)
        print(f"Wrote {path}. Edit it to choose your sources and projects.")
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
    print(f"Wrote profile to {out}. It's plain YAML; edit it freely.")
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    from .core.profile import build_profile_file

    cfg = load_config(args.config)
    out = build_profile_file(cfg)
    print(f"Rebuilt profile at {out}.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .core.pipeline import run_once

    cfg = load_config(args.config)
    result = run_once(cfg, fetch_only=args.fetch_only)
    print()
    for line in result.status_lines:
        print("  " + line)
    if result.digest_path:
        print(f"\nDigest: {result.digest_path}")
    return 0 if result.ok else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import secrets
    import webbrowser

    import uvicorn

    from .web.app import create_app

    cfg = load_config(args.config)
    key = secrets.token_urlsafe(24)
    app = create_app(cfg, args.config, port=args.port, session_key=key)
    url = f"http://127.0.0.1:{args.port}/?key={key}"
    print(f"Night Scout is running. Open this link (it signs your browser in):\n\n  {url}\n")
    if not args.no_browser:
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    from .core.schedule import describe, install, uninstall

    cfg = load_config(args.config)
    if args.uninstall:
        print(uninstall())
        return 0
    if args.install:
        print(install(cfg, args.config))
        return 0
    print(describe(cfg, args.config))
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

    s = sub.add_parser("schedule", help="print (or install) the nightly job")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true", help="install the job for this user")
    g.add_argument("--uninstall", action="store_true", help="remove the installed job")
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
