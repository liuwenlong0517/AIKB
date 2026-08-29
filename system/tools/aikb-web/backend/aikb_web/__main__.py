"""AIKB WebUI 本地启动入口。"""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    """仅绑定本机回环地址启动服务；第一阶段不开放局域网监听参数。"""
    parser = argparse.ArgumentParser(description="启动 AIKB WebUI 本地只读服务")
    parser.add_argument("--port", type=int, default=8000, help="本机监听端口，默认 8000")
    parser.add_argument("--reload", action="store_true", help="开发时启用自动重载")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须位于 1..65535")
    uvicorn.run("aikb_web.main:app", host="127.0.0.1", port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
