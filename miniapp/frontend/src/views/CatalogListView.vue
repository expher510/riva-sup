<template>
  <div class="min-h-screen pb-24 px-4 text-right">
    <!-- Top Bar -->
    <div class="flex items-center justify-between py-3 mb-2 sticky top-0 z-30 bg-background/80 backdrop-blur-md">
      <div class="w-9"></div>
      <h1 class="text-base font-extrabold text-white">
        {{ type === 'movie' ? 'جميع الأفلام السينمائية' : 'جميع المسلسلات التلفزيونية' }}
      </h1>
      <div class="w-9"></div>
    </div>

    <!-- Grid of items -->
    <div v-if="loading" class="p-8 text-center text-gray-400">
      <div class="w-8 h-8 border-2 border-primary border-t-transparent rounded-full animate-spin mx-auto mb-2"></div>
      <p class="text-xs">جارٍ التحميل...</p>
    </div>

    <div v-else class="grid grid-cols-3 gap-3">
      <MediaCard
        v-for="item in items"
        :key="type + item.id"
        :item="item"
        class="!w-full"
      />
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { CatalogService } from '../services/api'
import MediaCard from '../components/MediaCard.vue'

const props = defineProps({
  type: {
    type: String,
    default: 'movie'
  }
})

const items = ref([])
const loading = ref(true)

onMounted(async () => {
  try {
    const res = await CatalogService.getRecent()
    if (props.type === 'movie') {
      items.value = res.recent_movies || []
    } else {
      items.value = res.recent_series || []
    }
  } catch (e) {
    console.error(e)
  } finally {
    loading.value = false
  }
})
</script>

