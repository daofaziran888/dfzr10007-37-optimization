"""自助SQL查询业务规则：状态流转、字段校验与筛选口径都收在这里。

结果缓存按「脚本编号 + 参数指纹」建立多版本快照：
- 参数按键名排序拼成规范串再取摘要，同一组参数永远得到同一个指纹；
- 同一指纹重复执行只复用首次完整快照，不再生成新缓存版本；
- 参数变化生成新指纹、新缓存版本，旧参数版本保留可回看；
- 批量执行整组一次提交，按脚本与参数指纹归并，逐条返回缓存结果，失败脚本只重试自身。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "sqlpad"
REQUIRED_FIELDS = ["脚本编号", "脚本名称", "SQL语句"]
STATUS_ORDER = ["草稿", "已保存", "已分享", "已归档"]
ACTION_RULES = {"分享脚本": "已保存", "归档脚本": "已分享", "删除脚本": "已归档"}
NEGATIVE_ACTIONS = []
ARCHIVED_STATUS = "已归档"


def fingerprint_of(params: dict[str, Any]) -> tuple[str, str]:
    """把参数按键名排序拼成规范串，同一组参数永远得到同一个指纹。"""
    canonical = "&".join(f"{key}={params[key]}" for key in sorted(params))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]
    return f"FP-{digest}", canonical


def parse_param_names(definition: Any) -> list[str]:
    """从参数定义里拆出参数名：支持逗号、顿号、分号分隔。"""
    text = str(definition or "")
    for mark in ("、", "；", ";"):
        text = text.replace(mark, ",")
    return [chunk.strip() for chunk in text.split(",") if chunk.strip()]


class SqlpadService:
    def __init__(self) -> None:
        # 结果缓存：脚本编号 -> 指纹 -> 快照；执行历史：全局按执行版本递增。
        self._cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._history: list[dict[str, Any]] = []
        self._version_seq = 0
        self._seed_runtime()

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("脚本编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"查询脚本 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于自助SQL查询可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"查询脚本已{action}"

    # ------------------------------------------------------------------
    # 多版本结果缓存与执行历史
    # ------------------------------------------------------------------
    def _seed_runtime(self) -> None:
        """预置 E1~E6 的执行历史与缓存快照，打开页面就能看到多版本缓存形态。"""
        seeded = [
            ("SQLP-0001", {"region": "全国", "period": "202608"}, "2026-11-10 09:12:00"),
            ("SQLP-0002", {"region": "华北", "period": "202608"}, "2026-11-10 09:40:00"),
            ("SQLP-0003", {"channel": "线上", "period": "202608"}, "2026-11-10 10:05:00"),
            ("SQLP-0002", {"region": "华北", "period": "202608"}, "2026-11-11 08:30:00"),
            ("SQLP-0002", {"region": "华南", "period": "202608"}, "2026-11-11 11:20:00"),
            ("SQLP-0001", {"region": "全国", "period": "202609"}, "2026-11-12 09:02:00"),
        ]
        for code, params, at in seeded:
            self._execute(code, params, source="手动执行", at=at)

    def _next_version(self) -> str:
        self._version_seq += 1
        return f"E{self._version_seq}"

    def _find_by_code(self, code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("脚本编号", "")) == code:
                return row
        return None

    def _result_digest(self, code: str, fingerprint: str) -> str:
        """没有真实执行引擎，按脚本与指纹生成确定性的结果摘要，复用时内容一致。"""
        digest = int(hashlib.sha256(f"{code}|{fingerprint}".encode("utf-8")).hexdigest(), 16)
        return f"{digest % 9000 + 1000} 行 · {digest % 800 + 120}ms"

    def _execute(
        self,
        code: str,
        params: dict[str, Any],
        *,
        source: str,
        batch_seen: dict[tuple[str, str], dict[str, Any]] | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        """执行单条脚本：命中指纹复用唯一快照，未命中生成新缓存版本。"""
        code = str(code or "").strip()
        params = {str(key).strip(): str(value).strip() for key, value in (params or {}).items() if str(key).strip()}
        base: dict[str, Any] = {"脚本编号": code, "参数": params, "来源": source}
        row = self._find_by_code(code)
        if row is None:
            return {**base, "ok": False, "message": f"查询脚本 {code} 不存在或已归档"}
        base["脚本名称"] = str(row.get("脚本名称", ""))
        if row.get("status") == ARCHIVED_STATUS:
            return {**base, "ok": False, "message": f"查询脚本 {code} 已归档，不能执行"}
        declared = parse_param_names(row.get("参数定义"))
        missing = [name for name in declared if not str(params.get(name) or "").strip()]
        if missing:
            return {**base, "ok": False, "message": f"缺少执行参数：{'、'.join(missing)}，补齐后只重试本脚本"}

        fingerprint, canonical = fingerprint_of(params)
        versions = self._cache.setdefault(code, {})
        now = at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        snapshot: dict[str, Any] | None = None
        if batch_seen is not None and (code, fingerprint) in batch_seen:
            snapshot = batch_seen[(code, fingerprint)]
            cache_result = "同组归并复用"
        elif fingerprint in versions:
            snapshot = versions[fingerprint]
            cache_result = "复用快照"
        else:
            cache_result = "新建快照"

        version = self._next_version()
        if snapshot is None:
            snapshot = {
                "指纹": fingerprint,
                "参数": dict(params),
                "参数串": canonical,
                "缓存版本": version,
                "来源": source,
                "结果摘要": self._result_digest(code, fingerprint),
                "生成时间": now,
            }
            versions[fingerprint] = snapshot
        if batch_seen is not None:
            batch_seen[(code, fingerprint)] = snapshot

        if cache_result == "新建快照":
            note = f"指纹 {fingerprint} 首次执行，生成新缓存快照 {snapshot['缓存版本']}"
        elif cache_result == "同组归并复用":
            note = f"同组已执行相同指纹，归并复用快照 {snapshot['缓存版本']}，不重复生成"
        else:
            note = f"指纹 {fingerprint} 与既有快照一致，复用唯一快照 {snapshot['缓存版本']}"

        self._history.append({
            "执行版本": version,
            "脚本编号": code,
            "脚本名称": base["脚本名称"],
            "指纹": fingerprint,
            "参数串": canonical,
            "来源": source,
            "缓存结果": cache_result,
            "缓存版本": snapshot["缓存版本"],
            "时间": now,
            "说明": note,
        })

        latest = max(int(item["缓存版本"][1:]) for item in versions.values())
        row["执行历史"] = f"最近 {version} · {cache_result}"
        row["结果缓存"] = f"{len(versions)} 版缓存 · 最新 E{latest}"

        return {
            **base,
            "ok": True,
            "message": note,
            "指纹": fingerprint,
            "参数串": canonical,
            "缓存结果": cache_result,
            "执行版本": version,
            "缓存版本": snapshot["缓存版本"],
            "缓存版本数": len(versions),
        }

    def execute_one(self, code: str, params: dict[str, Any]) -> dict[str, Any]:
        """单脚本执行：失败只影响本脚本，可携带同一参数单独重试。"""
        return self._execute(code, params, source="手动执行")

    def execute_batch(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """批量执行：整组一次提交，按脚本与参数指纹归并缓存，逐条返回结果。"""
        if not items:
            return {"ok": False, "message": "未选择任何脚本，整组提交为空", "results": []}
        batch_seen: dict[tuple[str, str], dict[str, Any]] = {}
        results = [
            self._execute(
                str(item.get("脚本编号") or ""),
                item.get("参数") or {},
                source="批量执行",
                batch_seen=batch_seen,
            )
            for item in items
        ]
        failed = sum(1 for result in results if not result["ok"])
        reused = sum(1 for result in results if result["ok"] and result.get("缓存结果") != "新建快照")
        message = (
            f"整组提交 {len(results)} 个脚本：新建快照 {len(results) - failed - reused} 个，"
            f"复用缓存 {reused} 个，失败 {failed} 个；失败脚本可单独重试"
        )
        return {"ok": failed == 0, "message": message, "results": results}

    def list_cache_versions(self, entry_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """结果缓存多版本清单：按版本号升序，旧参数版本保留可回看。"""
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, []
        code = str(row.get("脚本编号", ""))
        versions = sorted(
            self._cache.get(code, {}).values(),
            key=lambda item: int(str(item["缓存版本"])[1:]),
        )
        return row, versions

    def list_history(self, entry_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """执行历史：每个执行版本都带来源，最新的一条排在最前。"""
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, []
        code = str(row.get("脚本编号", ""))
        entries = [item for item in self._history if item["脚本编号"] == code]
        return row, list(reversed(entries))
