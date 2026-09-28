"""Local administrative entry points.

Session provisioning, session activation and private Oracle reading are
management actions. They are reachable ONLY from this local CLI (or in-process
code) and deliberately have no HTTP route, so the Agent cannot change config,
extend budget, grant itself an infrastructure retry or read private verdicts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from runner import SERVICE_VERSION
from runner.config import SessionConfig, and000008_config, mock_config
from runner.contracts.clock import SystemClock
from runner.core.service import RunnerService
from runner.core.supervisor import Supervisor, run_forever
from runner.store.artifacts import ArtifactStore
from runner.store.db import Store
from runner.store.events import EventLog
from runner.transports.server import serve_forever

DEFAULT_ROOT = os.path.join(os.getcwd(), ".runner-state")


def _config_for(name: str) -> SessionConfig:
    if name == "and000008":
        return and000008_config()
    if name == "mock":
        return mock_config()
    raise SystemExit(f"unknown config profile: {name}")


def _build_service(root: str, config: SessionConfig) -> RunnerService:
    return RunnerService(root=root, config=config)


def cmd_admin_create(args: argparse.Namespace) -> int:
    config = _config_for(args.config)
    service = _build_service(args.root, config)
    session_id, token = service.create_session(config, credential_ttl_seconds=args.credential_ttl)
    service.activate_session(session_id, config)
    print(json.dumps({"session_id": session_id, "credential": token, "root": service.root}))
    return 0


def cmd_admin_show(args: argparse.Namespace) -> int:
    config = _config_for(args.config)
    service = _build_service(args.root, config)
    print(json.dumps(service.get_session(args.session_id), indent=2, sort_keys=True))
    return 0


def cmd_admin_oracle(args: argparse.Namespace) -> int:
    """Private researcher view. Never routed over HTTP."""

    config = _config_for(args.config)
    service = _build_service(args.root, config)
    row = service.read_private_oracle(args.attempt_id)
    if row is None:
        print(json.dumps({"attempt_id": args.attempt_id, "evaluation": None}))
        return 0
    print(json.dumps({k: row[k] for k in sorted(row)}, indent=2, default=str))
    return 0


def cmd_supervisor(args: argparse.Namespace) -> int:
    config = _config_for(args.config)
    clock = SystemClock()
    store = Store(os.path.join(args.root, "control.db"), clock=clock)
    events = EventLog(store, clock=clock)
    artifacts = ArtifactStore(os.path.join(args.root, "artifacts"), store, events, clock=clock)
    from runner.adapters.device_mock import MockDeviceAdapter
    from runner.adapters.oracle import MockOracle

    supervisor = Supervisor(
        store=store,
        artifacts=artifacts,
        events=events,
        clock=clock,
        config=config,
        device=MockDeviceAdapter(),
        oracle=MockOracle(),
    )
    run_forever(supervisor, interval_seconds=args.interval, stop_after=args.stop_after)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    config = _config_for(args.config)
    service = _build_service(args.root, config)
    serve_forever(
        service,
        config,
        host=args.host,
        port=args.port,
        session_id=args.session_id,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runner", description="Android PoC Runner (mock framework stage)")
    parser.add_argument("--version", action="version", version=SERVICE_VERSION)
    # Accepted both before and after the subcommand so the common
    # ``runner --root DIR serve`` form works as expected.
    parser.add_argument("--root", default=None, help="state directory (default: " + DEFAULT_ROOT + ")")
    sub = parser.add_subparsers(dest="command", required=True)

    admin = sub.add_parser("admin", help="local session management (no HTTP equivalent)")
    admin_sub = admin.add_subparsers(dest="admin_command", required=True)

    create = admin_sub.add_parser("create-session", help="create and activate a session")
    create.add_argument("--root", default=argparse.SUPPRESS)
    create.add_argument("--config", default="mock", choices=["mock", "and000008"])
    create.add_argument("--credential-ttl", type=float, default=None)
    create.set_defaults(func=cmd_admin_create)

    show = admin_sub.add_parser("show-session", help="print session state")
    show.add_argument("--root", default=argparse.SUPPRESS)
    show.add_argument("--config", default="mock", choices=["mock", "and000008"])
    show.add_argument("--session-id", required=True)
    show.set_defaults(func=cmd_admin_show)

    oracle = admin_sub.add_parser("show-oracle", help="PRIVATE verdict view (researcher only)")
    oracle.add_argument("--root", default=argparse.SUPPRESS)
    oracle.add_argument("--config", default="mock", choices=["mock", "and000008"])
    oracle.add_argument("--attempt-id", required=True)
    oracle.set_defaults(func=cmd_admin_oracle)

    serve = sub.add_parser("serve", help="run the restricted HTTP/JSON controller API")
    serve.add_argument("--root", default=argparse.SUPPRESS)
    serve.add_argument("--config", default="mock", choices=["mock", "and000008"])
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8642)
    serve.add_argument(
        "--session-id",
        default=None,
        help="pin the server to one session; the credential still binds the session",
    )
    serve.set_defaults(func=cmd_serve)

    sup = sub.add_parser("supervisor", help="run the independent supervisor")
    sup.add_argument("--root", default=argparse.SUPPRESS)
    sup.add_argument("--config", default="mock", choices=["mock", "and000008"])
    sup.add_argument("--interval", type=float, default=0.2)
    sup.add_argument("--stop-after", type=float, default=None)
    sup.set_defaults(func=cmd_supervisor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # The global --root is only a default for the subcommand's own --root.
    if getattr(args, "root", None) is None:
        args.root = DEFAULT_ROOT
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
