#!/usr/bin/env python3
"""Bundle/new-target installer and read-only deployment planner; no production apply."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.common import HarnessError
from harness import release


def main():
    parser = argparse.ArgumentParser(); subs = parser.add_subparsers(dest='command', required=True)
    package = subs.add_parser('package'); package.add_argument('--source', type=Path, required=True); package.add_argument('--output', type=Path, required=True); package.add_argument('--include-tests', action='store_true')
    verify = subs.add_parser('verify'); verify.add_argument('--bundle', type=Path, required=True)
    install = subs.add_parser('install-new'); install.add_argument('--bundle', type=Path, required=True); install.add_argument('--target', type=Path, required=True)
    plan = subs.add_parser('plan'); plan.add_argument('--bundle', type=Path, required=True); plan.add_argument('--workspace', type=Path, required=True); plan.add_argument('--state', type=Path, required=True); plan.add_argument('--output', type=Path, required=True)
    check = subs.add_parser('check-plan'); check.add_argument('--bundle', type=Path, required=True); check.add_argument('--file', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'package': result = release.package(args.source, args.output, include_tests=args.include_tests)
        elif args.command == 'verify': result = release.verify(args.bundle)
        elif args.command == 'install-new': result = release.install_new(args.bundle, args.target)
        elif args.command == 'check-plan': result = release.check_plan(args.bundle, json.loads(args.file.read_text()))
        else:
            result = release.deployment_plan(args.bundle, args.workspace, args.state)
            with args.output.open('x') as stream: stream.write(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    except (HarnessError, OSError, ValueError) as exc:
        print(json.dumps({'error': {'code': getattr(exc, 'code', 'IO_ERROR'), 'message': str(exc)}}), file=sys.stderr); return 2


if __name__ == '__main__': raise SystemExit(main())
