"""任务进度跟踪（内存态，供 Web 实时反馈）。

所有长任务（节点拉取 / 在线检测 / 速度测试 / 容器检测 / 容器更新）
统一调用本模块的 start / update / finish，前端只需轮询 /api/tasks/status
即可同时看到所有任务的进度。
"""

import time

_progress: dict[str, dict] = {}
_PROGRESS_TTL = 30  # 已完成任务状态保留秒数


def start(task: str, label: str, total: int = 0, message: str = ""):
    _progress[task] = {
        "label": label,
        "running": True,
        "done": 0,
        "total": total,
        "message": message,
        "percent": 0,
        "started_at": time.time(),
        "updated_at": time.time(),
    }


def update(task: str, **kwargs):
    entry = _progress.get(task)
    if entry is None:
        entry = {
            "label": task,
            "running": True,
            "done": 0,
            "total": 0,
            "message": "",
            "percent": 0,
            "started_at": time.time(),
        }
        _progress[task] = entry
    entry.update(kwargs)
    total = entry.get("total") or 0
    done = entry.get("done") or 0
    entry["percent"] = int(done / total * 100) if total > 0 else 0
    entry["updated_at"] = time.time()


def finish(task: str, message: str = "完成"):
    entry = _progress.get(task)
    if entry is None:
        return
    entry["running"] = False
    entry["message"] = message
    entry["percent"] = 100
    entry["updated_at"] = time.time()


def get_all() -> dict:
    """返回当前任务进度（自动清理已结束的旧任务）。"""
    now = time.time()
    for k in list(_progress.keys()):
        v = _progress[k]
        if not v.get("running") and now - v.get("updated_at", 0) > _PROGRESS_TTL:
            _progress.pop(k, None)
    return {k: dict(v) for k, v in _progress.items()}
