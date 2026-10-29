# -*- coding: utf-8 -*-
r"""进度账本（外壳约定 v0.1 §2.3）。

位置：`<工作根目录>\<文章文件夹>\进度账本.json`，每篇一份，引擎独占写，APP 只读。
`schema_version` 为整数 1；`steps` 为数组。步骤状态定稿 5 个取值：
`pending` / `running` / `done` / `imported` / `failed`。

写入原子化：先写 `<同名>.tmp` 再 `os.replace` 覆盖，强杀进程后账本不会半截。
"""
import datetime
import hashlib
import json
import os
from pathlib import Path

from . import paths

LEDGER_NAME = "进度账本.json"
SCHEMA_VERSION = 1

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_IMPORTED = "imported"
STATUS_FAILED = "failed"
STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_DONE, STATUS_IMPORTED, STATUS_FAILED)

# 只有这两种状态才算「已完成」，续跑时可跳过
SKIPPABLE_STATUS = (STATUS_DONE, STATUS_IMPORTED)


def now_iso():
    """带时区的 ISO 8601（秒精度），账本 created/updated 用。"""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def ledger_path(article_dir):
    return Path(article_dir) / LEDGER_NAME


def load(article_dir):
    p = ledger_path(article_dir)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save_atomic(article_dir, obj):
    p = ledger_path(article_dir)
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def expand(root, patterns):
    """把 glob 展开成实际文件列表（相对 root，`/` 分隔，排序去重）。"""
    out = []
    for pat in patterns:
        for p in sorted(Path(root).glob(pat)):
            if p.is_file():
                out.append(str(p.relative_to(root)).replace("\\", "/"))
    return sorted(set(out))


def fingerprint(root, rel_paths):
    """输入指纹 = 全部输入文件 sha256 合并（缺文件用 `<缺>` 占位，使指纹随缺失而变）。"""
    h = hashlib.sha256()
    for rel in sorted(rel_paths):
        p = Path(root) / rel
        h.update(rel.encode("utf-8"))
        h.update((sha256_file(p) if p.exists() else "<缺>").encode("utf-8"))
    return h.hexdigest()


def outputs_record(root, rel_paths):
    """产物清单：path / sha256 / size。"""
    recs = []
    for rel in rel_paths:
        p = Path(root) / rel
        recs.append({"path": rel, "sha256": sha256_file(p), "size": p.stat().st_size})
    return recs


def new_step(step_id, name, status=STATUS_PENDING, source=None, message="", attempts=0):
    """新建步骤记录。

    `attempts` 默认 **0**（尚未尝试）；引擎每真跑一次 +1，所以新样本第一次运行是 1。
    导入步骤（status=imported）由样本准备脚本显式置 1——它确实"产出过一次"。
    ST1-P2b 之前默认 1，导致首跑记成 2（ST1-P3 F1 修正）。
    """
    return {
        "step": step_id,
        "name": name,
        "status": status,
        "input_fingerprint": None,
        "code_version": None,
        "outputs": [],
        # `calls` = 付费入口被调用的次数（含命中缓存的）；`paid_calls` = 真正发出去的付费调用；
        # `cache_hits` = 命中缓存次数；`stub_calls` = 其中由**桩**产生的结果数
        # （ST1-P4a2 §一.6：>0 就绝不出片）；`estimated_cny` = 折合人民币（未知单价按 0 计，
        # 此时 `incomplete` 为 True，口径见 ST1-P4a 任务书 §一.4）。
        "cost": {"calls": 0, "paid_calls": 0, "cache_hits": 0, "stub_calls": 0,
                 "estimated_cny": None, "incomplete": False},
        "duration_sec": 0,
        "attempts": attempts,
        "source": source,
        "message": message,
    }


def new_doc(article, content_type):
    """新建空账本（顶层字段见外壳约定 v0.1 §2.3）。

    `pending_jobs`（ST1-P4a 新增）：已提交但还没收结果的**异步付费任务**（当前只有豆包 TTS）。
    每项 `{step, service, task_id, submitted, note}`。中断后重跑时先查这里登记的任务号，
    查到就接着收，不重复提交（省一次费用）。
    """
    now = now_iso()
    doc = {"schema_version": SCHEMA_VERSION, "article": article, "content_type": content_type,
           "engine_root": str(paths.ENGINE_ROOT),
           "created": now, "updated": now, "delivery_path": None, "pending_jobs": [],
           "steps": [], "totals": {}}
    return touch(doc)


def touch(doc):
    """刷新 updated 与 totals。"""
    doc["updated"] = now_iso()
    doc["totals"] = compute_totals(doc)
    return doc


def compute_totals(doc):
    """账本 totals：各状态计数 + 付费调用累计 + 缓存命中 + 折合人民币（ST1-P4a 起有值）。"""
    counts = {s: 0 for s in STATUSES}
    paid = hits = stubs = 0
    cny_any = False
    cny_sum = 0.0
    incomplete = False
    for r in doc.get("steps", []):
        st = r.get("status", STATUS_PENDING)
        counts[st] = counts.get(st, 0) + 1
        c = r.get("cost") or {}
        paid += c.get("paid_calls", 0)
        hits += c.get("cache_hits", 0)
        stubs += c.get("stub_calls", 0)
        if c.get("incomplete"):
            incomplete = True
        v = c.get("estimated_cny")
        if isinstance(v, (int, float)):
            cny_any = True
            cny_sum += float(v)
    out = {"steps_total": len(doc.get("steps", []))}
    out.update(counts)
    out["paid_calls"] = paid
    out["cache_hits"] = hits
    out["stub_calls"] = stubs
    out["estimated_cny"] = round(cny_sum, 4) if cny_any else None
    out["amount_incomplete"] = incomplete
    return out


# --------------------------------------------------------------------------
# 异步任务号（豆包 TTS）
# --------------------------------------------------------------------------


def add_job(doc, step, service, task_id, note=""):
    doc.setdefault("pending_jobs", []).append(
        {"step": step, "service": service, "task_id": str(task_id),
         "submitted": now_iso(), "note": note})
    return doc


def find_job(doc, step, service=None):
    for j in doc.get("pending_jobs") or []:
        if j.get("step") == step and (service is None or j.get("service") == service):
            return j
    return None


def remove_job(doc, task_id):
    jobs = doc.get("pending_jobs") or []
    doc["pending_jobs"] = [j for j in jobs if str(j.get("task_id")) != str(task_id)]


def find_step(doc, step_id):
    for r in doc.get("steps", []):
        if r.get("step") == step_id:
            return r
    return None


def upsert_step(doc, step_id, **fields):
    rec = find_step(doc, step_id)
    if rec is None:
        rec = new_step(step_id, fields.pop("name", step_id))
        doc.setdefault("steps", []).append(rec)
    rec.update(fields)
    return rec
