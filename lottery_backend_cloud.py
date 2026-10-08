# -*- coding: utf-8 -*-
"""
手机安装版 —— 云端后端入口（部署到免费公网托管，如 Render / Zeabur）
================================================================
- 同时托管「彩票分析_手机安装版.html」App 页面 与 数据 API
- 提供 CORS 跨域，手机 App 可随时随地访问
- 复用 lottery_analyzer.py 的抓取 + 分析逻辑
启动命令：python lottery_backend_cloud.py （云端会自动设置 PORT 环境变量）
"""
import os
import json
import urllib.parse
import socketserver
import sys

import lottery_analyzer as core

DEPLOY_DIR = os.path.dirname(os.path.abspath(__file__))


class CloudHandler(core.Handler):
    def end_headers(self):
        # 允许手机 App 跨域调用
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            html = os.path.join(DEPLOY_DIR, "彩票分析_手机安装版.html")
            if not os.path.exists(html):
                html = core.HTML_PATH  # 兜底用桌面版页面
            with open(html, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/manifest.json":
            with open(os.path.join(DEPLOY_DIR, "manifest.json"), "rb") as f:
                self._send(200, f.read(), "application/manifest+json; charset=utf-8")
        elif path == "/icon.png":
            with open(os.path.join(DEPLOY_DIR, "icon.png"), "rb") as f:
                self._send(200, f.read(), "image/png")
        else:
            super().do_GET()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    port = int(os.environ.get("PORT", "8765"))
    handler = lambda *a, **k: CloudHandler(*a, **k)  # noqa
    try:
        httpd = socketserver.ThreadingTCPServer(("0.0.0.0", port), handler)
    except OSError:
        httpd = socketserver.ThreadingTCPServer(("0.0.0.0", 0), handler)
        port = httpd.server_address[1]
    print("手机独立后端已启动，端口 %d" % port, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
