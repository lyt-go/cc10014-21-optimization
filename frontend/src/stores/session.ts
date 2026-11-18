import { defineStore } from 'pinia'

import { request } from '@/api/client'

export type TenantMeta = {
  id: string
  name: string
  system: string
  shared: boolean
}

export type RoleMeta = {
  name: string
  code: string
  permissions: string[]
}

export const useSessionStore = defineStore('session', {
  state: () => ({
    operator: '值班管理员',
    shiftLabel: '白班 08:00-20:00',
    scope: '数仓分层治理平台',
    // 多租户身份：请求头随每次调用带到后端，归属与权限由后端强制
    tenantId: 't_order',
    roleCode: 'admin',
    tenants: [] as TenantMeta[],
    roles: [] as RoleMeta[],
    contextPermissions: [] as string[],
  }),
  getters: {
    canOperate: (state) => state.operator.length > 0,
    tenantName: (state) => state.tenants.find((item) => item.id === state.tenantId)?.name ?? state.tenantId,
    roleName: (state) => state.roles.find((item) => item.code === state.roleCode)?.name ?? state.roleCode,
    can: (state) => (permission: string) => state.contextPermissions.includes(permission),
    isSharedTenant: (state) => state.tenantId === 'SHARED',
  },
  actions: {
    setShift(label: string) {
      this.shiftLabel = label
    },
    setTenant(tenantId: string) {
      this.tenantId = tenantId
      void this.refreshContext()
    },
    setRole(roleCode: string) {
      this.roleCode = roleCode
      void this.refreshContext()
    },
    tenantHeaders(): Record<string, string> {
      return { 'X-Tenant-Id': this.tenantId, 'X-Tenant-Role': this.roleCode }
    },
    async loadCatalog() {
      const response = await request('/api/audit_result/catalog')
      if (!response.ok) return
      const payload = await response.json()
      this.tenants = payload.tenants
      this.roles = payload.roles
      await this.refreshContext()
    },
    async refreshContext() {
      const response = await request('/api/audit_result/context', { headers: this.tenantHeaders() })
      if (!response.ok) {
        this.contextPermissions = []
        return
      }
      const payload = await response.json()
      this.contextPermissions = payload.permissions
    },
  },
})
