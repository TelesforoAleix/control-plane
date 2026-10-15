"""Owner commands for the scheduling state file."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys

from . import state
from .times import LOCAL, parse_when


COMMANDS = ("slot", "unslot", "start", "pause", "resume", "approve", "answer")


def change_for(command, argument=None):
    def change(item, data, at):
        warning = None
        notice = None
        if command in ("slot", "start"):
            if item["state"] != "ready":
                raise state.StateError(f"{item['id']} is not ready")
            accepted = {s["id"] for s in data["slices"] if s["state"] == "accepted"}
            unmet = [key for key in item["depends_on"] if key not in accepted]
            if unmet:
                warning = "unmet dependencies: " + ", ".join(unmet)
            item["slot"] = at if command == "start" else parse_when(argument)
            notice = f"{'Started' if command == 'start' else 'Slotted'} {item['id']} at {item['slot']}"
        elif command == "unslot":
            item["slot"] = None
        elif command in ("pause", "resume"):
            item["paused"] = command == "pause"
        elif command == "approve":
            if not item["attention"] or item["attention"]["kind"] != "merge":
                raise state.StateError(f"{item['id']} has no merge attention")
            item["attention"]["approved"] = True
            notice = f"Approved merge for {item['id']}"
        elif command == "answer":
            if not item["attention"] or item["attention"]["kind"] not in ("question", "escalation"):
                raise state.StateError(f"{item['id']} has no question or escalation attention")
            item["attention"]["answer"] = argument
            notice = f"Answered {item['id']}: {argument}"
        else:
            raise state.StateError(f"unknown command: {command}")
        return notice, warning
    return change


def pending(data):
    groups = {key: [] for key in ("needs attention", "scheduled", "ready", "running", "planned")}
    for item in data["slices"]:
        attention = item["attention"]
        unresolved = attention and (
            (attention["kind"] == "merge" and not attention["approved"])
            or (attention["kind"] in ("question", "escalation") and attention["answer"] is None)
            or attention["kind"] in ("node-step", "owner-check")
        )
        if item["state"] == "accepted":
            continue
        if unresolved or item["state"] in ("in review", "escalated"):
            key = "needs attention"
        elif item["state"] in ("issued", "awaiting handover"):
            key = "running"
        elif item["slot"]:
            key = "scheduled"
        elif item["state"] == "ready":
            key = "ready"
        else:
            key = "planned"
        groups[key].append(item)
    for key, items in groups.items():
        if key == "scheduled":
            items.sort(key=lambda s: (datetime.fromisoformat(s["slot"]), s["id"]))
        else:
            items.sort(key=lambda s: s["id"])
    return groups


def render(groups):
    lines = []
    for group, items in groups.items():
        lines.append(group)
        day = None
        for item in items:
            prefix = "  "
            if group == "scheduled":
                local = datetime.fromisoformat(item["slot"]).astimezone(LOCAL)
                if local.date() != day:
                    day = local.date()
                    lines.append(f"  {day.isoformat()}")
                prefix = f"    {local.strftime('%H:%M %z')} "
            flags = " [paused]" if item["paused"] else ""
            lines.append(f"{prefix}{item['id']} — {item['title']}{flags}")
        if not items:
            lines.append("  (none)")
    return "\n".join(lines)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f"cplane: {message}\n")


def parser():
    common = Parser(add_help=False)
    common.add_argument("--state", default=argparse.SUPPRESS, help="state JSON path (or CPLANE_STATE)")
    common.add_argument("--actor", default=argparse.SUPPRESS, help="log actor (default: owner)")
    common.add_argument("--commit", action="store_true", default=argparse.SUPPRESS, help="commit and sync state repository")
    root = Parser(description="A scheduler's state file and its commands", parents=[common])
    commands = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    view = commands.add_parser("pending", parents=[common], help="show pending slices")
    view.add_argument("--json", action="store_true", help="output grouped JSON")
    helps = {"slot": "schedule a ready slice", "unslot": "clear a slot", "start": "slot a ready slice now", "pause": "set paused", "resume": "clear paused", "approve": "approve merge attention", "answer": "answer a question or escalation"}
    for command in COMMANDS:
        sub = commands.add_parser(command, parents=[common], help=helps[command])
        sub.add_argument("id")
        if command in ("slot", "answer"):
            sub.add_argument("value", metavar="when" if command == "slot" else "text")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        path = getattr(args, "state", None) or os.environ.get("CPLANE_STATE")
        if not path:
            raise state.StateError("provide --state or CPLANE_STATE")
        path = Path(path).expanduser()
        if args.command == "pending":
            groups = pending(state.load(path))
            print(json.dumps(groups, indent=2, ensure_ascii=False) if args.json else render(groups))
        else:
            warning = state.update(path, args.id, args.command, getattr(args, "actor", "owner"), change_for(args.command, getattr(args, "value", None)), getattr(args, "commit", False))
            if warning:
                print(f"warning: {warning}", file=sys.stderr)
        return 0
    except (state.StateError, OSError, ValueError) as error:
        print("cplane: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
