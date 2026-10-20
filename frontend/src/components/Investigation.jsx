import { Json } from './Common'

export default function Investigation({ data }) {
  if (!data) return null
  const labels = {
    supported: '工具支持',
    contradicted: '已有反證',
    unresolved: '仍待查核',
    unknown: '未知',
    invalid: '格式錯誤，尚未驗證',
  }
  return (
    <section className="hypothesis-investigation">
      <h3>假說與驗證過程</h3>
      <p>
        {data.version} · {data.rounds}／3 輪 · {data.tools_used}／8 次工具查詢 · 停止原因：
        {{
          model_finished: '完成調查',
          round_budget: '已達調查輪數上限',
          tool_budget: '已達工具預算',
          model_error: '模型回應處理失敗',
          format_error: '回答格式錯誤，未能完成修正',
          model_timeout: '模型回應逾時',
          model_connection_error: '模型連線或服務請求失敗',
          evidence_incomplete: '調查達上限，證據仍不足',
          completed_with_warnings: '主要假說查核完成，部分條件仍待修正',
        }[data.stop_reason] || data.stop_reason}
      </p>
      <p>
        模型提出原因，唯讀工具檢查具體觀測。工具支持某些條件，不代表因果解釋必然正確；修復仍需政策檢查與人工核准。
      </p>
      {data.validation_issues?.length > 0 && (
        <section role="status">
          <p>部分回答格式不符，錯誤假說或方案不會取得修復資格；有效結果仍保留。</p>
          <Json value={data.validation_issues} />
        </section>
      )}
      {(data.candidate?.hypotheses || []).map((h) => {
        const assessment = (data.assessments || []).find((a) => a.hypothesis_id === h.id)
        return (
          <article key={h.id}>
            <h4>
              {h.id} · {h.cause} · {labels[assessment?.status] || '待查核'}
              {data.candidate.selected_hypothesis_id === h.id ? ' · 模型選定' : ''}
            </h4>
            <p>{h.explanation}</p>
            {assessment?.validation_errors?.length > 0 && (
              <Json value={assessment.validation_errors} />
            )}
            <p>查詢 Runbook：{h.search_query}</p>
            <p>必要查核缺口：{assessment?.missing_information?.join('；') || '無'}</p>
            <p>
              模型提出的查核問題：{h.missing_information?.join('；') || '無'}
              （是否已回答，以工具條件查核為準）
            </p>
            <p>後續調查：{h.follow_up_checks?.join('；') || '無'}</p>
            <ul>
              {assessment?.checks?.map((c, i) => (
                <li key={i}>
                  {labels[c.status]}：{c.evidence_id} · {c.path} {c.operator}{' '}
                  {JSON.stringify(c.expected)}
                  {c.reason === 'type_mismatch' && (
                    <span>
                      {' '}
                      · 型別不符：觀測為 {c.actual_type}，比較值為 {c.expected_type}；
                      {c.instruction}
                    </span>
                  )}
                  {c.reason === 'missing_evidence_or_field' && (
                    <span> · 找不到引用的證據或欄位</span>
                  )}
                  {c.reason === 'source_unavailable' && <span> · 來源失敗或資料截斷</span>}
                </li>
              ))}
            </ul>
            {assessment?.prior_contradiction_retained && (
              <p>先前輪次的反證仍保留，不因模型改寫條件而消失。</p>
            )}
            <details>
              <summary>此假說的文件引用</summary>
              <Json value={data.hypothesis_references?.[h.id] || []} />
            </details>
          </article>
        )
      })}
      <h4>修復方案比較</h4>
      {!data.candidate?.repair_options?.length ? (
        <p>無可比較的修復方案，需人工查核。</p>
      ) : (
        data.candidate.repair_options.map((p) => (
          <article key={p.id}>
            <p>
              {p.id} ·{' '}
              {p.action_type === 'invalid_repair_option' ? '方案格式或引用錯誤' : p.action_type} ·{' '}
              {data.candidate.selected_option_id === p.id ? '模型建議方案' : '替代方案'}
            </p>
            {data.repair_option_checks?.find((c) => c.option_id === p.id)?.validation_errors
              ?.length > 0 && (
              <Json
                value={
                  data.repair_option_checks.find((c) => c.option_id === p.id).validation_errors
                }
              />
            )}
            <p>依據：{p.rationale}</p>
            <p>風險：{p.risk}</p>
            <p>前置條件：{p.prerequisites?.join('；') || '無'}</p>
            <p>驗收方式：{p.verification}</p>
            <p>
              資格：
              {data.repair_option_checks?.find((c) => c.option_id === p.id)?.eligible_for_policy
                ? '可進入政策檢查，仍需人工核准'
                : '僅供人工參考，不能直接執行'}
            </p>
          </article>
        ))
      )}
      <details>
        <summary>逐輪假說、查核與引用紀錄</summary>
        <Json value={data.history} />
      </details>
    </section>
  )
}
