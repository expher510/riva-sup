<template>
  <nav class="fixed bottom-0 inset-x-0 z-40 pb-safe pt-2 px-4 glass-panel border-t border-white/10 bg-background/90 backdrop-blur-xl">
    <div class="flex items-center justify-around max-w-lg mx-auto py-1">
      <router-link
        v-for="tab in tabs"
        :key="tab.path"
        :to="tab.path"
        class="flex flex-col items-center gap-1 transition-all duration-200 active:scale-90"
        :class="$route.path === tab.path ? 'text-primary font-bold' : 'text-gray-400 hover:text-gray-200 font-medium'"
      >
        <component :is="tab.icon" class="w-5 h-5" />
        <span class="text-[11px]">{{ tab.label }}</span>
      </router-link>
    </div>
  </nav>
</template>

<script setup>
import { Home, Film, Tv, User } from 'lucide-vue-next'

const tabs = [
  { label: 'الرئيسية', path: '/', icon: Home },
  { label: 'أفلام', path: '/movies', icon: Film },
  { label: 'مسلسلات', path: '/series', icon: Tv },
  { label: 'حسابي', path: '/profile', icon: User }
]
</script>

<style scoped>
.pb-safe {
  padding-bottom: max(0.5rem, env(safe-area-inset-bottom));
}
</style>

