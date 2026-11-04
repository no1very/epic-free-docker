"""auth.json 持久化（/data 卷）。"""

import json
import os
import stat


def auth_path(data_dir: str) -> str:
    return os.path.join(data_dir, "auth.json")


def load(data_dir: str):
    try:
        with open(auth_path(data_dir), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save(data_dir: str, token: dict) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = auth_path(data_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(token, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 600
    except Exception:
        pass


def clear(data_dir: str) -> None:
    try:
        os.remove(auth_path(data_dir))
    except FileNotFoundError:
        pass
