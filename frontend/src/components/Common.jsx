export function Json({ value }) {
  return <pre className="aiops-json">{JSON.stringify(value, null, 2)}</pre>
}
export function RawData({ value, title = '查看原始資料' }) {
  return (
    <details>
      <summary>{title}</summary>
      <Json value={value} />
    </details>
  )
}
export function Pagination({ offset, size = 20, hasMore, onChange, disabled = false }) {
  return (
    <nav className="pagination" aria-label="分頁">
      <button
        disabled={disabled || offset === 0}
        onClick={() => onChange(Math.max(0, offset - size))}
      >
        上一頁
      </button>
      <span>第 {Math.floor(offset / size) + 1} 頁</span>
      <button disabled={disabled || !hasMore} onClick={() => onChange(offset + size)}>
        下一頁
      </button>
    </nav>
  )
}
