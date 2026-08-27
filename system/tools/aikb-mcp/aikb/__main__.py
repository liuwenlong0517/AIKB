"""AIKB 命令行入口，统一处理服务、检索、校验和 hook 子命令。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .audit import AuditStore, audit_summary, combine_invocations, filter_events, render_markdown, write_excel_report, write_report
from .config import Settings
from .hooks import handle_hook
from .indexer import metadata_report, rebuild_knowledge_index
from .knowledge import KnowledgeService
from .server import run_server
from .workstate import WorkStateStore


def _configure_stdio_utf8() -> None:
    """让 CLI JSON 协议不依赖 Windows 活动代码页。"""
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="\n", write_through=True)


def _json(value: object) -> None:
    """以便于人类查看的 UTF-8 JSON 输出结果，不改变调用方对象。"""
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器；实际路径解析交给 ``Settings.load``。"""
    parser = argparse.ArgumentParser(prog="aikb", description="AIKB local knowledge and work-state service")
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--knowledge-root", type=Path)
    parser.add_argument("--workspace-root", type=Path)
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve")
    serve.add_argument("--agent", default="unknown")
    sub.add_parser("validate")
    sub.add_parser("rebuild")
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    read = sub.add_parser("read")
    read.add_argument("identifier")
    read.add_argument("--section")
    read.add_argument("--max-chars", type=int, default=4000)
    work = sub.add_parser("work-get")
    work.add_argument("--project-path")
    work.add_argument("--work-id")
    hook = sub.add_parser("hook")
    hook.add_argument("--agent", required=True)
    hook.add_argument("--event", required=True)
    audit = sub.add_parser("audit")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_list = audit_sub.add_parser("list")
    audit_list.add_argument("--since")
    audit_list.add_argument("--date")
    audit_list.add_argument("--agent")
    audit_list.add_argument("--source", choices=["mcp", "hook"])
    audit_list.add_argument("--status", choices=["succeeded", "failed", "noop", "blocked", "incomplete"])
    audit_show = audit_sub.add_parser("show")
    audit_show.add_argument("event_id")
    audit_summary_parser = audit_sub.add_parser("summary")
    audit_summary_parser.add_argument("--since")
    audit_summary_parser.add_argument("--date")
    audit_summary_parser.add_argument("--agent")
    audit_summary_parser.add_argument("--source", choices=["mcp", "hook"])
    audit_report = audit_sub.add_parser("report")
    audit_report.add_argument("--date", help="报告日期，格式 YYYY-MM-DD；默认今天")
    audit_report.add_argument("--output", type=Path, help="Excel 报告文件路径（.xlsx）；默认 workspace/audit/reports/YYYY-MM-DD.xlsx")
    audit_report_markdown = audit_sub.add_parser("report-md", help="暂时弃用：生成 Markdown 兼容报告")
    audit_report_markdown.add_argument("--date", help="报告日期，格式 YYYY-MM-DD；默认今天")
    audit_report_markdown.add_argument("--output", type=Path, help="Markdown 报告文件路径（.md）；默认 workspace/audit/reports/YYYY-MM-DD.md")
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行一个 CLI 命令并返回进程退出码；未指定命令时进入 MCP 服务。"""
    _configure_stdio_utf8()
    args = build_parser().parse_args(argv)
    settings = Settings.load(
        repo_root=args.repo_root,
        workspace_root=args.workspace_root,
        knowledge_root=args.knowledge_root,
    )
    command = args.command or "serve"
    if command == "serve":
        run_server(settings, agent=args.agent if args.command else "unknown")
    elif command == "validate":
        report = metadata_report(settings)
        _json(report)
        return 0 if report["valid"] else 1
    elif command == "rebuild":
        result = {"knowledge": rebuild_knowledge_index(settings), "work": WorkStateStore(settings).rebuild_index()}
        _json(result)
    elif command == "search":
        _json(KnowledgeService(settings).search(args.query, limit=args.limit))
    elif command == "read":
        _json(KnowledgeService(settings).read(args.identifier, section=args.section, max_chars=args.max_chars))
    elif command == "work-get":
        _json(WorkStateStore(settings).get(project_path=args.project_path, work_id=args.work_id))
    elif command == "hook":
        # Windows PowerShell 管道偶尔会在 UTF-8 JSON 前保留 BOM，协议入口需容忍该边界。
        raw = sys.stdin.read().lstrip("\ufeff").strip()
        payload = json.loads(raw) if raw else {}
        print(json.dumps(handle_hook(args.agent, args.event, payload, settings), ensure_ascii=False, separators=(",", ":")))
    elif command == "audit":
        store = AuditStore(settings)
        loaded = store.read_events()
        selected_date = getattr(args, "date", None)
        if args.audit_command in {"report", "report-md"} and not selected_date:
            selected_date = datetime.now().astimezone().date().isoformat()
        combined = combine_invocations(loaded["events"])
        items = filter_events(
            combined, since=getattr(args, "since", None), on_date=selected_date,
            agent=getattr(args, "agent", None), source=getattr(args, "source", None),
        )
        requested_status = getattr(args, "status", None)
        if requested_status:
            items = [item for item in items if item.get("status") == requested_status]
        fallback_count = sum(1 for item in items if item.get("_fallback"))
        if args.audit_command == "list":
            _json({"count": len(items), "items": items, "damaged": loaded["damaged"]})
        elif args.audit_command == "show":
            match = next((item for item in items if args.event_id in {
                item.get("event_id"), item.get("finish_event_id"), item.get("invocation_id")
            }), None)
            _json(match or {"error": f"未找到审计事件：{args.event_id}"})
            return 0 if match else 1
        else:
            summary = audit_summary(items, damaged=loaded["damaged"], fallback_count=fallback_count)
            if args.audit_command == "summary":
                _json(summary)
            else:
                title_date = selected_date
                try:
                    if args.audit_command == "report-md":
                        report_path = args.output or (settings.workspace_root / "audit" / "reports" / f"{title_date}.md")
                        write_report(report_path, render_markdown(items, summary, title_date))
                        print("警告：audit report-md 暂时弃用；请改用 audit report 生成 Excel 审计报告。", file=sys.stderr)
                    else:
                        report_path = args.output or (settings.workspace_root / "audit" / "reports" / f"{title_date}.xlsx")
                        write_excel_report(report_path, items, summary, title_date)
                except ValueError as exc:
                    print(f"错误：{exc}", file=sys.stderr)
                    return 2
                except OSError as exc:
                    print(f"错误：无法写入审计报告 {report_path}：{exc}", file=sys.stderr)
                    return 1
                _json({"output": str(report_path.expanduser().resolve()), "count": len(items)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
