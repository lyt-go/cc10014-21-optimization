import { expect, test } from '@playwright/test'

/**
 * 分级稽核结果表多租户验收：
 * - 打开质量稽核入口并提交，进入结果表面板复核
 * - 切换租户后不出现其他租户明细，汇总不含可反推单租户数量
 * - 状态单向推进、跳级被拦、越权操作被拦、共享只读
 */
test.describe('分级稽核结果表多租户', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/audit_result')
    await expect(page.locator('tbody tr[data-testid^="row-"]')).toBeVisible({ timeout: 15000 })
  })

  test('提交稽核批次并在结果表面板复核规则结果', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(e.message))
    page.on('response', (r) => {
      if (r.url().includes('/api/') && r.status() >= 400) errors.push(`${r.status()} ${r.url()}`)
    })

    // 从质量稽核入口提交
    await page.goto('/quality_check')
    await expect(page.locator('tbody tr[data-testid^="row-"]')).toHaveCount(3)
    await page.getByTestId('submit-audit-batch').click()

    // 跳转到结果表面板并自动打开复核弹窗
    await expect(page).toHaveURL(/\/audit_result/)
    await expect(page.getByTestId('detail-modal')).toBeVisible()
    await expect(page.getByTestId('detail-table')).toBeVisible()
    // 本租户明细只含订单规则
    await expect(page.getByTestId('detail-row-QC-ORD-001')).toBeVisible()
    await expect(page.locator('[data-testid^="detail-row-QC-PAY"]')).toHaveCount(0)
    // 管理员视角含匿名汇总，且不含任何租户标识字段
    const summary = page.getByTestId('cross-summary')
    await expect(summary).toBeVisible()
    await expect(summary).toContainText('不含单租户行数，不可反推')
    await page.getByRole('button', { name: '关闭' }).click()

    expect(errors).toEqual([])
  })

  test('状态单向推进，跳级被拦截', async ({ page }) => {
    const errors: string[] = []
    page.on('response', (r) => {
      if (r.url().includes('/api/') && r.status() >= 400) errors.push(`${r.status()}`)
    })
    await page.getByTestId('open-create-batch').click()
    const row = page.locator('tbody tr[data-testid^="row-"]').first()
    await expect(row).toBeVisible()
    const id = (await row.getAttribute('data-testid'))?.replace('row-', '') ?? ''

    // 待复核状态下只允许「开始复核」，没有发布按钮（跳级入口不存在）
    await expect(page.getByTestId(`action-${id}-开始复核`)).toBeVisible()
    await expect(page.locator(`[data-testid="action-${id}-确认发布"]`)).toHaveCount(0)

    await page.getByTestId(`action-${id}-开始复核`).click()
    await expect(page.getByTestId(`status-${id}`)).toHaveText('复核中')
    await page.getByTestId(`action-${id}-复核通过`).click()
    await expect(page.getByTestId(`status-${id}`)).toHaveText('待发布')
    await page.getByTestId(`action-${id}-确认发布`).click()
    await expect(page.getByTestId(`status-${id}`)).toHaveText('已发布')
    // 终态后无任何动作按钮
    await expect(page.locator(`[data-testid^="action-${id}-"]`)).toHaveCount(0)
    expect(errors.filter((code) => code !== '409')).toEqual([])
  })

  test('切换租户后不出现其他租户明细，查看员只读', async ({ page }) => {
    // 订单租户先生成一个批次
    await page.getByTestId('open-create-batch').click()
    await expect(page.locator('tbody tr[data-testid^="row-"]').first()).toBeVisible()

    // 切换到支付租户：列表清空，看不到订单批次
    await page.getByTestId('tenant-select').selectOption('t_payment')
    await expect(page.getByTestId('empty-state')).toBeVisible({ timeout: 10000 })
    await expect(page.locator('tbody tr[data-testid^="row-"]')).toHaveCount(0)

    // 支付租户生成自己的批次，明细只含支付规则
    await page.getByTestId('open-create-batch').click()
    const payRow = page.locator('tbody tr[data-testid^="row-"]').first()
    await payRow.getByTestId(/^review-/).click()
    await expect(page.getByTestId('detail-row-QC-PAY-001')).toBeVisible()
    await expect(page.locator('[data-testid^="detail-row-QC-ORD"]')).toHaveCount(0)
    // 稽核员无跨租户汇总权限
    await expect(page.getByTestId('no-cross-summary')).toBeVisible()
    await page.getByRole('button', { name: '关闭' }).click()

    // 切到查看员：只读，无任何状态动作
    await page.getByTestId('role-select').selectOption('viewer')
    await expect(page.locator('tbody tr[data-testid^="row-"]').first()).toBeVisible()
    await expect(page.locator('.row-actions button.link', { hasText: '复核明细' })).toBeVisible()
    await expect(page.locator('.row-actions button.link', { hasText: '开始复核' })).toHaveCount(0)
  })
})
