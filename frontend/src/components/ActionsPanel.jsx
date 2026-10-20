import { useEffect, useState } from 'react'
import { actionLabel, approvalLabel, executionLabel, stamp } from '../format'
import { RawData } from './Common'

export default function ActionsPanel({ actions, report, detail, busy, onDecision, onVerify }) {
  const [clock, setClock] = useState(Date.now())
  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  const current = actions.items.filter((action) => action.run_id === report?.run_id)
  const historical = actions.items.filter((action) => action.run_id !== report?.run_id)
  const active = detail.runs.some((run) => ['pending', 'running', 'waiting'].includes(run.status))
  function renderAction(action, history = false) {
    const expiry = Date.parse(
      /[zZ]|[+-]\d\d:\d\d$/.test(action.expires_at) ? action.expires_at : action.expires_at + 'Z',
    )
    const expired = !Number.isFinite(expiry) || expiry <= clock
    const supported =
      action.target?.namespace === 'video-platform' &&
      [
        'retry_video_ffmpeg_h264',
        'restore_video_redis_and_retry',
        'increase_video_worker_memory_and_retry',
      ].includes(action.action_type)
    let reason =
      history || !supported ? '歷史或不支援的方案僅供查閱。' : action.review?.reason || ''
    if (!reason && action.approval_status !== 'pending') reason = '此方案目前不是等待核准狀態。'
    if (!reason && expired) reason = '方案已過期，請重新診斷取得新草案。'
    if (!reason && report?.validation_status !== 'validated') reason = '最新診斷尚未通過證據驗證。'
    if (!reason && !actions.remediation_enabled) reason = '受限修復尚未啟用。'
    if (!reason && action.execution_status !== 'pending')
      reason = '方案已進入執行流程，不能重複核准。'
    const canApprove = !history && !reason && action.review?.can_approve !== false
    const canReject =
      !history &&
      supported &&
      !expired &&
      action.approval_status === 'pending' &&
      action.execution_status === 'pending' &&
      action.review?.can_reject !== false
    return (
      <article key={action.id} className="action-card">
        <h4>{actionLabel(action.action_type)}</h4>
        <p>
          審核：{approvalLabel(action.approval_status)} · 執行：
          {executionLabel(action.execution_status)}
        </p>
        <p>
          核准期限：{stamp(action.expires_at)}
          {expired ? '（已過期）' : ''}
        </p>
        <p>
          目標：{action.target?.namespace}／{action.target?.deployment} · 影片：
          {action.params?.job_id || '—'}
        </p>
        {action.approval_status === 'pending' && reason && (
          <p className="blocked-reason">無法核准：{reason}</p>
        )}
        {action.execution_status === 'uncertain' && (
          <p>核准已記錄，但復原尚未確認；可查核既有成果，不會重跑影片。</p>
        )}
        <RawData
          title="查看方案參數與稽核紀錄"
          value={{
            target: action.target,
            params: action.params,
            reviewer: action.approval,
            result: action.result,
          }}
        />
        {!history && (
          <div className="actions">
            {action.execution_status === 'uncertain' &&
              action.approval_status === 'approved' &&
              action.target?.namespace === 'video-platform' && (
                <button disabled={busy || active} onClick={() => onVerify(action)}>
                  重新驗證（不重跑）
                </button>
              )}
            <button disabled={busy || !canApprove} onClick={() => onDecision(action, 'approved')}>
              核准
            </button>
            <button disabled={busy || !canReject} onClick={() => onDecision(action, 'rejected')}>
              拒絕
            </button>
          </div>
        )}
      </article>
    )
  }
  return (
    <section>
      <h3>人工核准</h3>
      <p>
        {actions.remediation_enabled
          ? '送出核准時，服務端會重新查核證據、政策與實際目標；核准不代表修復已完成。'
          : '受限修復目前關閉，方案不能核准執行。'}
      </p>
      {current.map((action) => renderAction(action))}
      {!current.length && <p>最新報告沒有待審核方案；請查看證據缺口及政策原因。</p>}
      {!!historical.length && (
        <details>
          <summary>歷史方案（{historical.length} 筆，僅供查閱）</summary>
          {historical.map((action) => renderAction(action, true))}
        </details>
      )}
    </section>
  )
}
