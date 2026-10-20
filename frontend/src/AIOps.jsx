import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, requestJson } from './api'
import { usePolling } from './usePolling'
import { actionLabel, jobLabel, stamp } from './format'
import { RawData } from './components/Common'
import IncidentDetail from './components/IncidentDetail'
import IncidentList from './components/IncidentList'

const base = '/aiops/api'
const emptyActions = () => ({ items: [], remediation_enabled: false })

export default function AIOps({ token, setToken, videoJobId, onBack }) {
  const [input, setInput] = useState('')
  const [page, setPage] = useState(videoJobId ? 'Detail' : 'Overview')
  const [rows, setRows] = useState([])
  const [offset, setOffset] = useState(0)
  const [status, setStatus] = useState('')
  const [selected, setSelected] = useState(null)
  const [videoJob, setVideoJob] = useState(null)
  const [detail, setDetail] = useState(null)
  const [report, setReport] = useState(null)
  const [evidence, setEvidence] = useState([])
  const [timeline, setTimeline] = useState([])
  const [actions, setActions] = useState(emptyActions)
  const [knowledge, setKnowledge] = useState([])
  const [citations, setCitations] = useState([])
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [revision, setRevision] = useState(0)
  const citationCache = useRef(new Map())
  const mutation = useRef(null)
  useEffect(() => () => mutation.current?.abort(), [])

  const resetDetail = useCallback(() => {
    setDetail(null)
    setReport(null)
    setActions(emptyActions())
    setEvidence([])
    setTimeline([])
    setCitations([])
  }, [])
  useEffect(() => {
    setSelected(null)
    resetDetail()
    setVideoJob(null)
    setPage(videoJobId ? 'Detail' : 'Overview')
  }, [videoJobId, resetDetail])
  useEffect(() => {
    if (!token) {
      resetDetail()
      setSelected(null)
      setRows([])
      setVideoJob(null)
      setKnowledge([])
      citationCache.current.clear()
      setNotice('')
    }
  }, [token, resetDetail])
  const request = useCallback(
    (path, options = {}) =>
      requestJson(base + path, {
        ...options,
        token,
        onUnauthorized: () => {
          setError('登入已失效，請重新輸入操作員憑證。')
          setToken('')
        },
      }),
    [token, setToken],
  )

  const load = useCallback(
    async (signal) => {
      if (videoJobId) {
        const linked = await request('/video-jobs/' + videoJobId, { signal })
        if (signal.aborted) return
        setVideoJob(linked.job)
        if (!selected && linked.incidents.length && page === 'Detail') {
          setSelected(linked.incidents[0].id)
          return
        }
      }
      if (page === 'Overview' || page === 'Incidents') {
        const list = await request(
          `/incidents?limit=20&offset=${offset}${status ? '&status=' + status : ''}`,
          { signal },
        )
        if (signal.aborted) return
        if (!list.items.length && offset > 0) {
          setOffset(Math.max(0, offset - 20))
          return
        }
        setRows(list.items)
      } else if (page === 'Knowledge') {
        const data = await request('/knowledge/documents?limit=100', { signal })
        if (!signal.aborted) setKnowledge(data.items)
      } else if (selected) {
        const [incident, sources, history, plans, diagnosis] = await Promise.all([
          request('/incidents/' + selected, { signal }),
          request('/incidents/' + selected + '/evidence?limit=100', { signal }),
          request('/incidents/' + selected + '/timeline?limit=100', { signal }),
          request('/incidents/' + selected + '/actions', { signal }),
          request('/incidents/' + selected + '/report', { signal }).catch((failure) => {
            if (failure instanceof ApiError && failure.status === 404) return null
            throw failure
          }),
        ])
        const refs = await Promise.all(
          (diagnosis?.diagnosis.runbook_chunk_ids || []).map(async (id) => {
            if (citationCache.current.has(id)) return citationCache.current.get(id)
            const chunk = await request('/knowledge/chunk?chunk_id=' + encodeURIComponent(id), {
              signal,
            })
            if (!signal.aborted) citationCache.current.set(id, chunk)
            return chunk
          }),
        )
        if (signal.aborted) return
        setDetail(incident)
        setEvidence(sources.items)
        setTimeline(history.items)
        setActions(plans)
        setReport(diagnosis)
        setCitations(refs)
      }
    },
    [videoJobId, selected, page, offset, status, request],
  )
  const poll = usePolling(load, { enabled: !!token && !busy, revision })

  async function mutate(task) {
    const controller = new AbortController()
    mutation.current = controller
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await task(controller.signal)
    } catch (failure) {
      if (failure.name !== 'AbortError') setError(failure.message)
    } finally {
      if (!controller.signal.aborted) {
        setBusy(false)
        setRevision((value) => value + 1)
      }
      mutation.current = null
    }
  }
  function decide(action, decision) {
    const reason = window.prompt(
      `請填寫${decision === 'approved' ? '核准' : '拒絕'}理由（至少 5 字）`,
    )
    if (reason === null) return
    if (reason.trim().length < 5) {
      setError('理由至少 5 字。')
      return
    }
    if (
      !window.confirm(
        `${decision === 'approved' ? '核准' : '拒絕'}「${actionLabel(action.action_type)}」？\n目標：${action.target.namespace}/${action.target.deployment}\n原影片：${action.params.job_id || '—'}\n核准期限：${stamp(action.expires_at)}\n服務端會重新查核證據、設定綁定與政策。`,
      )
    )
      return
    mutate(async (signal) => {
      const result = await request(`/incidents/${selected}/actions/${action.id}/decision`, {
        method: 'POST',
        body: JSON.stringify({
          decision,
          reason: reason.trim(),
          binding_hash: action.binding_hash,
        }),
        signal,
      })
      setActions((old) => ({
        ...old,
        items: old.items.map((item) => (item.id === result.id ? result : item)),
      }))
      setNotice('決策已儲存；執行結果由服務端確認。')
    })
  }
  function removeIncident(incident) {
    if (!window.confirm(`刪除歷史事件 ${incident.id}？\n證據與核准稽核保留，影片檔案不受影響。`))
      return
    mutate(async (signal) => {
      await request('/incidents/' + incident.id, { method: 'DELETE', signal })
      setRows((old) => old.filter((item) => item.id !== incident.id))
      setNotice('歷史事件已刪除，稽核紀錄保留。')
    })
  }
  function removeAllIncidents() {
    if (
      !window.confirm(
        '一鍵刪除所有事件？\n包含所有分頁與狀態；處理中或等待核准的事件會保留。\n證據與核准稽核保留，影片檔案不會刪除。',
      )
    )
      return
    mutate(async (signal) => {
      const result = await request('/incidents', { method: 'DELETE', signal })
      setOffset(0)
      setStatus('')
      setSelected(null)
      resetDetail()
      setNotice(
        `已刪除 ${result.deleted_count} 筆歷史事件，保留 ${result.retained_count} 筆處理中或尚不可刪除的事件。`,
      )
    })
  }
  const reverify = (action) =>
    mutate(async (signal) => {
      const result = await request(`/incidents/${selected}/actions/${action.id}/verify`, {
        method: 'POST',
        body: '{}',
        signal,
      })
      setNotice(result.message)
    })
  const analyze = () =>
    mutate(async (signal) => {
      const result = await request('/incidents/' + selected + '/analyze', {
        method: 'POST',
        headers: { 'Idempotency-Key': crypto.randomUUID() },
        body: '{}',
        signal,
      })
      setNotice(result.reopened ? '已重新查核並開啟事件；新草案仍需重新核准。' : '已排入診斷佇列。')
    })
  const selectIncident = (id) => {
    setSelected(id)
    resetDetail()
    setPage('Detail')
    setError('')
    setNotice('')
  }
  function navigate(next) {
    setPage(next)
    setError('')
    setNotice('')
    if (next === 'Overview') {
      setOffset(0)
      setStatus('')
    }
  }

  if (!token)
    return (
      <main className="aiops">
        <h1>AIOps 故障診斷</h1>
        <p>憑證只存在頁面記憶體，重新整理後須重新登入。</p>
        <form
          onSubmit={(event) => {
            event.preventDefault()
            setToken(input.trim())
            setInput('')
            setError('')
          }}
        >
          <label>
            操作員憑證
            <input
              type="password"
              autoComplete="off"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              required
            />
          </label>
          <button>登入</button>
        </form>
        {(error || poll.error) && <p role="alert">{error || poll.error}</p>}
      </main>
    )
  return (
    <main className="aiops">
      <header className="section-heading">
        <h1>AIOps 故障診斷</h1>
        <button onClick={() => setToken('')}>登出</button>
      </header>
      <nav className="tabs" aria-label="診斷導覽">
        {[
          ['Overview', '總覽'],
          ['Incidents', '事件列表'],
          ['Knowledge', '知識文件'],
        ].map(([key, label]) => (
          <button
            key={key}
            aria-current={page === key ? 'page' : undefined}
            onClick={() => navigate(key)}
          >
            {label}
          </button>
        ))}
      </nav>
      {(error || poll.error) && <p role="alert">{error || poll.error}</p>}
      {notice && <p role="status">{notice}</p>}
      <small>
        {poll.loading
          ? '正在載入…'
          : poll.updatedAt
            ? `更新時間：${poll.updatedAt.toLocaleTimeString('zh-TW')}`
            : '等待更新'}
      </small>
      {page === 'Overview' && (
        <section>
          <h2>事件總覽</h2>
          <p>自動更新最近 20 筆事件，下列統計僅計算此頁資料。</p>
          <p>
            載入 {rows.length} 筆 · 未結束 {rows.filter((row) => row.status === 'open').length} 筆 ·
            轉交人工 {rows.filter((row) => row.lifecycle === 'ESCALATED').length} 筆
          </p>
          <p>診斷通過 → 政策查核 → 人工核准 → 執行修復 → 復原驗證。</p>
          <button onClick={() => navigate('Incidents')}>查看事件</button>
        </section>
      )}
      {page === 'Incidents' && (
        <IncidentList
          rows={rows}
          offset={offset}
          status={status}
          busy={busy}
          onStatus={(value) => {
            setStatus(value)
            setOffset(0)
          }}
          onPage={setOffset}
          onSelect={selectIncident}
          onRemove={removeIncident}
          onRemoveAll={removeAllIncidents}
        />
      )}
      {page === 'Knowledge' && (
        <section>
          <h2>知識文件</h2>
          <p>版本化 Runbook 只提供參考，不授權執行操作。目前最多顯示 100 份。</p>
          {knowledge.map((document) => (
            <article key={document.document_id}>
              <h3>{document.title}</h3>
              <p>
                {document.version} · {document.status} · {document.source_path}
              </p>
              <RawData title="查看來源與 checksum" value={document} />
            </article>
          ))}
          {!knowledge.length && !poll.loading && <p>尚無知識文件。</p>}
        </section>
      )}
      {videoJobId && (
        <section className="video-recovery">
          <h2>影片診斷與修復</h2>
          <p>
            {videoJob?.original_filename || videoJobId} ·{' '}
            {jobLabel(videoJob?.job_status) || '讀取中'}
          </p>
          <p>上傳 → 處理失敗 → Gemma 診斷 → 人工核准 → 重跑 → 播放成果</p>
          {videoJob?.job_status === 'COMPLETED' && (
            <p>
              <a href={`/video/api/jobs/${videoJobId}/video`} target="_blank" rel="noreferrer">
                播放影片
              </a>{' '}
              · <a href={`/video/api/jobs/${videoJobId}/download`}>下載影片</a>
            </p>
          )}
          <button onClick={onBack}>回影片列表</button>
        </section>
      )}
      {page === 'Detail' &&
        (detail ? (
          <IncidentDetail
            detail={detail}
            report={report}
            actions={actions}
            evidence={evidence}
            timeline={timeline}
            citations={citations}
            videoJobId={videoJobId}
            busy={busy}
            onAnalyze={analyze}
            onDecision={decide}
            onVerify={reverify}
            onBack={onBack}
          />
        ) : (
          <p>
            {videoJob?.job_status === 'COMPLETED'
              ? '這支影片已完成；一般成功任務不會產生故障事件。'
              : videoJobId
                ? '正在查找這支影片的故障事件。'
                : '正在載入事件…'}
          </p>
        ))}
    </main>
  )
}
