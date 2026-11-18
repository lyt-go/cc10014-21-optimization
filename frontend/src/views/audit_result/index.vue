<template>
  <section class="page" data-module="audit_result">
    <header class="page-head">
      <div>
        <h2>分级稽核结果表</h2>
        <p class="page-desc">
          按租户归属抽样稽核生成的分级结果表：明细按租户隔离，汇总仅保留匿名口径；状态单向推进，越权操作在服务端拦截。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create-batch" type="button" @click="createBatch">
          提交稽核批次
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card" :data-testid="`kpi-${item.label}`">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>结果状态</span>
        <select v-model="statusFilter" data-testid="filter-status">
          <option value="">全部状态</option>
          <option v-for="item in statusOptions" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>结果编号</span>
        <input v-model="keyword" data-testid="filter-keyword" placeholder="按结果编号检索" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span class="status-badge" :data-testid="`status-${row.id}`">{{ row.status }}</span>
            <span v-if="row.readonly" class="readonly-tag" :data-testid="`readonly-${row.id}`">只读</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in row.allowed_actions ?? []"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" :data-testid="`review-${row.id}`" @click="openDetail(row)">
              复核明细
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state" data-testid="empty-state">
            当前租户归属下暂无分级稽核结果表，可在质量稽核入口提交
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="detail" class="modal-backdrop" data-testid="detail-modal" @click.self="detail = null">
      <div class="modal detail-modal">
        <h3>复核：{{ detail.结果编号 }}</h3>
        <p class="page-desc">
          {{ detail.批次编号 }} · 等级 {{ detail.grade }} · 通过率 {{ detail.规则通过率 }} ·
          状态 {{ detail.status }}
        </p>

        <div v-if="detail.conflict" class="conflict-box" data-testid="conflict-box">
          <strong>待裁决冲突</strong>
          <p>{{ detail.conflict.message }}</p>
          <p class="page-desc">{{ detail.conflict.pending_decision }}</p>
        </div>

        <h4>本租户稽核明细（按归属隔离）</h4>
        <table class="data-table detail-table" data-testid="detail-table">
          <thead>
            <tr>
              <th>规则编号</th>
              <th>校验表</th>
              <th>候选行数</th>
              <th>抽样行数</th>
              <th>通过</th>
              <th>失败</th>
              <th>通过率</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in detail.details" :key="item.rule_id" :data-testid="`detail-row-${item.rule_id}`">
              <td>{{ item.rule_id }}</td>
              <td>{{ item.table }}</td>
              <td>{{ item.total }}</td>
              <td>{{ item.sampled }}</td>
              <td>{{ item.passed }}</td>
              <td>{{ item.failed }}</td>
              <td>{{ (item.pass_rate * 100).toFixed(2) }}%</td>
            </tr>
            <tr v-if="!detail.details.length">
              <td colspan="7" class="empty-state">非归属结果表，明细按租户边界隔离不可见（只读）</td>
            </tr>
          </tbody>
        </table>

        <h4>跨租户匿名汇总</h4>
        <div v-if="detail.cross_summary" class="summary-box" data-testid="cross-summary">
          <p>汇总口径：{{ detail.汇总口径 }} · 参与租户数 {{ detail.cross_summary.tenant_count }} ·
            规则数 {{ detail.cross_summary.rule_count }}</p>
          <p>整体通过率：{{ detail.cross_summary.overall_pass_rate }}
            <span class="readonly-tag">不含单租户行数，不可反推</span>
          </p>
          <p>等级分布：{{ formatDistribution(detail.cross_summary.grade_distribution) }}</p>
        </div>
        <p v-else class="page-desc" data-testid="no-cross-summary">
          当前角色无跨租户汇总权限，结果表仅含本租户明细与规则通过率
        </p>

        <div class="page-actions">
          <button class="btn" type="button" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span data-testid="identity-line">共 {{ total }} 条结果表 · {{ store.tenantName }} / {{ store.roleName }}</span>
      <span v-if="errorMessage" class="error-text" data-testid="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { request } from '@/api/client'
