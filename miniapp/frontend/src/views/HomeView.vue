<template>
  <div class="min-h-screen pb-24 text-right">
    <!-- Top Header Bar -->
    <header class="flex items-center justify-between px-4 pt-3 pb-2 sticky top-0 z-30 bg-background/80 backdrop-blur-md">
      <div class="flex items-center gap-2">
        <!-- Theme Toggle -->
        <button
          @click="toggleTheme"
          class="w-9 h-9 rounded-xl bg-card border border-white/5 flex items-center justify-center text-gray-300 hover:text-white active:scale-90 transition"
        >
          <Sun class="w-4 h-4" />
        </button>

        <!-- Search Button -->
        <button
          @click="searchOpen = !searchOpen"
          class="w-9 h-9 rounded-xl bg-card border border-white/5 flex items-center justify-center text-gray-300 hover:text-white active:scale-90 transition"
        >
          <Search class="w-4 h-4" />
        </button>
      </div>

      <!-- App Brand Logo -->
      <div class="flex items-center gap-2">
        <span class="text-lg font-black tracking-wide text-transparent bg-clip-text bg-gradient-to-l from-indigo-400 via-primary to-accent">
          ريفا سينما
        </span>
        <div class="w-2 h-2 rounded-full bg-primary animate-pulse"></div>
      </div>
    </header>

    <!-- Search Overlay / Input -->
    <div v-if="searchOpen" class="px-4 py-2 animate-fade-in">
      <div class="relative">
        <input
          v-model="searchQuery"
          @input="handleSearch"
          type="text"
          placeholder="ابحث عن فيلم، مسلسل، أو شخصية..."
          class="w-full py-3 px-4 pr-10 rounded-2xl bg-card border border-white/10 text-white text-sm focus:outline-none focus:border-primary transition"
        />
        <Search class="absolute right-3.5 top-3.5 w-4 h-4 text-gray-400" />
      </div>

      <!-- Search Results -->
      <div v-if="searchResults.length" class="mt-3 p-2 rounded-2xl glass-panel max-h-72 overflow-y-auto space-y-1">
        <div
          v-for="res in searchResults"
          :key="res.type + res.id"
          @click="$router.push(`/details/${res.type}/${res.id}`)"
          class="flex items-center gap-3 p-2 rounded-xl hover:bg-white/5 cursor-pointer transition"
        >
          <img :src="res.poster" class="w-10 h-14 object-cover rounded-lg bg-card" />
          <div class="flex-1">
            <h4 class="text-xs font-bold text-white">{{ res.title }}</h4>
            <span class="text-[10px] text-gray-400">{{ res.year }} • {{ res.badge }}</span>
          </div>
          <span class="text-xs text-amber-400 font-bold">★ {{ res.rating }}</span>
        </div>
      </div>
    </div>

    <!-- Hero Carousel -->
    <HeroCarousel :items="featuredItems" />

    <!-- Request Button Card (صفحة الطلبات) -->
    <div class="px-4 my-4">
      <router-link
        to="/requests"
        class="w-full py-3 px-4 rounded-2xl glass-panel hover:bg-card/90 border border-white/5 flex items-center justify-center gap-2 text-sm font-bold text-gray-200 hover:text-white transition active:scale-98 shadow-sm group"
      >
        <MessageSquarePlus class="w-4 h-4 text-primary group-hover:scale-110 transition-transform" />
        <span>صفحة الطلبات</span>
      </router-link>
    </div>

    <!-- Section 1: Recently Added (المضاف حديثاً) -->
    <section class="mt-4">
      <div class="flex items-center justify-between px-4 mb-3">
        <router-link to="/movies" class="text-xs text-primary font-bold hover:underline flex items-center gap-0.5">
          <span>عرض الكل</span>
          <span>&gt;</span>
        </router-link>
        <h2 class="text-base font-extrabold text-white">المضاف حديثاً</h2>
      </div>

      <!-- Horizontal Cards Scroll -->
      <div class="flex items-center gap-3 overflow-x-auto px-4 no-scrollbar pb-2">
        <MediaCard
          v-for="item in recentMovies"
          :key="'rec-mv-' + item.id"
          :item="item"
        />
      </div>
    </section>

    <!-- Section 2: Recent Series (أحدث المسلسلات) -->
    <section class="mt-6">
      <div class="flex items-center justify-between px-4 mb-3">
        <router-link to="/series" class="text-xs text-cyan-400 font-bold hover:underline flex items-center gap-0.5">
          <span>عرض الكل</span>
          <span>&gt;</span>
        </router-link>
        <h2 class="text-base font-extrabold text-white">أحدث المسلسلات</h2>
      </div>

      <!-- Horizontal Cards Scroll -->
      <div class="flex items-center gap-3 overflow-x-auto px-4 no-scrollbar pb-2">
        <MediaCard
          v-for="item in recentSeries"
          :key="'rec-sr-' + item.id"
          :item="item"
        />
      </div>
    </section>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { Sun, Search, MessageSquarePlus } from 'lucide-vue-next'
import HeroCarousel from '../components/HeroCarousel.vue'
import MediaCard from '../components/MediaCard.vue'
import { CatalogService } from '../services/api'

const featuredItems = ref([])
const recentMovies = ref([])
const recentSeries = ref([])
const searchOpen = ref(false)
const searchQuery = ref('')
const searchResults = ref([])

onMounted(async () => {
  try {
    const feat = await CatalogService.getFeatured()
    featuredItems.value = feat.featured || []

    const rec = await CatalogService.getRecent()
    recentMovies.value = rec.recent_movies || []
    recentSeries.value = rec.recent_series || []
  } catch (e) {
    console.error('Failed to load home catalog:', e)
  }
})

let searchTimeout = null
const handleSearch = () => {
  clearTimeout(searchTimeout)
  if (searchQuery.value.trim().length < 2) {
    searchResults.value = []
    return
  }
  searchTimeout = setTimeout(async () => {
    try {
      const res = await CatalogService.search(searchQuery.value)
      searchResults.value = res.results || []
    } catch (e) {}
  }, 300)
}

const toggleTheme = () => {
  document.documentElement.classList.toggle('dark')
}
</script>

<style scoped>
@keyframes fadeIn {
  0% { opacity: 0; transform: translateY(-6px); }
  100% { opacity: 1; transform: translateY(0); }
}
.animate-fade-in {
  animation: fadeIn 0.2s ease-out forwards;
}
</style>

