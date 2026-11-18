"""内存数据仓库：支持模块间关联、版本管理、模拟表数据、血缘记录与状态流转。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 模拟真实表数据（分层存储的实际行数据）
        self._table_data: dict[str, list[dict[str, Any]]] = {}
        # 版本历史: "module:id" -> [{version, timestamp, data}, ...]
        self._versions: dict[str, list[dict[str, Any]]] = {}
        # 血缘边记录
        self._lineage_edges: list[dict[str, Any]] = []
        # 质量稽核结果
        self._quality_results: dict[str, list[dict[str, Any]]] = {}
        # 快照冻结数据: "snapshot_id" -> {table_name, rows, frozen_at}
        self._snapshot_data: dict[str, dict[str, Any]] = {}
        # 归档记录
        self._archive_records: list[dict[str, Any]] = []
        # 补数区间记录
        self._backfill_partitions: dict[str, list[dict[str, Any]]] = {}
        # 分级稽核结果表（按租户归属隔离）
        self._audit_results: list[dict[str, Any]] = []
        # 数据发布跨模块持久化的稽核结果快照
        self._publish_audit_snapshots: list[dict[str, Any]] = []
        self._seed_audit_demo_data()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def find_all_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ---- 模拟真实表数据 ----

    def table_rows(self, table_name: str) -> list[dict[str, Any]]:
        return self._table_data.setdefault(table_name, [])

    def set_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data[table_name] = rows

    def append_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data.setdefault(table_name, []).extend(rows)

    def tenant_table_rows(self, table_name: str, tenant_id: str) -> list[dict[str, Any]]:
        """只读取指定租户归属的行；无租户标记的历史数据视为该表旧数据，兼容保留。"""
        from app.tenancy import SHARED_TENANT
        result = []
        for row in self.table_rows(table_name):
            owner = row.get("_tenant")
            if owner is None or owner in (tenant_id, SHARED_TENANT):
                result.append(row)
        return result

    @staticmethod
    def sample_rows(rows: list[dict[str, Any]], ratio: float) -> list[dict[str, Any]]:
        """按比例确定性等距抽样，替代整表全扫；ratio<=0 取空，>=1 全量。

        等距抽样不依赖随机数，同批次重复执行结果可复现，也不会因为数据量大而逐行随机。
        """
        total = len(rows)
        if total == 0:
            return []
        if ratio >= 1:
            return list(rows)
        if ratio <= 0:
            return []
        sample_size = max(1, int(round(total * ratio)))
        if sample_size >= total:
            return list(rows)
        step = total / sample_size
        picked = {min(int(index * step), total - 1) for index in range(sample_size)}
        return [rows[index] for index in sorted(picked)]

    # ---- 版本管理 ----

    def _version_key(self, module: str, entry_id: int) -> str:
        return f"{module}:{entry_id}"

    def save_version(self, module: str, entry_id: int, data: dict[str, Any]) -> dict[str, Any]:
        key = self._version_key(module, entry_id)
        versions = self._versions.setdefault(key, [])
        version = {
            "version": len(versions) + 1,
            "timestamp": datetime.now().isoformat(),
            "data": dict(data),
        }
        versions.append(version)
        return version

    def get_versions(self, module: str, entry_id: int) -> list[dict[str, Any]]:
        key = self._version_key(module, entry_id)
        return list(self._versions.get(key, []))

    def get_latest_version(self, module: str, entry_id: int) -> dict[str, Any] | None:
        versions = self.get_versions(module, entry_id)
        return versions[-1] if versions else None

    # ---- 血缘 ----

    def add_lineage(self, upstream: str, downstream: str, field_map: str,
                    transform_logic: str = "", batch_id: str = "") -> dict[str, Any]:
        edge = {
            "id": len(self._lineage_edges) + 1,
            "上游表名": upstream,
            "下游表名": downstream,
            "来源字段": field_map,
            "目标字段": field_map,
            "转换逻辑": transform_logic,
            "采集批次": batch_id,
            "链路版本": f"v{len(self._lineage_edges) + 1}",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        }
        self._lineage_edges.append(edge)
        # 同时写入 lineage_track 模块表供查询
        self.rows("lineage_track").append(edge)
        return edge

    def get_lineage_edges(self) -> list[dict[str, Any]]:
        return list(self._lineage_edges)

    # ---- 质量稽核结果 ----

    def save_quality_result(self, rule_id: str, result: dict[str, Any]) -> None:
        self._quality_results.setdefault(rule_id, []).append(result)

    def get_quality_results(self, rule_id: str) -> list[dict[str, Any]]:
        return list(self._quality_results.get(rule_id, []))

    # ---- 快照 ----

    def save_snapshot_data(self, snapshot_id: str, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._snapshot_data[snapshot_id] = {
            "table_name": table_name,
            "rows": [dict(r) for r in rows],
            "frozen_at": datetime.now().isoformat(),
            "row_count": len(rows),
        }

    def get_snapshot_data(self, snapshot_id: str) -> dict[str, Any] | None:
        return self._snapshot_data.get(snapshot_id)

    # ---- 归档 ----

    def add_archive_record(self, record: dict[str, Any]) -> None:
        self._archive_records.append(record)

    def get_archive_records(self) -> list[dict[str, Any]]:
        return list(self._archive_records)

    # ---- 补数分区 ----

    def save_backfill_partition(self, job_id: str, partition: dict[str, Any]) -> None:
        self._backfill_partitions.setdefault(job_id, []).append(partition)

    def get_backfill_partitions(self, job_id: str) -> list[dict[str, Any]]:
        return list(self._backfill_partitions.get(job_id, []))

    # ---- 分级稽核结果表 ----

    def audit_results(self) -> list[dict[str, Any]]:
        return self._audit_results

    def find_audit_result(self, result_id: int) -> dict[str, Any] | None:
        for row in self._audit_results:
            if int(row.get("id", 0)) == result_id:
                return row
        return None

    def add_audit_result(self, entry: dict[str, Any]) -> dict[str, Any]:
        entry["id"] = max((int(row.get("id", 0)) for row in self._audit_results), default=0) + 1
        self._audit_results.append(entry)
        return entry

    # ---- 数据发布：跨模块持久化分级稽核结果表 ----

    def save_publish_audit_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._publish_audit_snapshots.append(snapshot)

    def get_publish_audit_snapshots(self, *, dataset: str | None = None) -> list[dict[str, Any]]:
        snapshots = list(self._publish_audit_snapshots)
        if dataset:
            snapshots = [snap for snap in snapshots if snap.get("dataset") == dataset]
        return snapshots

    def _seed_audit_demo_data(self) -> None:
        """预置分租户表数据，让按租户归属抽样与跨租户匿名汇总开箱可见。"""
        from app.tenancy import SHARED_TENANT
        demo_tables = {
            "ods_orders": {
                "tenant": "t_order",
                "rows": [
                    {"order_id": "O001", "order_amount": "100.00", "order_status": "已完成", "dt": "2026-11-01"},
                    {"order_id": "O002", "order_amount": "200.00", "order_status": "已完成", "dt": "2026-11-01"},
                    {"order_id": "O003", "order_amount": "-10.00", "order_status": "已完成", "dt": "2026-11-01"},
                    {"order_id": "O004", "order_amount": "300.00", "order_status": "已完成", "dt": "2026-11-01"},
                ],
            },
            "ods_payments": {
                "tenant": "t_payment",
                "rows": [
                    {"pay_id": "P001", "pay_amount": "50.00", "pay_time": "2026-11-01T10:00:00", "dt": "2026-11-01"},
                    {"pay_id": "P002", "pay_amount": "120.00", "pay_time": "2026-11-01T11:00:00", "dt": "2026-11-01"},
                    {"pay_id": "P003", "pay_amount": "9999999.00", "pay_time": "2026-11-01T12:00:00", "dt": "2026-11-01"},
                ],
            },
            "ods_users": {
                "tenant": "t_user",
                "rows": [
                    {"user_id": "U001", "user_level": "VIP", "dt": "2026-11-01"},
                    {"user_id": "U002", "user_level": "普通", "dt": "2026-11-01"},
                    {"user_id": "U003", "user_level": "非法值", "dt": "2026-11-01"},
                ],
            },
        }
        for table, spec in demo_tables.items():
            existing = self._table_data.setdefault(table, [])
            if existing:
                continue
            for row in spec["rows"]:
                row.setdefault("_tenant", spec["tenant"])
            existing.extend(spec["rows"])
        # 共享域数据：任何租户可读、永远只读
        self._table_data.setdefault("ods_tenant_dictionary", [
            {"dict_code": "TENANT", "dict_value": "多租户", "_tenant": SHARED_TENANT, "dt": "2026-11-01"},
        ])


store = Store()
