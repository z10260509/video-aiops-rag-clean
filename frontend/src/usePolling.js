import { useEffect, useState } from 'react'

// Schedule the next batch only after settling and cancelling any remaining reads.
export function usePolling(load, { enabled = true, interval = 5000, revision = 0 } = {}) {
  const [state, setState] = useState({ loading: false, error: '', updatedAt: null })
  useEffect(() => {
    if (!enabled) return
    let stopped = false,
      timer,
      controller
    setState({ loading: true, error: '', updatedAt: null })
    async function tick() {
      const current = new AbortController()
      controller = current
      try {
        await load(current.signal)
        if (!stopped) setState({ loading: false, error: '', updatedAt: new Date() })
      } catch (error) {
        if (!stopped && error.name !== 'AbortError')
          setState((old) => ({ ...old, loading: false, error: error.message }))
      } finally {
        // Promise.all can reject before sibling reads finish; end the entire batch.
        current.abort()
        if (!stopped) timer = setTimeout(tick, interval)
      }
    }
    tick()
    return () => {
      stopped = true
      clearTimeout(timer)
      controller?.abort()
    }
  }, [load, enabled, interval, revision])
  return state
}
