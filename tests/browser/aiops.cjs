/* Isolated UI contracts. All APIs are intercepted; no real tokens, approval or repair. */
const { chromium } = require(process.env.PLAYWRIGHT_PACKAGE || 'playwright')
const assert = require('node:assert/strict'),
  fs = require('node:fs')
const base = process.env.UI_URL || 'http://127.0.0.1:13010'
const origin = new URL(base).origin
assert(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname))
const iid = '00000000-0000-0000-0000-000000000001',
  aid = '00000000-0000-0000-0000-000000000002',
  jid = '00000000-0000-0000-0000-000000000003'
const token = 'synthetic-browser-token',
  hash = 'a'.repeat(64)
const source = {
  name: 'browser-v2.mp4',
  mimeType: 'video/mp4',
  buffer: Buffer.from('synthetic MP4 fixture'),
}
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function fixture(options = {}) {
  const row = {
    id: iid,
    symptom: 'VideoJobFailure',
    severity: 'warning',
    namespace: 'video-platform',
    resource_id: 'worker-fixture',
    lifecycle: 'WAITING_APPROVAL',
    status: 'open',
    runs: [{ id: 'run', status: 'waiting', workflow_kind: 'aiops-hypothesis-v2' }],
    video_job: { job_id: jid },
    first_seen_at: '2026-10-20T00:00:00Z',
    last_seen_at: '2026-10-20T00:00:00Z',
  }
  const action = {
    id: aid,
    run_id: 'run',
    action_type: 'retry_video_ffmpeg_h264',
    target: { namespace: 'video-platform', deployment: 'worker' },
    params: { job_id: jid, video_codec: 'h264' },
    binding_hash: hash,
    approval_status: 'pending',
    execution_status: 'pending',
    expires_at: '2099-10-20T00:00:00Z',
    result: null,
    review: { can_approve: true, can_reject: true, reason: '', policy_recheck_required: true },
    ...options.action,
  }
  const report = {
    run_id: 'run',
    model_id: 'synthetic-v2-ui-fixture',
    validation_status: 'validated',
    created_at: '2026-10-20T00:00:00Z',
    diagnosis: {
      action_id: aid,
      root_cause_code: 'FFMPEG_INVALID_CODEC',
      summary: '原任務回報 Unknown encoder',
      alternative_causes: [],
      missing_evidence: [],
      follow_up_checks: [],
      proposed_action_type: action.action_type,
      evidence_ids: ['evidence'],
      runbook_chunk_ids: [],
      recovery_verified: false,
      investigation: {
        version: 'hypothesis-rag-v2',
        field_catalog_version: 'scalar-catalog-v2-stable',
        rounds: 2,
        tools_used: 4,
        stop_reason: 'model_finished',
        candidate: { hypotheses: [], repair_options: [] },
        assessments: [],
        history: [],
      },
    },
  }
  const job = {
    id: jid,
    original_filename: 'browser-v2.mp4',
    status: 'FAILED',
    job_status: 'FAILED',
    progress: 100,
    video_codec: 'h264',
    target_resolution: '480p',
    compression_preset: 'balanced',
    thumbnail_available: false,
  }
  return {
    ...options,
    row,
    action,
    report,
    job,
    jobs: options.jobs || [],
    calls: [],
    decisions: [],
    deleted: [],
    errors: [],
    activeReads: 0,
    maxReads: 0,
    uploaded: false,
  }
}

