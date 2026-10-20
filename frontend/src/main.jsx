import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import './style.css'
import AIOps from './AIOps'
import VideoPlatform from './components/VideoPlatform'

function Platform() {
  const [section, setSection] = useState('video')
  const [videoJobId, setVideoJobId] = useState(null)
  const [token, setToken] = useState('')
  const openJob = (id) => {
    setVideoJobId(id)
    setSection('aiops')
  }
  return (
    <>
      <nav className="platform-nav" aria-label="平台導覽">
        <button
          aria-current={section === 'video' ? 'page' : undefined}
          onClick={() => setSection('video')}
        >
          影片平台
        </button>
        <button
          aria-current={section === 'aiops' ? 'page' : undefined}
          onClick={() => {
            setVideoJobId(null)
            setSection('aiops')
          }}
        >
          AIOps 智慧診斷
        </button>
      </nav>
      {section === 'video' ? (
        <VideoPlatform onDiagnose={openJob} operatorToken={token} />
      ) : (
        <AIOps
          token={token}
          setToken={setToken}
          videoJobId={videoJobId}
          onBack={() => setSection('video')}
        />
      )}
    </>
  )
}
createRoot(document.getElementById('root')).render(<Platform />)