import { useSessionStore } from '@/stores/session'

type DetailItem = {
  rule_id: string
  table: string
  total: number
  sampled: number
  passed: number
  failed: number
  pass_rate: number
}
type CrossSummary = {
  tenant_count: number
  rule_count: number
  overall_pass_rate: string
  grade_distribution: Record<string, number>
}
type AuditRow = Record<string, string | number | null> & {
  id: number
  status: string
  grade: string
  readonly: boolean
  allowed_actions: string[]
  details: DetailItem[]
  cross_summary: CrossSummary | null
  conflict: { type: string; message: string; pending_decision: string } | null
}

const ENDPOINT = '/api/audit_result'
const columns = ['结果编号', '批次编号', '租户归属', '稽核等级', '规则通过率', '采样比例', '明细规则数', '跨租户汇总', '汇总口径']
const statusOptions = ['待复核', '复核中', '待发布', '已发布']

const store = useSessionStore()
const rows = ref<AuditRow[]>([])
const total = ref(0)
const statusFilter = ref('')
const keyword = ref('')
const errorMessage = ref('')
const detail = ref<AuditRow | null>(null)

const stats = computed(() => [
  { label: '结果表总数', value: total.value },
  { label: '待复核', value: rows.value.filter((row) => row.status === '待复核').length },
  { label: '待发布', value: rows.value.filter((row) => row.status === '待发布').length },
  { label: '已发布', value: rows.value.filter((row) => row.status === '已发布').length },
])

function formatDistribution(dist: Record<string, number> | undefined): string {
  if (!dist) return '—'
  return Object.entries(dist).map(([grade, count]) => `${grade}级 ${count}`).join(' / ')
}

function resetFilters() {
  statusFilter.value = ''
  keyword.value = ''
  void reload()
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  if (statusFilter.value) params.set('status', statusFilter.value)
  if (keyword.value) params.set('keyword', keyword.value)
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`, { headers: store.tenantHeaders() })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload?.detail?.message ?? '结果表读取失败')
    rows.value = payload.items ?? []
    total.value = payload.total ?? 0
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结果表读取失败'
  }
}

async function createBatch() {
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      headers: store.tenantHeaders(),
      body: JSON.stringify({}),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload?.detail?.message ?? '稽核批次提交失败')
    }
    await reload()
    await openDetail(payload.entry as AuditRow)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '稽核批次提交失败'
  }
}

async function runAction(action: string, row: AuditRow) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      headers: store.tenantHeaders(),
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload?.detail?.message ?? '状态推进失败')
    }
    await reload()
    if (detail.value && detail.value.id === row.id) {
      detail.value = payload.entry as AuditRow
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '状态推进失败'
  }
}

async function openDetail(row: AuditRow) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`, { headers: store.tenantHeaders() })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload?.detail?.message ?? '明细读取失败')
    detail.value = payload as AuditRow
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '明细读取失败'
  }
}

// 切换租户或角色后重新按归属与权限加载，确保不残留他租户明细
watch(() => [store.tenantId, store.roleCode], () => {
  detail.value = null
  void reload()
})

onMounted(reload)
</script>

<style scoped>
.status-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  background: #eff8ff;
  color: #175cd3;
  font-size: 12px;
}
.readonly-tag {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 8px;
  background: #f2f4f7;
  color: #475467;
  font-size: 11px;
}
.detail-modal {
  width: min(880px, 100%);
}
.detail-table {
  margin: 8px 0 16px;
}
.conflict-box {
  border: 1px solid #fda29b;
  background: #fef3f2;
  color: #b42318;
  border-radius: 8px;
  padding: 10px 12px;
  margin: 10px 0;
}
.conflict-box p {
  margin: 4px 0 0;
  font-size: 13px;
}
.summary-box {
  border: 1px solid var(--border);
  background: #fafbfc;
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
}
</style>
