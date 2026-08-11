import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// Self-hosted IBM Plex (latin subset only) -- no CDN, no runtime network
// request. Plex Sans carries the prose and UI; Plex Mono carries every
// readout, legend, id, and SQL block in the console.
import '@fontsource/ibm-plex-sans/latin-400.css'
import '@fontsource/ibm-plex-sans/latin-500.css'
import '@fontsource/ibm-plex-sans/latin-600.css'
import '@fontsource/ibm-plex-mono/latin-400.css'
import '@fontsource/ibm-plex-mono/latin-500.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
