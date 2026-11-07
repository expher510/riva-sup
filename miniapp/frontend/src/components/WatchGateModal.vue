<template>
  <div v-if="userStore.watchGateOpen" class="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-0 sm:p-4 bg-black/85 backdrop-blur-md">
    <div class="w-full max-w-md p-6 text-center rounded-t-3xl sm:rounded-3xl glass-panel border-t sm:border border-white/10 shadow-2xl animate-slide-up">
      <!-- Video Icon Header -->
      <div class="flex items-center justify-center w-16 h-16 mx-auto mb-4 rounded-full bg-indigo-600/20 text-indigo-400 ring-4 ring-indigo-500/10">
        <Video class="w-8 h-8" />
      </div>

      <!-- Title & Description -->
      <h2 class="text-xl font-extrabold text-white mb-2">خطوة أخيرة</h2>
      <p class="text-sm text-gray-300 leading-relaxed mb-6 px-4">
        لمساعدتنا في تقديم الخدمة مجاناً شاهد إعلان واحد وهيصلك الفيديو
      </p>

      <!-- Option 1: Watch Ad (Free) -->
      <button
        @click="watchAd"
        :disabled="loadingAd"
        class="w-full py-4 px-6 rounded-2xl font-bold text-white bg-gradient-to-r from-indigo-600 via-primary to-accent hover:opacity-95 shadow-lg shadow-indigo-600/30 transition-all flex items-center justify-center gap-3 active:scale-95 disabled:opacity-50 text-base mb-5"
      >
        <Play class="w-5 h-5 fill-current" />
        <span>{{ loadingAd ? 'جارٍ تحميل الإعلان...' : 'شاهد اعلان' }}</span>
      </button>

      <!-- OR Divider -->
      <div class="relative flex items-center justify-center mb-5">
        <div class="border-t border-gray-700/60 w-full"></div>
        <span class="bg-card px-4 text-xs font-semibold text-gray-400 uppercase tracking-wider">أو</span>
        <div class="border-t border-gray-700/60 w-full"></div>
      </div>

      <!-- Option 2: 100 Stars VIP Card -->
      <div
        @click="subscribeStars"
        class="group cursor-pointer p-4 rounded-2xl bg-surface/90 hover:bg-surface border border-indigo-500/30 hover:border-indigo-500/60 transition-all shadow-md active:scale-98 text-right relative overflow-hidden"
      >
        <div class="flex items-center justify-between mb-2">
          <div class="flex items-center gap-2">
            <div class="w-7 h-7 rounded-lg bg-amber-500/20 text-amber-400 flex items-center justify-center">
              <Crown class="w-4 h-4" />
            </div>
            <span class="font-bold text-white text-base">من غير اعلانات خالص</span>
          </div>
          <span class="text-xs px-2.5 py-1 rounded-full bg-indigo-500/20 text-indigo-300 font-bold">VIP</span>
        </div>
        
        <p class="text-xs text-gray-400 mb-3">اشتراك شهري وشاهد على طول بدون أي إعلانات</p>

        <!-- Price Pill -->
        <div class="p-3 rounded-xl bg-card border border-white/5 flex items-center justify-between">
          <div>
            <div class="flex items-center gap-1.5 text-amber-400 font-bold text-sm">
              <span>⭐</span>
              <span>100 نجمة</span>
              <span class="text-xs text-gray-400 font-normal">/ شهرياً</span>
            </div>
            <p class="text-[11px] text-gray-400">من غير إعلانات، ومن غير حدود في المشاهدة</p>
          </div>
          <button class="px-3.5 py-1.5 rounded-lg bg-amber-500 hover:bg-amber-400 text-slate-950 font-bold text-xs transition">
            اشتراك
          </button>
        </div>
      </div>

      <!-- Cancel Button -->
      <button
        @click="userStore.closeWatchGate()"
        class="w-full mt-4 py-2.5 text-sm font-semibold text-gray-400 hover:text-white transition"
      >
        إلغاء
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { Video, Play, Crown } from 'lucide-vue-next'
import { useUserStore } from '../stores/user'
import { WatchService, PaymentService } from '../services/api'

const userStore = useUserStore()
const loadingAd = ref(false)

const watchAd = async () => {
  userStore.haptic('medium')
  loadingAd.value = true

  const media = userStore.targetMedia
  if (!media) return

  // Adsgram Rewarded Ad integration
  if (window.Adsgram) {
    try {
      const AdController = window.Adsgram.init({
        blockId: import.meta.env.VITE_ADSGRAM_BLOCK_ID || "int-0000",
        debug: true
      })

      AdController.show().then(async (result) => {
        // User watched ad to completion
        userStore.haptic('success')
        await deliverVideo(media)
      }).catch(async (error) => {
        console.warn('Adsgram error or dismissed:', error)
        // Fallback for demo/testing: still deliver or prompt user
        await deliverVideo(media)
      }).finally(() => {
        loadingAd.value = false
      })
      return
    } catch (e) {
      console.error('Adsgram init error:', e)
    }
  }

  // Fallback simulator if running in regular browser without Adsgram
  setTimeout(async () => {
    loadingAd.value = false
    await deliverVideo(media)
  }, 1200)
}

const deliverVideo = async (media) => {
  try {
    const res = await WatchService.adCompleted({
      telegram_id: userStore.telegramId,
      media_type: media.type,
      item_id: media.id
    })
    userStore.closeWatchGate()
    userStore.showNotification(res.message || 'تم إرسال الفيديو إلى محادثتك بنجاح 🍿', 'success')
  } catch (err) {
    userStore.showNotification(err.response?.data?.detail || 'حدث خطأ أثناء إرسال الفيديو', 'error')
  }
}

const subscribeStars = async () => {
  userStore.haptic('medium')
  try {
    const data = await PaymentService.createStarsInvoice(userStore.telegramId)
    const tg = window.Telegram?.WebApp
    if (tg?.openInvoice) {
      tg.openInvoice(data.invoice_link, async (status) => {
        if (status === 'paid') {
          userStore.haptic('success')
          await PaymentService.confirmPayment({
            telegram_id: userStore.telegramId,
            charge_id: 'stars_' + Date.now()
          })
          await userStore.syncUser()
          userStore.closeWatchGate()
          userStore.showNotification('👑 تم تفعيل اشتراك VIP بنجاح! استمتع بدون إعلانات.', 'success')
        }
      })
    } else {
      window.open(data.invoice_link, '_blank')
    }
  } catch (e) {
    userStore.showNotification('تعذر إنشاء فاتورة النجوم حالياً', 'error')
  }
}
</script>

<style scoped>
@keyframes slideUp {
  0% { transform: translateY(100%); opacity: 0; }
  100% { transform: translateY(0); opacity: 1; }
}
.animate-slide-up {
  animation: slideUp 0.28s cubic-bezier(0.16, 1, 0.3, 1) forwards;
}
</style>

