import axios from 'axios'

const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000/api/v1'

export const api = axios.create({
  baseURL: API_BASE,
  timeout: 15000,
  headers: {
    'Content-Type': 'application/json'
  }
})

export const CatalogService = {
  getFeatured: () => api.get('/catalog/featured').then(r => r.data),
  getRecent: () => api.get('/catalog/recent').then(r => r.data),
  search: (query) => api.get(`/catalog/search?q=${encodeURIComponent(query)}`).then(r => r.data),
  getDetails: (type, id) => api.get(`/catalog/details/${type}/${id}`).then(r => r.data)
}

export const UserService = {
  auth: (user) => api.post('/user/auth', user).then(r => r.data),
  verifyChannel: (telegram_id) => api.post('/user/verify-channel', { telegram_id }).then(r => r.data)
}

export const WatchService = {
  requestPlay: (payload) => api.post('/watch/request-play', payload).then(r => r.data),
  adCompleted: (payload) => api.post('/watch/ad-completed', payload).then(r => r.data)
}

export const PaymentService = {
  createStarsInvoice: (telegram_id) => api.post('/payment/create-stars-invoice', { telegram_id }).then(r => r.data),
  confirmPayment: (payload) => api.post('/payment/confirm-payment', payload).then(r => r.data)
}

export const RequestService = {
  submit: (payload) => api.post('/requests/submit', payload).then(r => r.data)
}

