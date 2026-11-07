<template>
  <div class="min-h-screen bg-background text-gray-100 flex flex-col font-sans select-none">
    <!-- Notification Toast -->
    <transition name="toast">
      <div
        v-if="userStore.notification"
        class="fixed top-4 inset-x-4 z-50 p-3.5 rounded-2xl shadow-2xl text-center text-xs font-bold text-white max-w-sm mx-auto backdrop-blur-xl border border-white/10"
        :class="userStore.notification.type === 'error' ? 'bg-red-600/90' : 'bg-emerald-600/90'"
      >
        {{ userStore.notification.msg }}
      </div>
    </transition>

    <!-- Main Content Area -->
    <main class="flex-1">
      <router-view v-slot="{ Component }">
        <transition name="fade" mode="out-in">
          <component :is="Component" />
        </transition>
      </router-view>
    </main>

    <!-- Global Modals -->
    <ForceJoinModal />
    <WatchGateModal />

    <!-- Bottom Navigation Bar -->
    <BottomNav v-if="showBottomNav" />
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useUserStore } from './stores/user'
import BottomNav from './components/BottomNav.vue'
import ForceJoinModal from './components/ForceJoinModal.vue'
import WatchGateModal from './components/WatchGateModal.vue'

const route = useRoute()
const userStore = useUserStore()

const showBottomNav = computed(() => {
  return !['details', 'requests'].includes(route.name)
})

onMounted(() => {
  userStore.initTelegramUser()
})
</script>

<style>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.18s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

.toast-enter-active,
.toast-leave-active {
  transition: all 0.25s ease-out;
}
.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(-20px);
}
</style>

