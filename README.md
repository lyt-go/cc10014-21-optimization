# 数仓分层治理平台

面向数据入库、清洗转换、分层建模、质量校验与血缘追踪的分层数仓治理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 原始入库 | `raw_ingest` | 入库批次 | 批次编号、来源系统、文件格式 |
| 字段映射 | `column_mapping` | 映射规则 | 规则编号、源字段名、目标字段名 |
| 清洗策略 | `cleanse_rule` | 清洗策略 | 策略编号、目标字段、空值处理 |
| 去重配置 | `dedup_config` | 去重配置 | 配置编号、业务键列、排序字段 |
| 贴源层管理 | `ods_layer` | 贴源表 | 表编号、来源系统、表名称 |
| 明细层管理 | `dwd_layer` | 明细表 | 表编号、父层来源、表名称 |
| 汇总层管理 | `dws_layer` | 汇总表 | 表编号、统计粒度、汇总维度 |
| 维度建模 | `dim_model` | 维度模型 | 模型编号、维度名称、自然键列 |
| 快照管理 | `snapshot_mgr` | 数据快照 | 快照编号、目标数据集、快照时间 |
| 分区治理 | `partition_mgr` | 分区策略 | 策略编号、目标表名、分区字段 |
| 质量稽核 | `quality_check` | 稽核规则 | 规则编号、校验表名、校验字段 |
| 分级稽核结果表 | `audit_result` | 分级结果表 | 结果编号、批次编号、租户归属、稽核等级、规则通过率 |
| 血缘追踪 | `lineage_track` | 血缘链路 | 链路编号、上游表名、下游表名 |
| 补数调度 | `backfill_job` | 补数任务 | 任务编号、目标表名、起始日期 |
| 表级比对 | `table_compare` | 比对任务 | 任务编号、左表名称、右表名称 |
| 结构注册 | `schema_registry` | 结构定义 | 定义编号、表名称、字段名称 |
| 数据发布 | `data_publish` | 发布版本 | 版本编号、数据集名称、发布范围 |
| 转换编排 | `transform_dag` | 转换节点 | 节点编号、输入表名、输出表名 |
| 冷热归档 | `cold_archive` | 归档策略 | 策略编号、目标表名、冷数据阈值 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 多租户与权限（质量稽核 / 分级稽核结果表）

- 请求头 `X-Tenant-Id`（`t_order`/`t_payment`/`t_user`/`SHARED`）与
  `X-Tenant-Role`（`admin`/`auditor`/`viewer`）解析为调用上下文，缺省为订单租户管理员；
  归属与权限矩阵统一定义在 `backend/app/tenancy.py`。
- 稽核按租户归属读取并确定性等距抽样（`store.sample_rows`），替代对所有租户的整表扫描；
  无上下文的历史调用链保持整表扫描语义。
- 分级稽核结果表明细按租户隔离；跨租户汇总仅输出匿名口径（通过率、等级分布），
  禁止任何可反推单租户的行数字段，发布前做边界一致性校验。
- 结果表状态只能沿「待复核 → 复核中 → 待发布 → 已发布」单向推进，跳级/回退返回 409，
  越权与跨归属写操作返回 403；确认冲突与规则优先级未裁决的批次禁止发布。
- 结果表确认发布时跨模块持久化快照到数据发布侧
  （`GET /api/data_publish/audit_snapshots` 可查询）。

领域：数据工程
子方向：数仓与数据湖
