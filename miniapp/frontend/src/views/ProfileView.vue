<template>
  <div class="min-h-screen pb-24 px-4 text-right">
    <!-- Top Bar -->
    <div class="flex items-center justify-between py-3 mb-4 sticky top-0 z-30 bg-background/80 backdrop-blur-md">
      <div class="w-9"></div>
      <span class="text-sm font-bold text-gray-300">حسابي</span>
      <div class="w-9"></div>
    </div>

    <div class="max-w-md mx-auto space-y-4">
      <!-- Profile Card -->
      <div class="p-6 rounded-3xl glass-panel border border-white/10 text-center relative overflow-hidden">
        <!-- Glow Blob -->
        <div class="absolute -top-12 -left-12 w-32 h-32 bg-primary/20 rounded-full blur-2xl pointer-events-none"></div>

        <!-- Avatar -->
        <div class="w-20 h-20 rounded-full bg-gradient-to-tr from-indigo-600 to-accent mx-auto mb-3 flex items-center justify-center text-2xl font-black text-white shadow-xl ring-4 ring-white/10">
          {{ userStore.firstName ? userStore.firstName[0] : 'U' }}
        </div>

        <h1 class="text-lg font-black text-white mb-0.5">{{ userStore.firstName }}</h1>
        <p v-if="userStore.username" class="text-xs text-gray-400 mb-3">@{{ userStore.username }}</p>

        <!-- VIP Badge Pill -->
        <div class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold"
          :class="userStore.isVip ? 'bg-amber-500/20 text-amber-400 border border-amber-500/30' : 'bg-gray-800 text-gray-300 border border-white/5'"
        >
          <span>{{ userStore.isVip ? '👑 عضو VIP (بدون إعلانات)' : 'عضو مجاني' }}</span>
        </div>

        <p v-if="userStore.isVip && userStore.vipExpiresAt" class="text-[11px] text-amber-300/80 mt-2">
          ينتهي الاشتراك في: {{ new Date(userStore.vipExpiresAt).toLocaleDateString('ar-EG') }}
        </p>
      </div>

      <!-- Stats Grid -->
      <div class="grid grid-cols-2 gap-3">
        <div class="p-4 rounded-2xl glass-panel text-center border border-white/5">
          <span class="text-2xl font-black text-primary mb-1 block">{{ userStore.totalAdsWatched }}</span>
          <span class="text-xs text-gray-400 font-semibold">إعلانات تمت مشاهدتها</span>
        </div>
        <div class="p-4 rounded-2xl glass-panel text-center border border-white/5">
          <span class="text-2xl font-black text-amber-400 mb-1 block">{{ userStore.isVip ? 'نشط' : '100⭐' }}</span>
          <span class="text-xs text-gray-400 font-semibold">حالة اشتراك VIP</span>
        </div>
      </div>

      <!-- VIP Upgrade Banner if not VIP -->
      <div v-if="!userStore.isVip" class="p-5 rounded-3xl bg-gradient-to-br from-indigo-950/80 via-card to-card border border-indigo-500/30 shadow-xl">
        <div class="flex items-center gap-3 mb-2">
          <div class="w-10 h-10 rounded-xl bg-amber-500/20 text-amber-400 flex items-center justify-center">
            <Crown class="w-6 h-6" />
          </div>
          <div>
            <h3 class="text-sm font-bold text-white">ترقية لحساب VIP بدون إعلانات</h3>
            <p class="text-[11px] text-gray-400">100 نجمة فقط شهرياً لمشاهدة فورية</p>
          </div>
        </div>

        <button
          @click="subscribeStars"
          class="w-full mt-3 py-3 rounded-xl font-bold text-slate-950 bg-amber-400 hover:bg-amber-300 transition text-xs shadow active:scale-98 flex items-center justify-center gap-1.5"
        >
          <span>⭐</span>
          <span>اشترك الآن بـ 100 نجمة تليجرام</span>
        </button>
      </div>

      <!-- Quick Actions List -->
      <div class="rounded-3xl glass-panel border border-white/5 divide-y divide-white/5 overflow-hidden">
        <a
          :href="userStore.channelInviteUrl"
          target="_blank"
          class="flex items-center justify-between p-4 hover:bg-white/5 transition"
        >
          <div class="flex items-center gap-3 text-xs font-bold text-gray-200">
            <Send class="w-4 h-4 text-primary" />
            <span>قناتنا الرسمية على تليجرام</span>
          </div>
          <span class="text-gray-500">&lt;</span>
        </a>

        <router-link
          to="/requests"
          class="flex items-center justify-between p-4 hover:bg-white/5 transition"
        >
          <div class="flex items-center gap-3 text-xs font-bold text-gray-200">
            <MessageSquarePlus class="w-4 h-4 text-cyan-400" />
            <span>صفحة طلبات الأفلام والمسلسلات</span>
          </div>
          <span class="text-gray-500">&lt;</span>
        </router-link>
      </div>
    </div>
  </div>
</template>

<script setup>
import { Crown, Send, MessageSquarePlus } from 'lucide-vue-next'
import { useUserStore } from '../stores/user'
import { PaymentService } from '../services/api'

const userStore = useUserStore()

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
          userStore.showNotification('👑 تم تفعيل اشتراك VIP بنجاح!', 'success')
        }
      })
    } else {
      window.open(data.invoice_link, '_blank')
    }
  } catch (e) {
    userStore.showNotification('تعذر إنشاء الفاتورة حالياً', 'error')
  }
}
</script>