async function setup(browser, options = {}) {
  const data = fixture(options),
    page = await browser.newPage({ viewport: { width: 1280, height: 1000 } })
  page.setDefaultTimeout(8000)
  page.on('pageerror', (error) => data.errors.push(error.message))
  await page.route('**/*', async (route) => {
    const request = route.request(),
      u = new URL(request.url()),
      path = u.pathname
    if (u.origin !== origin) return route.abort()
    if (!path.startsWith('/video/api/') && !path.startsWith('/aiops/api/')) return route.continue()
    data.calls.push({ method: request.method(), path, query: u.search })
    let body,
      status = 200
    if (path.startsWith('/video/api/')) {
      if (request.method() === 'POST') {
        assert.equal(path, '/video/api/jobs')
        if (data.uploadFailure === 'network') return route.abort('failed')
        if (data.uploadFailure === 'html')
          return route.fulfill({
            status: 502,
            contentType: 'text/html',
            body: '<html>proxy unavailable</html>',
          })
        assert.equal(request.headers().authorization, 'Bearer ' + token)
        assert(request.postDataBuffer().toString().includes('demonstrate_failure'))
        data.uploaded = true
        data.jobs = [data.job]
        body = data.job
        status = 202
      } else if (request.method() === 'DELETE') {
        const id = path.split('/').pop()
        data.deleted.push(id)
        data.jobs = data.jobs.filter((job) => job.id !== id)
        return route.fulfill({ status: 204 })
      } else {
        if (path === '/video/api/jobs') {
          assert.equal(u.searchParams.get('limit'), '21')
          data.activeReads++
          data.maxReads = Math.max(data.maxReads, data.activeReads)
          if (data.slowRead) await pause(data.slowRead)
          const offset = Number(u.searchParams.get('offset'))
          body = data.jobs.slice(offset, offset + 21)
          data.activeReads--
        } else body = data.job
      }
    } else {
      assert.equal(request.headers().authorization, 'Bearer ' + token)
      if (data.unauthorized)
        return route.fulfill({
          status: 401,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Unauthorized' }),
        })
      if (path.endsWith('/decision')) {
        const decision = request.postDataJSON()
        assert.equal(decision.binding_hash, hash)
        assert.deepEqual(Object.keys(decision).sort(), ['binding_hash', 'decision', 'reason'])
        data.decisions.push(decision)
        data.action.approval_status = decision.decision
        data.action.review = {
          can_approve: false,
          can_reject: false,
          reason: '決策已記錄',
          policy_recheck_required: true,
        }
        body = data.action
      } else if (path.includes('/video-jobs/')) body = { job: data.job, incidents: [data.row] }
      else if (path.endsWith('/actions'))
        body = {
          items: [data.action, ...(data.historical || [])],
          remediation_enabled: data.enabled !== false,
        }
      else if (path.endsWith('/report')) body = data.report
      else if (path.endsWith('/evidence'))
        body = {
          items: [
            {
              evidence_id: 'evidence',
              tool_name: 'get_task_status',
              source: 'video_task_api',
              observed_at: '2026-10-20T00:00:00Z',
              truncated: false,
              errors: [],
              data: { error: 'Unknown encoder' },
            },
          ],
        }
      else if (path.endsWith('/timeline') || path.includes('/knowledge/')) body = { items: [] }
      else if (path.endsWith('/' + iid)) body = data.row
      else body = { items: [data.row] }
    }
    try {
      await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    } catch (error) {
      if (!page.isClosed() && !/Target.*closed|already handled/.test(error.message)) throw error
    }
  })
  await page.goto(base)
  return { page, data }
}
async function login(page) {
  await page.getByRole('button', { name: 'AIOps 智慧診斷', exact: true }).click()
  await page.getByLabel('操作員憑證').fill(token)
  await page.getByRole('button', { name: '登入', exact: true }).click()
}
async function openIncident(page) {
  await login(page)
  await page.getByRole('button', { name: '事件列表', exact: true }).click()
  await page.getByRole('button', { name: 'VideoJobFailure', exact: true }).click()
  await page.getByRole('heading', { name: '假說與驗證過程', exact: true }).waitFor()
}

