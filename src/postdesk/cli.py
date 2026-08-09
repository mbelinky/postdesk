from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import __version__
from .config import add_tenant, list_tenants, load_tenant, public_config, tenants_dir
from .core import (
    add_post,
    approve_post,
    cancel_post,
    delete_receipt,
    edit_receipt,
    get_post,
    list_posts,
    pull_insights,
    reconcile,
    run_due,
    serialize_metric,
    serialize_post,
    serialize_receipt,
)
from .drivers import default_drivers
from .errors import ConfigError, InvalidInput, PostdeskError, error_payload
from .models import Receipt
from .store import initialize, resolve_store


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InvalidInput(message, path="arguments")


def _store_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", default=argparse.SUPPRESS, help="SQLite file or store directory")


def build_parser() -> Parser:
    parser = Parser(prog="postdesk", description="Tenant-neutral social publishing desk")
    parser.add_argument("--store", help="SQLite file or store directory")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)

    init = commands.add_parser("init", help="create a store and demo tenant")
    _store_arg(init)
    init.add_argument("--json", action="store_true")

    tenant = commands.add_parser("tenant", help="manage tenant configuration")
    _store_arg(tenant)
    tenant.add_argument("--json", action="store_true")
    tenant_commands = tenant.add_subparsers(dest="tenant_command", required=True, parser_class=Parser)
    tenant_add = tenant_commands.add_parser("add")
    _store_arg(tenant_add)
    tenant_add.add_argument("name")
    tenant_add.add_argument("--json", action="store_true")
    tenant_list = tenant_commands.add_parser("list")
    _store_arg(tenant_list)
    tenant_list.add_argument("--json", action="store_true")
    tenant_show = tenant_commands.add_parser("show")
    _store_arg(tenant_show)
    tenant_show.add_argument("name")
    tenant_show.add_argument("--json", action="store_true")

    queue = commands.add_parser("queue", help="manage queued posts")
    _store_arg(queue)
    queue.add_argument("--json", action="store_true")
    queue_commands = queue.add_subparsers(dest="queue_command", required=True, parser_class=Parser)
    queue_add = queue_commands.add_parser("add")
    _store_arg(queue_add)
    _post_args(queue_add)
    queue_add.add_argument("--json", action="store_true")
    queue_list = queue_commands.add_parser("list")
    _store_arg(queue_list)
    queue_list.add_argument("--status")
    queue_list.add_argument("--tenant")
    queue_list.add_argument("--json", action="store_true")
    queue_show = queue_commands.add_parser("show")
    _store_arg(queue_show)
    queue_show.add_argument("post_id", type=int)
    queue_show.add_argument("--json", action="store_true")
    queue_approve = queue_commands.add_parser("approve")
    _store_arg(queue_approve)
    queue_approve.add_argument("post_id", type=int)
    queue_approve.add_argument("--json", action="store_true")
    queue_cancel = queue_commands.add_parser("cancel")
    _store_arg(queue_cancel)
    queue_cancel.add_argument("post_id", type=int)
    queue_cancel.add_argument("--json", action="store_true")
    queue_preview = queue_commands.add_parser("preview")
    _store_arg(queue_preview)
    queue_preview.add_argument("--status")
    queue_preview.add_argument("--tenant")
    queue_preview.add_argument("--json", action="store_true")

    run = commands.add_parser("run", help="publish due approved posts")
    _store_arg(run)
    run.add_argument("--json", action="store_true")
    run.add_argument("--due", action="store_true", required=True)
    run.add_argument("--dry-run", action="store_true")

    rec = commands.add_parser("reconcile", help="resolve expired or uncertain attempts")
    _store_arg(rec)
    rec.add_argument("--json", action="store_true")

    publish = commands.add_parser("publish", help="publish immediately without approval")
    _store_arg(publish)
    publish.add_argument("--json", action="store_true")
    publish.add_argument("--dry-run", action="store_true")
    _post_args(publish)

    edit = commands.add_parser("edit", help="edit a published caption")
    _store_arg(edit)
    edit.add_argument("--json", action="store_true")
    edit.add_argument("--receipt", type=int, required=True)
    edit.add_argument("--caption-file", required=True)

    delete = commands.add_parser("delete", help="delete a published post")
    _store_arg(delete)
    delete.add_argument("--json", action="store_true")
    delete.add_argument("--receipt", type=int, required=True)

    insights = commands.add_parser("insights", help="pull raw network metrics")
    _store_arg(insights)
    insights.add_argument("--json", action="store_true")
    insight_commands = insights.add_subparsers(dest="insights_command", required=True, parser_class=Parser)
    pull = insight_commands.add_parser("pull")
    _store_arg(pull)
    pull.add_argument("--tenant", required=True)
    pull.add_argument("--since")
    pull.add_argument("--json", action="store_true")

    capabilities = commands.add_parser("capabilities", help="show honest channel support")
    _store_arg(capabilities)
    capabilities.add_argument("--json", action="store_true")
    capabilities.add_argument("--tenant", required=True)

    describe = commands.add_parser("describe", help="describe the tool for agents")
    describe.add_argument("--json", action="store_true")
    return parser


