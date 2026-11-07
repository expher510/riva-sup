<template>
  <div class="min-h-screen pb-24 text-right">
    <!-- Top Bar with Back Button -->
    <div class="flex items-center justify-between px-4 py-3 sticky top-0 z-30 bg-background/80 backdrop-blur-md">
      <button
        @click="$router.back()"
        class="w-9 h-9 rounded-xl bg-card border border-white/5 flex items-center justify-center text-gray-300 hover:text-white active:scale-90 transition"
      >
        <ArrowRight class="w-5 h-5" />
      </button>
      <span class="text-sm font-bold text-gray-300">تفاصيل العمل</span>
      <div class="w-9"></div>
    </div>

    <!-- Loading Skeleton -->
    <div v-if="loading" class="p-8 text-center text-gray-400">
      <div class="w-10 h-10 border-2 border-primary border-t-transparent rounded-full animate-spin mx-auto mb-3"></div>
      <p class="text-xs">جارٍ جلب تفاصيل العمل...</p>
    </div>

    <div v-else-if="item" class="px-4">
      <!-- Centered Poster Card (matches Image 3) -->
      <div class="w-44 sm:w-52 mx-auto aspect-[2/3] rounded-3xl overflow-hidden shadow-2xl border-2 border-white/10 my-4 bg-card">
        <img
          :src="item.poster"
          :alt="item.title"
          class="w-full h-full object-cover"
        />
      </div>

      <!-- Titles -->
      <div class="text-center mb-4">
        <h1 class="text-2xl font-black text-white mb-1 leading-snug">
          {{ item.title }}
        </h1>
        <p v-if="item.original_title" class="text-xs text-gray-400 font-medium">
          {{ item.original_title }}
        </p>
      </div>

      <!-- Meta Info Row (Country, Seasons/Duration, Year) -->
      <div class="flex items-center justify-center gap-3 text-xs text-gray-300 mb-4 flex-wrap">
        <span class="flex items-center gap-1 bg-card/60 px-2.5 py-1 rounded-lg border border-white/5">
          <span>📅</span>
          <span>{{ item.year }}</span>
        </span>
        <span class="flex items-center gap-1 bg-card/60 px-2.5 py-1 rounded-lg border border-white/5">
          <span>⏱️</span>
          <span>{{ item.type === 'series' ? `${item.seasons_count || 1} مواسم` : `${item.duration || 120} دقيقة` }}</span>
        </span>
        <span class="flex items-center gap-1 bg-card/60 px-2.5 py-1 rounded-lg border border-white/5">
          <span>🌐</span>
          <span>{{ item.country || 'الولايات المتحدة' }}</span>
        </span>
      </div>

      <!-- Classification Badge -->
      <div class="text-center mb-3">
        <span class="inline-block px-3 py-1 rounded-full text-xs font-semibold bg-indigo-500/15 text-indigo-400 border border-indigo-500/20">
          {{ item.type === 'series' ? 'مسلسل أجنبي' : 'فيلم سينمائي' }}
        </span>
      </div>

      <!-- Genres Tags -->
      <div class="flex items-center justify-center gap-2 mb-4 flex-wrap">
        <span
          v-for="g in item.genres"
          :key="g"
          class="px-3 py-1 rounded-xl text-xs bg-card border border-white/5 text-gray-300"
        >
          {{ g }}
        </span>
      </div>

      <!-- Stats Row (Views & Rating) -->
      <div class="flex items-center justify-center gap-6 py-2.5 px-4 rounded-2xl glass-panel max-w-xs mx-auto mb-5">
        <span class="flex items-center gap-1.5 text-xs text-amber-400 font-bold">
          <span>⭐</span>
          <span>{{ item.rating }}</span>
        </span>
        <span class="text-gray-600">|</span>
        <span class="flex items-center gap-1.5 text-xs text-gray-300">
          <span>👁️</span>
          <span>{{ item.views }} مشاهدة</span>
        </span>
      </div>

      <!-- Main Action Button (For Movies) -->
      <div v-if="item.type === 'movie'" class="mb-6">
        <button
          @click="startWatch(item.id, 'movie')"
          class="w-full py-4 px-6 rounded-2xl font-bold text-white bg-gradient-to-r from-indigo-600 via-primary to-accent hover:opacity-95 shadow-xl shadow-indigo-600/30 transition active:scale-98 flex items-center justify-center gap-3 text-base"
        >
          <Play class="w-5 h-5 fill-current" />
          <span>مشاهدة الآن</span>
        </button>
      </div>

      <!-- Seasons & Episodes Accordion (For Series) -->
      <div v-if="item.type === 'series' && item.seasons" class="mb-6 space-y-4">
        <!-- Season Selector Tabs -->
        <div class="flex items-center gap-2 overflow-x-auto no-scrollbar pb-1">
          <button
            v-for="(sea, idx) in item.seasons"
            :key="sea.id"
            @click="activeSeasonIndex = idx"
            class="px-4 py-2 rounded-xl text-xs font-bold transition flex-shrink-0"
            :class="activeSeasonIndex === idx ? 'bg-primary text-white shadow-md' : 'bg-card text-gray-400 hover:text-white'"
          >
            الموسم {{ sea.season_number }}
          </button>
        </div>

        <!-- Episodes List -->
        <div class="space-y-2">
          <div
            v-for="ep in currentSeasonEpisodes"
            :key="ep.id"
            class="flex items-center justify-between p-3 rounded-2xl glass-panel hover:bg-card/90 transition border border-white/5"
          >
            <div class="flex items-center gap-3">
              <span class="w-7 h-7 rounded-lg bg-surface flex items-center justify-center text-xs font-bold text-gray-300">
                {{ ep.episode_number }}
              </span>
              <span class="text-xs font-bold text-white">{{ ep.title }}</span>
            </div>

            <button
              @click="startWatch(ep.id, 'episode')"
              class="px-3.5 py-1.5 rounded-xl bg-indigo-600/20 hover:bg-indigo-600 text-indigo-300 hover:text-white font-bold text-xs transition flex items-center gap-1.5 active:scale-95"
            >
              <Play class="w-3.5 h-3.5 fill-current" />
              <span>تشغيل</span>
            </button>
          </div>
        </div>
      </div>

      <!-- Overview / Story -->
      <div class="p-4 rounded-2xl glass-panel mb-6">
        <h3 class="text-sm font-bold text-white mb-2">قصة العمل</h3>
        <p class="text-xs text-gray-300 leading-relaxed">
          {{ item.overview }}
        </p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ArrowRight, Play } from 'lucide-vue-next'
import { CatalogService } from '../services/api'
import { useUserStore } from '../stores/user'

const route = useRoute()
const userStore = useUserStore()

const item = ref(null)
const loading = ref(true)
const activeSeasonIndex = ref(0)

const currentSeasonEpisodes = computed(() => {
  if (!item.value || !item.value.seasons || !item.value.seasons[activeSeasonIndex.value]) {
    return []
  }
  return item.value.seasons[activeSeasonIndex.value].episodes || []
})

onMounted(async () => {
  const { type, id } = route.params
  try {
    item.value = await CatalogService.getDetails(type, id)
  } catch (e) {
    console.error('Failed to fetch details:', e)
  } finally {
    loading.value = false
  }
})

const startWatch = (targetId, mediaType) => {
  userStore.openWatchGate({
    id: targetId,
    type: mediaType,
    title: item.value.title
  })
}
</script>

