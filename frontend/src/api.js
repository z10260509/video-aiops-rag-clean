export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message)
    this.status = status
  }
}

function detailMessage(detail) {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail))
    return detail
      .map(
        (item) => `${(item.loc || []).slice(1).join('／')}：${item.msg || item.type || '格式不符'}`,
      )
      .join('；')
  return '請稍後重試。'
}

export async function requestJson(
  url,
  { token, onUnauthorized, timeout = 15000, signal, ...options } = {},
) {
  const controller = new AbortController()
  let timedOut = false
  const abort = () => controller.abort()
  if (signal?.aborted) controller.abort()
  signal?.addEventListener('abort', abort, { once: true })
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeout)
  try {
    const headers = {
      ...(typeof options.body === 'string' ? { 'Content-Type': 'application/json' } : {}),
      ...options.headers,
    }
    if (token) headers.Authorization = `Bearer ${token}`
    const response = await fetch(url, { ...options, headers, signal: controller.signal })
    if (response.status === 401 && onUnauthorized) {
      onUnauthorized()
      throw new ApiError('登入已失效，請重新輸入操作員憑證。', 401)
    }
    const text = await response.text()
    let data = null
    try {
      data = text ? JSON.parse(text) : null
    } catch {
      throw new ApiError(
        `服務回傳無法讀取的內容（HTTP ${response.status}），請稍後重試。`,
        response.status,
      )
    }
    if (!response.ok)
      throw new ApiError(`API ${response.status}：${detailMessage(data?.detail)}`, response.status)
    return data
  } catch (error) {
    if (timedOut) throw new ApiError('連線逾時，請查閱目前結果後再操作。')
    if (error.name === 'AbortError' || error instanceof ApiError) throw error
    throw new ApiError('無法連線到服務，請檢查連線後重試。')
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
  }
}
