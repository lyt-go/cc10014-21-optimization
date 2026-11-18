<template>
  <section class="page" data-module="quality_check">
    <header class="page-head">
      <div>
        <h2>质量稽核管理</h2>
        <p class="page-desc">维护稽核规则的输入、执行约束与结果记录，按租户归属抽样执行，为分级稽核结果表提供可验证的稽核链路。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记稽核规则</button>
        <button class="btn" data-testid="submit-audit-batch" type="button" @click="submitAuditBatch">
          提交分级稽核
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出质量稽核清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <div v-if="executeMessage" class="audit-message" data-testid="execute-message" :class="executeOk ? 'ok' : 'fail'">
      {{ executeMessage }}
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>租户归属</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ tenantLabel(row.租户归属) }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`execute-${row.id}`"
              @click="executeRule(row)"
            >
              执行稽核
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">当前租户归属下暂无稽核规则，可先登记稽核规则</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记稽核规则</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条质量稽核记录（{{ store.tenantName }} / {{ store.roleName }}）</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { request } from '@/api/client'
import { useSessionStore } from '@/stores/session'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/quality_check'
const columns = ["规则编号", "校验表名", "校验字段", "校验表达式", "阈值下界", "阈值上界", "阻断级别", "采样比例"]
const actions = ["提交校验", "标记生效", "停用规则"]
const statuses = ["草稿", "待校验", "已生效", "已停用"]
const metricNames = ["稽核规则总数", "待校验规则", "已生效规则"]

const router = useRouter()
const store = useSessionStore()
const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const executeMessage = ref('')
const executeOk = ref(true)
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

const tenantNames = computed(() =>
  Object.fromEntries(store.tenants.map((tenant) => [tenant.id, tenant.name])))
function tenantLabel(owner: unknown): string {
  if (!owner) return '—'
  return tenantNames.value[String(owner)] ?? String(owner)
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'quality_check.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      headers: store.tenantHeaders(),
      body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      headers: store.tenantHeaders(),
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('质量稽核动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质量稽核操作失败'
  }
}

async function executeRule(row: Row) {
  errorMessage.value = ''
  executeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/execute`, {
      method: 'POST',
      headers: store.tenantHeaders(),
    })
    const payload = await response.json()
    if (!response.ok) {
      executeOk.value = false
      executeMessage.value = payload?.detail?.message ?? '稽核执行被拦截'
      return
    }
    executeOk.value = true
    const result = payload.result
    executeMessage.value =
      `规则 ${result.rule_id} 按租户归属抽样稽核：候选 ${result.total} 行、抽样 ${result.sampled} 行、` +
      `通过 ${result.passed}、失败 ${result.failed}、通过率 ${(result.pass_rate * 100).toFixed(2)}%`
  } catch (error) {
    executeOk.value = false
    executeMessage.value = error instanceof Error ? error.message : '稽核执行失败'
  }
}

async function submitAuditBatch() {
  errorMessage.value = ''
  try {
    const response = await request('/api/audit_result', {
      method: 'POST',
      headers: store.tenantHeaders(),
      body: JSON.stringify({}),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload?.detail?.message ?? payload?.message ?? '分级稽核提交失败')
    }
    await router.push('/audit_result')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分级稽核提交失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`, { headers: store.tenantHeaders() })
    if (!response.ok) {
      throw new Error('稽核规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质量稽核列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.audit-message {
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
  font-size: 13px;
  border: 1px solid var(--border);
}
.audit-message.ok {
  background: #ecfdf3;
  border-color: #a6f4c5;
  color: #027a48;
}
.audit-message.fail {
  background: #fef3f2;
  border-color: #fda29b;
  color: #b42318;
}
</style>
