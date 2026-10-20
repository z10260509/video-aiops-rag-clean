import { useCallback, useEffect, useRef, useState } from 'react'
import { requestJson } from '../api'
import { usePolling } from '../usePolling'
import { jobLabel } from '../format'
import { Pagination } from './Common'

const api = import.meta.env.VITE_API_BASE_URL || '/video/api'
const PAGE_SIZE = 20
const bytes = (value) =>
  value == null
    ? '—'
    : value < 1024 * 1024
      ? `${Math.round(value / 1024)} KB`
      : `${(value / (1024 * 1024)).toFixed(1)} MB`
const saving = (job) =>
  !job.input_size_bytes || job.output_size_bytes == null
    ? '—'
    : `${Math.abs(Math.round((1 - job.output_size_bytes / job.input_size_bytes) * 100))}% ${job.output_size_bytes > job.input_size_bytes ? '增加' : '縮小'}`

export default function VideoPlatform({ onDiagnose, operatorToken }) {
  const [file, setFile] = useState(null)
  const [resolution, setResolution] = useState('720p')
  const [codec, setCodec] = useState('h264')
  const [compression, setCompression] = useState('balanced')
  const [demoFailure, setDemoFailure] = useState(false)
  const [jobs, setJobs] = useState([])
  const [offset, setOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [revision, setRevision] = useState(0)
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const fileInput = useRef(null)
  const mutation = useRef(null)
  useEffect(() => () => mutation.current?.abort(), [])

  const load = useCallback(
    async (signal) => {
      const rows = await requestJson(`${api}/jobs?limit=${PAGE_SIZE + 1}&offset=${offset}`, {
        signal,
      })
      if (!Array.isArray(rows)) throw Error('影片列表格式不符，請稍後重試。')
      if (signal.aborted) return
      if (!rows.length && offset > 0) {
        setOffset(Math.max(0, offset - PAGE_SIZE))
        return
      }
      setJobs(rows.slice(0, PAGE_SIZE))
      setHasMore(rows.length > PAGE_SIZE)
    },
    [offset],
  )
  const poll = usePolling(load, { interval: 3000, enabled: !busy && !deleting, revision })

  async function submit(event) {
    event.preventDefault()
    if (!file || !file.name.toLowerCase().endsWith('.mp4')) {
      setMessage('請選擇 MP4 影片。')
      return
    }
    if (demoFailure && !operatorToken) {
      setMessage('故障展示需要先登入 AIOps。')
      return
    }
    const controller = new AbortController()
    mutation.current = controller
    setBusy(true)
    setMessage('')
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('operation', 'transcode')
      form.append('target_resolution', resolution)
      form.append('video_codec', codec)
      form.append('compression_preset', compression)
      if (demoFailure) form.append('demonstrate_failure', 'true')
      const job = await requestJson(`${api}/jobs`, {
        method: 'POST',
        body: form,
        token: demoFailure ? operatorToken : undefined,
        signal: controller.signal,
        timeout: 120000,
      })
      if (!job?.id) throw Error('無法確認上傳結果，請先查看影片列表。')
      setMessage('影片已提交，將在背景處理。')
      setFile(null)
      if (fileInput.current) fileInput.current.value = ''
      setOffset(0)
      setRevision((value) => value + 1)
      if (demoFailure) onDiagnose(job.id)
    } catch (error) {
      if (error.name !== 'AbortError')
        setMessage(`${error.message} 若已送出上傳，請先確認列表，避免重複提交。`)
    } finally {
      if (!controller.signal.aborted) setBusy(false)
      mutation.current = null
    }
  }

  async function removeJobs(targets) {
    if (!targets.length) return
    const label =
      targets.length === 1
        ? `影片「${targets[0].original_filename}」`
        : `此頁 ${targets.length} 支已結束影片`
    if (!window.confirm(`刪除${label}及其輸入、輸出和縮圖？此操作會刪除影片檔案。`)) return
    const controller = new AbortController()
    mutation.current = controller
    setDeleting(true)
    setMessage('')
    const deleted = new Set()
    let errorMessage = ''
    try {
      for (const job of targets) {
        try {
          await requestJson(`${api}/jobs/${job.id}`, {
            method: 'DELETE',
            signal: controller.signal,
          })
          deleted.add(job.id)
        } catch (error) {
          if (error.name === 'AbortError') throw error
          errorMessage = error.message
        }
      }
    } catch (error) {
      errorMessage = error.message
    } finally {
      if (!controller.signal.aborted) {
        setJobs((old) => old.filter((job) => !deleted.has(job.id)))
        setMessage(
          `已刪除 ${deleted.size} 支影片${targets.length > deleted.size ? `；${targets.length - deleted.size} 支未完成刪除。${errorMessage}` : '。'}`,
        )
        setDeleting(false)
        setRevision((value) => value + 1)
      }
      mutation.current = null
    }
  }

  const finished = jobs.filter((job) => ['COMPLETED', 'FAILED'].includes(job.status))
  return (
    <main>
      <h1>影片處理平台</h1>
      <p>上傳 MP4 後由背景 worker 轉檔；完成後可播放與下載。</p>
      <form className="upload-form" onSubmit={submit}>
        <label>
          MP4 影片
          <input
            ref={fileInput}
            type="file"
            accept="video/mp4,.mp4"
            disabled={busy}
            onChange={(event) => setFile(event.target.files[0] || null)}
          />
        </label>
        <label>
          解析度
          <select
            value={resolution}
            disabled={busy}
            onChange={(event) => setResolution(event.target.value)}
          >
            <option value="720p">720p</option>
            <option value="480p">480p</option>
          </select>
        </label>
        <label>
          影片編碼
          <select value={codec} disabled={busy} onChange={(event) => setCodec(event.target.value)}>
            <option value="h264">H.264（相容性較佳）</option>
            <option value="h265">H.265／HEVC（檔案較小）</option>
          </select>
        </label>
        <label>
          壓縮品質
          <select
            value={compression}
            disabled={busy}
            onChange={(event) => setCompression(event.target.value)}
          >
            <option value="high">高品質</option>
            <option value="balanced">平衡</option>
            <option value="small">節省空間</option>
          </select>
        </label>
        <label className="demo-option">
          <input
            type="checkbox"
            checked={demoFailure}
            disabled={!operatorToken || busy}
            onChange={(event) => setDemoFailure(event.target.checked)}
          />
          展示 FFmpeg 故障與人工修復
        </label>
        {!operatorToken && <small>先到 AIOps 登入即可啟用故障展示；一般上傳不受影響。</small>}
        <button disabled={busy || deleting}>{busy ? '上傳中…' : '上傳並處理'}</button>
      </form>
      {message && <p role="status">{message}</p>}
      {poll.error && <p role="alert">{poll.error}</p>}
      <div className="section-heading">
        <h2>影片列表</h2>
        <button
          className="danger"
          disabled={!finished.length || deleting || busy}
          onClick={() => removeJobs(finished)}
        >
          {deleting ? '刪除中…' : '刪除此頁已結束影片'}
        </button>
      </div>
      <p>每頁最多 20 支影片；自動更新不會重疊請求。{poll.loading ? '正在載入…' : ''}</p>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>預覽</th>
              <th>檔名</th>
              <th>輸出設定</th>
              <th>大小</th>
              <th>狀態</th>
              <th>進度</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => (
              <tr key={job.id}>
                <td>
                  {job.thumbnail_available ? (
                    <img
                      loading="lazy"
                      className="thumbnail"
                      src={`${api}/jobs/${job.id}/thumbnail`}
                      alt={`${job.original_filename} 縮圖`}
                    />
                  ) : (
                    '—'
                  )}
                </td>
                <td>
                  {job.original_filename}
                  <small>{job.target_resolution}</small>
                </td>
                <td>
                  {job.video_codec?.toUpperCase()}
                  <small>
                    {{ high: '高品質', balanced: '平衡', small: '節省空間' }[
                      job.compression_preset
                    ] || job.compression_preset}
                  </small>
                </td>
                <td>
                  {bytes(job.input_size_bytes)} → {bytes(job.output_size_bytes)}
                  <small>{saving(job)}</small>
                </td>
                <td>
                  <span className={`status ${job.status}`}>{jobLabel(job.status)}</span>
                  {job.error_message && <small>{job.error_message}</small>}
                </td>
                <td>{job.progress}%</td>
                <td>
                  <div className="actions">
                    <button onClick={() => onDiagnose(job.id)}>診斷與修復</button>
                    {job.status === 'COMPLETED' && (
                      <>
                        <a href={`${api}/jobs/${job.id}/video`} target="_blank" rel="noreferrer">
                          播放
                        </a>
                        <a href={`${api}/jobs/${job.id}/download`}>下載</a>
                      </>
                    )}
                    {['COMPLETED', 'FAILED'].includes(job.status) && (
                      <button
                        className="danger"
                        disabled={deleting || busy}
                        onClick={() => removeJobs([job])}
                      >
                        刪除影片
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!jobs.length && !poll.loading && <p>尚無影片。</p>}
      <Pagination
        offset={offset}
        hasMore={hasMore}
        onChange={setOffset}
        disabled={deleting || busy}
      />
    </main>
  )
}