;(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] }),
    passed = []
  async function scenario(name, options, run) {
    if (process.env.BROWSER_CASE && !name.includes(process.env.BROWSER_CASE)) return
    const { page, data } = await setup(browser, options)
    try {
      await run(page, data)
      assert.deepEqual(data.errors, [])
      passed.push(name)
    } finally {
      await page.close()
    }
  }
  try {
    await scenario('上傳→事件→人工核准，重新整理清除憑證', {}, async (page, data) => {
      await login(page)
      await page.getByRole('button', { name: '影片平台', exact: true }).click()
      await page.getByLabel('MP4 影片').setInputFiles(source)
      await page.getByRole('checkbox').check()
      await page.getByRole('button', { name: '上傳並處理', exact: true }).click()
      await page.getByRole('heading', { name: '假說與驗證過程', exact: true }).waitFor()
      assert(data.uploaded)
      assert(await page.getByRole('button', { name: '核准', exact: true }).isEnabled())
      assert.equal(data.decisions.length, 0)
      assert((await page.locator('main.aiops').innerText()).includes('未驗證'))
      page.on('dialog', (dialog) =>
        dialog.type() === 'prompt' ? dialog.accept('核對原影片與方案，核准測試') : dialog.accept(),
      )
      await page.getByRole('button', { name: '核准', exact: true }).click()
      await page.getByRole('status').filter({ hasText: '決策已儲存' }).waitFor()
      assert.equal(data.decisions[0].decision, 'approved')
      assert.deepEqual(
        await page.evaluate(() => [localStorage.length, sessionStorage.length]),
        [0, 0],
      )
      await page.reload()
      await page.getByRole('button', { name: 'AIOps 智慧診斷', exact: true }).click()
      await page.getByRole('button', { name: '登入', exact: true }).waitFor()
      assert(
        !data.calls.some((call) => call.path.endsWith('/repair') || call.path.endsWith('/verify')),
      )
    })
    await scenario('獨立待審方案可拒絕', {}, async (page, data) => {
      await openIncident(page)
      page.on('dialog', (dialog) =>
        dialog.type() === 'prompt' ? dialog.accept('此測試方案由操作員拒絕') : dialog.accept(),
      )
      await page.getByRole('button', { name: '拒絕', exact: true }).click()
      await page.getByRole('status').filter({ hasText: '決策已儲存' }).waitFor()
      assert.equal(data.decisions[0].decision, 'rejected')
    })
    for (const kind of ['network', 'html'])
      await scenario('上傳失敗恢復按鈕：' + kind, { uploadFailure: kind }, async (page) => {
        await page.getByLabel('MP4 影片').setInputFiles(source)
        await page.getByRole('button', { name: '上傳並處理', exact: true }).click()
        await page
          .getByRole('status')
          .filter({ hasText: kind === 'html' ? '無法讀取' : '無法連線' })
          .waitFor()
        assert(await page.getByRole('button', { name: '上傳並處理', exact: true }).isEnabled())
      })
    await scenario(
      '後端政策提示與歷史方案僅供查閱',
      {
        action: {
          review: { can_approve: false, can_reject: true, reason: '目標設定已改變，請重新診斷。' },
        },
        historical: [
          {
            id: 'historical',
            run_id: 'old',
            action_type: 'retry_lab_ffmpeg_h264',
            target: { namespace: 'chaos-lab' },
            params: {},
            approval_status: 'pending',
            execution_status: 'pending',
            expires_at: '2099-01-01T00:00:00Z',
          },
        ],
      },
      async (page, data) => {
        await openIncident(page)
        assert(await page.getByRole('button', { name: '核准', exact: true }).isDisabled())
        await page.getByText('無法核准：目標設定已改變，請重新診斷。', { exact: true }).waitFor()
        await page.getByText('歷史方案（1 筆，僅供查閱）', { exact: true }).click()
        assert.equal(await page.getByRole('button', { name: '核准', exact: true }).count(), 1)
        assert.equal(data.decisions.length, 0)
      },
    )
    await scenario(
      '核准期限到期即停用',
      { action: { expires_at: new Date(Date.now() + 8000).toISOString() } },
      async (page) => {
        await openIncident(page)
        assert(await page.getByRole('button', { name: '核准', exact: true }).isEnabled())
        await page
          .getByText('無法核准：方案已過期，請重新診斷取得新草案。', { exact: true })
          .waitFor({ timeout: 12000 })
        assert(await page.getByRole('button', { name: '核准', exact: true }).isDisabled())
      },
    )
    const jobs = Array.from({ length: 25 }, (_, index) => ({
      id: 'job-' + index,
      original_filename: `影片-${index}.mp4`,
      status: 'FAILED',
      progress: 100,
      video_codec: 'h264',
      target_resolution: '480p',
    }))
    await scenario('影片分頁與刪除只作用於本頁', { jobs }, async (page, data) => {
      await page.getByRole('cell').filter({ hasText: '影片-0.mp4' }).waitFor()
      assert.equal(await page.locator('tbody tr').count(), 20)
      await page.getByRole('button', { name: '下一頁', exact: true }).click()
      await page.getByRole('cell').filter({ hasText: '影片-20.mp4' }).waitFor()
      page.on('dialog', (dialog) => {
        assert(dialog.message().includes('此頁 5 支'))
        return dialog.accept()
      })
      await page.getByRole('button', { name: '刪除此頁已結束影片', exact: true }).click()
      await page.getByRole('status').filter({ hasText: '已刪除 5 支影片' }).waitFor()
      assert.deepEqual(data.deleted, ['job-20', 'job-21', 'job-22', 'job-23', 'job-24'])
      assert.equal(data.jobs.length, 20)
    })
    await scenario('慢請求完成後才安排下次更新', { slowRead: 4200 }, async (page, data) => {
      await pause(8000)
      assert.equal(data.maxReads, 1)
      assert.equal(data.calls.filter((call) => call.path === '/video/api/jobs').length, 2)
      await page.getByRole('button', { name: 'AIOps 智慧診斷', exact: true }).click()
      await page.getByRole('button', { name: '登入', exact: true }).waitFor()
    })
    await scenario('過期登入明確提示', { unauthorized: true }, async (page) => {
      await login(page)
      await page.getByRole('alert').filter({ hasText: '登入已失效' }).waitFor()
      await page.getByRole('button', { name: '登入', exact: true }).waitFor()
    })
    await scenario('手機版表格捲動且頁面不溢出', { jobs }, async (page) => {
      await page.setViewportSize({ width: 390, height: 844 })
      await page.getByRole('cell').filter({ hasText: '影片-0.mp4' }).waitFor()
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
      assert(
        await page.locator('.table-scroll').evaluate((node) => node.scrollWidth > node.clientWidth),
      )
      if (process.env.SCREENSHOT_DIR) {
        fs.mkdirSync(process.env.SCREENSHOT_DIR, { recursive: true })
        await page.screenshot({ path: process.env.SCREENSHOT_DIR + '/mobile.png', fullPage: true })
      }
      await openIncident(page)
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
      if (process.env.SCREENSHOT_DIR)
        await page.screenshot({
          path: process.env.SCREENSHOT_DIR + '/incident.png',
          fullPage: true,
        })
    })
    const result = {
      workflow: 'hypothesis-rag-v2',
      intercepted_api_fixtures: true,
      passed,
      scenarios: passed.length,
      javascript_errors: [],
      live_approvals: 0,
      live_repairs: 0,
    }
    if (process.env.BROWSER_OUTPUT)
      fs.writeFileSync(process.env.BROWSER_OUTPUT, JSON.stringify(result, null, 2) + '\n')
    console.log(JSON.stringify(result))
  } finally {
    await browser.close()
  }
})().catch((error) => {
  console.error(error.stack)
  process.exit(1)
})