def _post_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--channel", required=True, choices=("instagram", "facebook"))
    parser.add_argument("--kind", required=True)
    parser.add_argument("--media", action="append", default=[])
    parser.add_argument("--caption-file")
    parser.add_argument("--first-comment-file")
    parser.add_argument("--collaborator", action="append", default=[])
    parser.add_argument("--link")
    parser.add_argument("--at")
    parser.add_argument("--external-ref")


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    wants_json = "--json" in args_list
    try:
        args = build_parser().parse_args(args_list)
        wants_json = bool(getattr(args, "json", False) or wants_json)
        payload, status = dispatch(args)
        emit(payload, as_json=wants_json, stream=sys.stdout)
        return status
    except PostdeskError as exc:
        payload = error_payload(exc)
        emit(payload, as_json=wants_json, stream=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        wrapped = ConfigError(str(exc), path=getattr(exc, "filename", "") or "filesystem")
        emit(error_payload(wrapped), as_json=wants_json, stream=sys.stderr)
        return wrapped.exit_code


def dispatch(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    drivers = default_drivers()
    if args.command == "describe":
        return describe_payload(drivers), 0
    store = resolve_store(getattr(args, "store", None), init=args.command == "init")
    engine = initialize(store)
    if args.command == "init":
        demo_path = tenants_dir(store) / "demo" / "config.toml"
        if not demo_path.exists():
            add_tenant(store, "demo", demo=True)
        return {"ok": True, "store": str(store), "tenants_dir": str(tenants_dir(store)), "demo": str(demo_path)}, 0
    if args.command == "tenant":
        if args.tenant_command == "add":
            path = add_tenant(store, args.name)
            return {"ok": True, "tenant": args.name, "config": str(path)}, 0
        if args.tenant_command == "list":
            return {"ok": True, "tenants": list_tenants(store)}, 0
        return {"ok": True, "tenant": public_config(load_tenant(store, args.name))}, 0
    if args.command == "capabilities":
        config = load_tenant(store, args.tenant)
        matrices = {
            channel: drivers[channel].capabilities().as_dict()
            for channel in sorted(config.channels)
            if channel in drivers
        }
        return {"ok": True, "tenant": args.tenant, "channels": matrices}, 0
    with Session(engine, expire_on_commit=False) as session:
        if args.command == "queue":
            if args.queue_command == "add":
                row, created = _add_from_args(session, store, drivers, args, approved=False)
                return {"ok": True, "created": created, "post": serialize_post(row)}, 0
            if args.queue_command == "list":
                rows = list_posts(session, status=args.status, tenant=args.tenant)
                return {"ok": True, "posts": [serialize_post(row) for row in rows]}, 0
            if args.queue_command == "show":
                return {"ok": True, "post": serialize_post(get_post(session, args.post_id), session=session)}, 0
            if args.queue_command == "approve":
                return {"ok": True, "post": serialize_post(approve_post(session, args.post_id))}, 0
            if args.queue_command == "cancel":
                return {"ok": True, "post": serialize_post(cancel_post(session, args.post_id))}, 0
            rows = list_posts(session, status=args.status, tenant=args.tenant)
            return {"ok": True, "preview": [_preview(row) for row in rows]}, 0
        if args.command == "publish":
            row, created = _add_from_args(session, store, drivers, args, approved=True)
            if not created and row.status == "published":
                return {"ok": True, "created": False, "post": serialize_post(row, session=session)}, 0
            post_id = row.id
    if args.command == "run":
        payload = run_due(engine, store, drivers, dry_run=args.dry_run)
        return _command_result(payload)
    if args.command == "reconcile":
        return reconcile(engine, store, drivers), 0
    if args.command == "publish":
        payload = run_due(engine, store, drivers, dry_run=args.dry_run, only_ids=[post_id])
        return _command_result(payload)
    if args.command == "edit":
        receipt = edit_receipt(engine, store, drivers, args.receipt, _read_file(args.caption_file, "caption_file"))
        return {"ok": True, "receipt": serialize_receipt(receipt)}, 0
    if args.command == "delete":
        receipt = delete_receipt(engine, store, drivers, args.receipt)
        return {"ok": True, "receipt": serialize_receipt(receipt)}, 0
    if args.command == "insights" and args.insights_command == "pull":
        metrics = pull_insights(engine, store, drivers, args.tenant, args.since)
        return {"ok": True, "metrics": [serialize_metric(row) for row in metrics]}, 0
    raise InvalidInput("Unknown command.", path="command")


def _add_from_args(session: Session, store: Path, drivers: dict[str, Any], args: argparse.Namespace, *, approved: bool):
    caption = _read_file(args.caption_file, "caption_file") if args.caption_file else None
    first_comment = _read_file(args.first_comment_file, "first_comment_file") if args.first_comment_file else None
    return add_post(
        session,
        store,
        drivers,
        tenant=args.tenant,
        channel=args.channel,
        kind=args.kind,
        media=args.media,
        caption=caption,
        first_comment=first_comment,
        collaborators=args.collaborator,
        link=args.link,
        at=args.at,
        external_ref=args.external_ref,
        approved=approved,
    )


def _read_file(value: str, field: str) -> str:
    path = Path(value).expanduser()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise InvalidInput(f"Cannot read {field}: {exc}", path=str(path)) from exc


def _preview(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant": row.tenant,
        "channel": row.channel,
        "kind": row.kind,
        "at": row.scheduled_at.isoformat(),
        "status": row.status,
        "media": json.loads(row.media_json),
        "caption": row.caption,
    }


def _result_status(payload: dict[str, Any]) -> int:
    failures = [int(item.get("exit_code", 0)) for item in payload.get("results", [])]
    return max(failures, default=0)


def _command_result(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    status = _result_status(payload)
    if status == 0:
        return payload, 0
    failure = next(item for item in payload.get("results", []) if int(item.get("exit_code", 0)) == status)
    kinds = {2: "invalid_input", 3: "config", 4: "network_api", 5: "claim_conflict"}
    structured = {
        "ok": False,
        "error": {
            "kind": kinds[status],
            "detail": failure.get("error", "Command failed."),
            "path": f"results.{failure.get('id', '')}",
        },
        "result": payload,
    }
    return structured, status


def describe_payload(drivers: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "tool": "postdesk",
        "version": __version__,
        "purpose": "Queue, approve, schedule, publish at most once, reconcile, and pull raw insights for tenant-configured social channels.",
        "store": {"type": "sqlite", "argument": "--store", "environment": "POSTDESK_STORE"},
        "commands": {
            "init": "Create the SQLite store and demo tenant.",
            "tenant": ["add", "list", "show"],
            "queue": ["add", "list", "show", "approve", "cancel", "preview"],
            "run": "Publish due approved posts.",
            "reconcile": "Resolve expired leases and uncertain attempts without blind retry.",
            "publish": "Publish immediately without an approval gate.",
            "edit": "Edit a receipt when the channel supports it.",
            "delete": "Delete a receipt when the channel supports it.",
            "insights": ["pull"],
            "capabilities": "Return the per-channel capability matrix.",
            "describe": "Return this document.",
        },
        "exit_codes": {"0": "success", "2": "invalid input", "3": "configuration or credential error", "4": "network or API error", "5": "claim conflict"},
        "at_most_once": {
            "lease": True,
            "durable_attempt_before_network": True,
            "remote_id_checkpoints": True,
            "lookup": ["found", "not_found", "unknown"],
            "unknown_action": "hold in_doubt for operator triage",
        },
        "drivers": sorted(drivers),
        "dry_run": "Uses the real validation and lifecycle, records a local receipt, and emits exact redacted API call transcripts without HTTP requests.",
    }


def emit(payload: dict[str, Any], *, as_json: bool, stream) -> None:
    if as_json:
        stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return
    if payload.get("ok"):
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        error = payload["error"]
        stream.write(f"postdesk: {error['detail']}\n")


if __name__ == "__main__":
    raise SystemExit(main())
