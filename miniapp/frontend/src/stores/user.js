import { defineStore } from 'pinia'
import { UserService, WatchService, PaymentService } from '../services/api'

export const useUserStore = defineStore('user', {
  state: () => ({
    telegramId: null,
    firstName: 'مستخدم',
    username: null,
    isVip: false,
    vipExpiresAt: null,
    totalAdsWatched: 0,
    isChannelMember: false,
    checkingChannel: false,
    channelInviteUrl: 'https://t.me/riva18_official',
    theme: 'dark',
    // Watch Gate Modal State
    watchGateOpen: false,
    targetMedia: null, // { id, type, title }
    loadingAction: false,
    notification: null
  }),

  actions: {
    initTelegramUser() {
      const tg = window.Telegram?.WebApp
      if (tg) {
        tg.ready()
        tg.expand()
        // Set header color
        try {
          tg.setHeaderColor('#0b0f19')
          tg.setBackgroundColor('#0b0f19')
        } catch (e) {}

        const user = tg.initDataUnsafe?.user
        if (user) {
          this.telegramId = user.id
          this.firstName = user.first_name || 'مستخدم'
          this.username = user.username
        }
      }

      // Fallback ID for local browser testing
      if (!this.telegramId) {
        this.telegramId = 999999999
        this.firstName = 'مستخدم تجريبي'
      }

      this.syncUser()
      this.checkChannelMembership()
    },

    haptic(type = 'light') {
      try {
        const tg = window.Telegram?.WebApp
        if (tg?.HapticFeedback) {
          if (type === 'light' || type === 'medium' || type === 'heavy') {
            tg.HapticFeedback.impactOccurred(type)
          } else if (type === 'success' || type === 'error' || type === 'warning') {
            tg.HapticFeedback.notificationOccurred(type)
          }
        }
      } catch (e) {}
    },

    async syncUser() {
      try {
        const data = await UserService.auth({
          telegram_id: this.telegramId,
          first_name: this.firstName,
          username: this.username
        })
        this.isVip = data.is_vip
        this.vipExpiresAt = data.vip_expires_at
        this.totalAdsWatched = data.total_ads_watched
      } catch (e) {
        console.error('Failed to sync user:', e)
      }
    },

    async checkChannelMembership() {
      this.checkingChannel = true
      try {
        const res = await UserService.verifyChannel(this.telegramId)
        this.isChannelMember = res.is_member
        if (res.channel_url) {
          this.channelInviteUrl = res.channel_url
        }
      } catch (e) {
        console.error('Channel check failed:', e)
        this.isChannelMember = true // do not hard-block on network error
      } finally {
        this.checkingChannel = false
      }
    },

    openWatchGate(media) {
      this.haptic('medium')
      this.targetMedia = media
      this.watchGateOpen = true
    },

    closeWatchGate() {
      this.watchGateOpen = false
      this.targetMedia = null
    },

    showNotification(msg, type = 'success') {
      this.notification = { msg, type }
      setTimeout(() => {
        this.notification = null
      }, 4000)
    }
  }
})

