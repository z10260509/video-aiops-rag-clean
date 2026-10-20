import { actionLabel, lifecycleLabel, stamp, validationLabel } from '../format'
import { Json, RawData } from './Common'
import Investigation from './Investigation'
import ActionsPanel from './ActionsPanel'

function Recovery({ action }) {
  const proof = action?.result?.verification,
    sample = proof?.fresh_sample
  let message = '目前沒有可執行的修復方案，尚未進行復原驗證。'
  if (action)
    message =
      action.approval_status === 'pending'
        ? '等待人工核准，尚未進行復原驗證。'
        : '尚未取得完整復原結果，請查看方案的執行狀態。'
  return (
    <section>
      <h3>復原驗證結果</h3>
      {!sample ? (
        <p>{message}</p>
      ) : (
        <>
          <p>
            任務完成：
            {sample.sample_status === 'COMPLETED' && sample.new_outcome_completed ? '是' : '否'} ·
            輸出編碼：{sample.output_codec || '未取得'}
          </p>
          <p>
            Worker 可用副本：{proof.available_replicas}／{proof.desired_replicas ?? '未記錄目標數'}
          </p>
          <p>
            {action.execution_status === 'succeeded'
              ? '完整復原檢查已通過。'
              : '部分查核已有結果，完整復原檢查仍未通過。'}
          </p>
          <RawData title="查看復原任務與詳細證明" value={proof} />
        </>
      )}
    </section>
  )
}

const sourceLabel = (source) =>
  ({
    video_task_api: '原影片私有 API：worker 保存的任務結果、FFmpeg 錯誤及 Pod 身分',
    kubernetes: 'Kubernetes API：Pod 狀態、事件與日誌',
    prometheus: 'Prometheus：指標與佇列深度',
    loki: 'Loki：容器日誌',
    task_database: '歷史演練任務資料庫',
  })[source] || source

export default function IncidentDetail({
  detail,
  report,
  actions,
  evidence,
  timeline,
  citations,
  videoJobId,
  busy,
  onAnalyze,
  onDecision,
  onVerify,
  onBack,
}) {
  const active = detail.runs.some((run) => ['pending', 'running', 'waiting'].includes(run.status))
  const newer = detail.runs.some(
    (run) => run.id !== report?.run_id && ['pending', 'running', 'waiting'].includes(run.status),
  )
  const diagnosis = report?.diagnosis
  return (
    <section>
      <div className="section-heading">
        <h2>事件詳情 · {detail.symptom}</h2>
        <button onClick={onAnalyze} disabled={busy || active}>
          重新診斷
        </button>
      </div>
      <p>
        事件狀態：{lifecycleLabel(detail.lifecycle)} · {detail.namespace}／{detail.resource_id}
      </p>
      <RawData
        title="查看事件與診斷執行識別碼"
        value={{ incident_id: detail.id, video_job: detail.video_job, runs: detail.runs }}
      />
      {detail.video_job && !videoJobId && (
        <p>
          <button onClick={onBack}>回影片列表</button>
          {diagnosis?.recovery_verified && (
            <>
              {' '}
              ·{' '}
              <a
                href={`/video/api/jobs/${detail.video_job.job_id}/video`}
                target="_blank"
                rel="noreferrer"
              >
                播放修復成果
              </a>{' '}
              · <a href={`/video/api/jobs/${detail.video_job.job_id}/download`}>下載影片</a>
            </>
          )}
        </p>
      )}
      {newer && report && <p role="status">新診斷進行中；下方是前次報告，請等待新的查核結果。</p>}
      <section className="report-summary">
        <h3>診斷結果</h3>
        {diagnosis ? (
          <>
            <p>
              <strong>{validationLabel(report.validation_status)}</strong> · {report.model_id} ·{' '}
              {stamp(report.created_at)}
            </p>
            <p>
              <strong>{diagnosis.root_cause_code}</strong>：{diagnosis.summary}
            </p>
            <p>替代原因（尚未確認）：{diagnosis.alternative_causes?.join('；') || '無'}</p>
            <p>
              缺少證據／不確定：
              {[
                ...new Set([
                  ...(diagnosis.missing_evidence || []),
                  ...(diagnosis.validation_gaps || []),
                ]),
              ].join('；') || '引用查核已完成，因果解釋仍須操作者核對。'}
            </p>
            <p>後續查核：{diagnosis.follow_up_checks?.join('；') || '無'}</p>
            <p>
              修復建議：{actionLabel(diagnosis.proposed_action_type)} ·{' '}
              {diagnosis.policy_reason || diagnosis.policy_status}
            </p>
            <p>復原驗證：{diagnosis.recovery_verified ? '已驗證' : '未驗證'}</p>
          </>
        ) : (
          <p>尚未產生報告，請查看診斷執行狀態。</p>
        )}
      </section>
      <ActionsPanel
        actions={actions}
        report={report}
        detail={detail}
        busy={busy}
        onDecision={onDecision}
        onVerify={onVerify}
      />
      <Recovery action={actions.items.find((action) => action.id === diagnosis?.action_id)} />
      <Investigation data={diagnosis?.investigation} />
      <details>
        <summary>Runbook 引用（{citations.length} 筆）</summary>
        {citations.map((chunk) => (
          <article key={chunk.chunk_id}>
            <h4>
              {chunk.title} · {chunk.heading}
            </h4>
            <p>
              版本：{chunk.version} · {chunk.source_path}
            </p>
            <pre className="aiops-json">{chunk.content}</pre>
          </article>
        ))}
      </details>
      <details>
        <summary>證據事實與來源（本頁最多 100 筆）</summary>
        <p>這些是系統唯讀採集的快照，並非 Gemma 生成。parsed_lines 是從原始日誌解析的檢視。</p>
        {evidence.map((item) => (
          <details key={item.evidence_id}>
            <summary>
              {item.tool_name} · {stamp(item.observed_at)}{' '}
              {diagnosis?.evidence_ids?.includes(item.evidence_id) ? '（診斷引用）' : ''}
            </summary>
            <p>
              {sourceLabel(item.source)} · {item.truncated ? '已截斷' : '未截斷'} ·{' '}
              {item.errors?.join('；')}
            </p>
            <Json value={item} />
          </details>
        ))}
      </details>
      <details>
        <summary>事件時間軸與核准稽核（本頁最多 100 筆）</summary>
        <ol>
          {timeline.map((item) => (
            <li key={item.id}>
              {stamp(item.created_at)} · {item.actor} · {item.event_type}
              <RawData value={item.payload} />
            </li>
          ))}
        </ol>
      </details>
    </section>
  )
}
