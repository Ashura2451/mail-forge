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

export async function uploadWithFields(path: string, file: File, fields: Record<string, string>) {
  const form = new FormData()
  form.append('file', file)
  Object.entries(fields).forEach(([key, value]) => form.append(key, value))
  const response = await fetch(`/api/v1${path}`, {
    method: 'POST', body: form, credentials: 'same-origin', headers: { 'x-mailforge-csrf': csrf },
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.error?.message || 'Data Studio request failed')
  return payload
}

export async function uploadMany(path: string, files: File[], fields: Record<string, string> = {}) {
  const form = new FormData()
  files.forEach(file => form.append('files', file))
  Object.entries(fields).forEach(([key, value]) => form.append(key, value))
  const response = await fetch(`/api/v1${path}`, { method: 'POST', body: form, credentials: 'same-origin', headers: { 'x-mailforge-csrf': csrf } })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.error?.message || 'Data Studio request failed')
  return payload
}

export async function uploadFiles(path: string, files: Record<string, File>, fields: Record<string, string> = {}) {
  const form = new FormData()
  Object.entries(files).forEach(([key, file]) => form.append(key, file))
  Object.entries(fields).forEach(([key, value]) => form.append(key, value))
  const response = await fetch(`/api/v1${path}`, { method: 'POST', body: form, credentials: 'same-origin', headers: { 'x-mailforge-csrf': csrf } })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.error?.message || 'Import failed')
  return payload
}

export async function uploadAsset(file: File, kind: 'attachment' | 'image' = 'attachment') {
  return uploadFiles('/message-studio/assets', { file }, { kind })
}

export async function exportSortSelection(jobId: string, providerKeys: string[], selectionMode: 'all' | 'selected' | 'none') {
  const response = await fetch(`/api/v1/list-tools/jobs/${jobId}/export-selected`, {
    method: 'POST', body: JSON.stringify({ provider_keys: providerKeys, selection_mode: selectionMode }), credentials: 'same-origin',
    headers: { 'content-type': 'application/json', 'x-mailforge-csrf': csrf },
  })
  if (!response.ok) {
    const payload = await response.json()
    throw new Error(payload.error?.message || 'Sort export failed')
  }
  return response.blob()
}

export async function uploadComparison(main: File | null, references: File[], fields: Record<string, string> = {}) {
  const form = new FormData()
  if (main) form.append('main', main)
  references.forEach(file => form.append('references', file))
  Object.entries(fields).forEach(([key, value]) => form.append(key, value))
  const response = await fetch('/api/v1/list-comparison/preview', {
    method: 'POST', body: form, credentials: 'same-origin', headers: { 'x-mailforge-csrf': csrf },
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.error?.message || 'Comparison failed')
  return payload
}

export async function exportComparison(result: ApiRecord) {
  const response = await fetch('/api/v1/list-comparison/export', {
    method: 'POST', body: JSON.stringify(result), credentials: 'same-origin',
    headers: { 'content-type': 'application/json', 'x-mailforge-csrf': csrf },
  })
  if (!response.ok) {
    const payload = await response.json()
    throw new Error(payload.error?.message || 'Export failed')
  }
  return response.blob()
}
