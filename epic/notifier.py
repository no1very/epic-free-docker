"""结果通知：ntfy / Bark / 通用 webhook / 关闭。环境变量配置。"""

import json
import os
import urllib.parse
import urllib.request

from .api import DESKTOP_UA


def send(title: str, message: str) -> None:
    method = os.environ.get("NOTIFY_METHOD", "none").strip().lower()
    url = os.environ.get("NOTIFY_URL", "").strip()
    if method == "none" or not url:
        return
    try:
        if method == "ntfy":
            _post(
                url,
                data=message.encode("utf-8"),
                headers={"Title": title, "Tags": "game", "User-Agent": DESKTOP_UA},
            )
        elif method == "bark":
            u = f"{url.rstrip('/')}/{urllib.parse.quote(title)}/{urllib.parse.quote(message)}"
            _get(u)
        elif method == "webhook":
            _post(
                url,
                data=json.dumps({"title": title, "message": message}).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": DESKTOP_UA},
            )
        else:
            print(f"[notify] 未知 NOTIFY_METHOD: {method}")
    except Exception as e:
        print(f"[notify] 发送失败: {e}")


def _post(url, data, headers):
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": DESKTOP_UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()
