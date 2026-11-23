import argparse
from pathlib import Path
import sys

from .pipeline import generate_issue, load_config, prepare
from .publish import build, deliver


def main():
    parser = argparse.ArgumentParser(description="Morning digest: GitHub Actions + Pages")
    parser.add_argument("command", choices=["check", "prepare", "generate", "run", "build", "deliver", "instant-view"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--force", action="store_true", help="Create an additional edition today")
    parser.add_argument("--date", help="Calendar day YYYY-MM-DD; historical runs never send Telegram")
    parser.add_argument("--response-file", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.command == "check":
            config = load_config(root)
            print(f"Configuration valid: {len(config['sources'])} sources, provider={config['provider']}.")
        if args.command in {"prepare", "run"}:
            prepare(root, scheduled=args.scheduled, force=args.force, date=args.date)
        if args.command in {"generate", "run"}:
            generate_issue(root, response_file=args.response_file)
        if args.command in {"build", "run"}:
            build(root)
        if args.command == "instant-view":
            from .instant_view import publish
            publish(root)
        if args.command == "deliver":
            deliver(root)
    except (ValueError, RuntimeError, KeyError, OSError) as error:
        print(f"Digest failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
