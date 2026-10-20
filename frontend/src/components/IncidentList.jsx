import { lifecycleLabel, stamp } from '../format'
import { Pagination } from './Common'

export default function IncidentList({
  rows,
  offset,
  status,
  busy,
  onStatus,
  onPage,
  onSelect,
  onRemove,
  onRemoveAll,
}) {
  return (
    <section>
      <div className="section-heading">
        <h2>事件列表</h2>
        <button className="danger" disabled={busy} onClick={onRemoveAll}>
          一鍵刪除所有事件
        </button>
      </div>
      <p>只移除已結束且無進行中診斷／修復的事件；影片檔案不受影響，稽核紀錄保留。</p>
      <label>
        事件狀態
        <select value={status} onChange={(event) => onStatus(event.target.value)}>
          <option value="">全部</option>
          {[
            'DETECTED',
            'INVESTIGATING',
            'DIAGNOSED',
            'WAITING_APPROVAL',
            'REMEDIATING',
            'VERIFYING',
            'RESOLVED',
            'RESOLVED_NO_ACTION',
            'ESCALATED',
            'REJECTED',
            'CANCELLED',
            'FAILED',
          ].map((state) => (
            <option key={state} value={state}>
              {lifecycleLabel(state)}
            </option>
          ))}
        </select>
      </label>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>故障</th>
              <th>嚴重度</th>
              <th>狀態</th>
              <th>Namespace／Pod</th>
              <th>首次／更新</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>
                  <button onClick={() => onSelect(row.id)}>{row.symptom}</button>
                </td>
                <td>
                  {{ info: '資訊', warning: '警告', critical: '嚴重' }[row.severity] ||
                    row.severity}
                </td>
                <td>{lifecycleLabel(row.lifecycle)}</td>
                <td>
                  {row.namespace}
                  <br />
                  {row.resource_id}
                </td>
                <td>
                  {stamp(row.first_seen_at)}
                  <br />
                  {stamp(row.last_seen_at)}
                </td>
                <td>
                  <button disabled={busy || row.status !== 'closed'} onClick={() => onRemove(row)}>
                    刪除事件
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!rows.length && <p>沒有符合條件的事件。</p>}
      <Pagination offset={offset} hasMore={rows.length === 20} onChange={onPage} disabled={busy} />
    </section>
  )
}
