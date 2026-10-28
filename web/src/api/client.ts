export type ApiRecord = Record<string, any>

let csrf = ''

export async function bootstrapSession() {
  const response = await fetch('/api/v1/session', { credentials: 'same-origin' })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.error?.message || 'Unable to establish local session')
  csrf = payload.csrf_token
  return payload
}

export async function api<T = ApiRecord>(path: string, init: RequestInit = {}): Promise<T> {
  const method = init.method || 'GET'
  const headers = new Headers(init.headers)
  if (method !== 'GET') {
    headers.set('content-type', 'application/json')
    headers.set('x-mailforge-csrf', csrf)
  }
  const response = await fetch(`/api/v1${path}`, { ...init, headers, credentials: 'same-origin' })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.error?.message || 'Mailforge request failed')
  return payload
}

export async function upload(path: string, file: File, query = '') {
  const form = new FormData()
  form.append('file', file)
  const response = await fetch(`/api/v1${path}${query}`, {
    method: 'POST', body: form, credentials: 'same-origin', headers: { 'x-mailforge-csrf': csrf },
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.error?.message || 'Import failed')
  return payload
}
