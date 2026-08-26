"""AIKB 命令行入口，统一处理服务、检索、校验和 hook 子命令。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
    sub.add_parser("serve")
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
        run_server(settings)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
