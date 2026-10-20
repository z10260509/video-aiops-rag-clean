export const stamp = (value) =>
  value
    ? new Date(/[zZ]|[+-]\d\d:\d\d$/.test(String(value)) ? value : value + 'Z').toLocaleString(
        'zh-TW',
        { timeZone: 'Asia/Taipei' },
      )
    : '—'
export const jobLabel = (value) =>
  ({ QUEUED: '等待處理', PROCESSING: '處理中', COMPLETED: '已完成', FAILED: '處理失敗' })[value] ||
  value
export const lifecycleLabel = (value) =>
  ({
    DETECTED: '已發現',
    INVESTIGATING: '調查中',
    DIAGNOSED: '已診斷',
    WAITING_APPROVAL: '等待人工核准',
    REMEDIATING: '修復中',
    VERIFYING: '驗證中',
    RESOLVED: '已解決',
    RESOLVED_NO_ACTION: '無需修復',
    ESCALATED: '轉交人工',
    REJECTED: '已拒絕',
    CANCELLED: '已取消',
    FAILED: '流程失敗',
  })[value] || value
export const approvalLabel = (value) =>
  ({
    draft: '草案尚未通過政策',
    pending: '等待核准',
    approved: '已核准',
    rejected: '已拒絕',
    expired: '已過期',
    superseded: '已由新診斷取代',
  })[value] || value
export const executionLabel = (value) =>
  ({
    pending: '尚未執行',
    queued: '等待執行',
    running: '執行中',
    succeeded: '修復與驗證成功',
    uncertain: '結果不確定，尚未通過復原驗證',
    denied: '執行前檢查未通過',
  })[value] || value
export const actionLabel = (value) =>
  ({
    retry_video_ffmpeg_h264: '以 H.264 重跑這支影片',
    restore_video_redis_and_retry: '還原影片服務 Redis 並重跑',
    increase_video_worker_memory_and_retry: '提高影片 worker 記憶體至 2Gi 並重跑',
    restore_lab_redis_hostname: '歷史：還原 lab Redis 位址',
    retry_lab_ffmpeg_h264: '歷史：H.264 重跑 lab 任務',
    increase_lab_worker_memory: '歷史：提高 lab worker 記憶體',
  })[value] || value
export const validationLabel = (value) =>
  ({ validated: '證據驗證通過', insufficient_evidence: '證據不足' })[value] || value
