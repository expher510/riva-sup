<template>
  <div v-if="!userStore.isChannelMember" class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md">
    <div class="w-full max-w-sm p-6 text-center rounded-3xl glass-panel shadow-2xl border border-white/10 animate-scale-up">
      <!-- Lock Icon -->
      <div class="flex items-center justify-center w-16 h-16 mx-auto mb-4 rounded-2xl bg-indigo-600/20 text-indigo-400 ring-1 ring-indigo-500/30">
        <Lock class="w-8 h-8" />
      </div>

      <!-- Title & Text -->
      <h2 class="text-xl font-bold text-white mb-2">عذراً، يجب الانضمام للقناة</h2>
      <p class="text-sm text-gray-300 leading-relaxed mb-6">
        للاستمرار في استخدام التطبيق ومشاهدة الأفلام والمسلسلات، يرجى الانضمام لقناتنا الرسمية أولاً.
      </p>

      <!-- Action Buttons -->
      <div class="space-y-3">
        <button
          @click="joinChannel"
          class="w-full py-3.5 px-4 rounded-xl font-bold text-white bg-gradient-to-r from-indigo-600 to-primary hover:from-indigo-500 hover:to-indigo-600 shadow-lg shadow-indigo-600/30 transition-all flex items-center justify-center gap-2 active:scale-95"
        >
          <span class="text-lg">+</span>
          <span>انضم الآن</span>
        </button>

        <button
          @click="verifyMembership"
          :disabled="userStore.checkingChannel"
          class="w-full py-3 px-4 rounded-xl font-semibold text-gray-200 bg-surface/80 hover:bg-surface border border-white/10 transition-all flex items-center justify-center gap-2 active:scale-95 disabled:opacity-50"
        >
          <RotateCw class="w-4 h-4" :class="{ 'animate-spin': userStore.checkingChannel }" />
          <span>{{ userStore.checkingChannel ? 'جارٍ التحقق...' : 'تحقق' }}</span>
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { Lock, RotateCw } from 'lucide-vue-next'
import { useUserStore } from '../stores/user'

const userStore = useUserStore()

const joinChannel = () => {
  userStore.haptic('medium')
  const tg = window.Telegram?.WebApp
  if (tg?.openTelegramLink) {
    tg.openTelegramLink(userStore.channelInviteUrl)
  } else {
    window.open(userStore.channelInviteUrl, '_blank')
  }
}

const verifyMembership = () => {
  userStore.haptic('light')
  userStore.checkChannelMembership()
}
</script>

<style scoped>
@keyframes scaleUp {
  0% { transform: scale(0.92); opacity: 0; }
  100% { transform: scale(1); opacity: 1; }
}
.animate-scale-up {
  animation: scaleUp 0.25s cubic-bezier(0.16, 1, 0.3, 1) forwards;
}
</style>

