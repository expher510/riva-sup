<template>
  <div v-if="items && items.length" class="relative w-full overflow-hidden px-4 pt-2">
    <!-- Main Carousel Card -->
    <div
      @click="openDetails(items[currentIndex])"
      class="relative w-full aspect-[16/10] sm:aspect-[21/9] rounded-3xl overflow-hidden cursor-pointer group shadow-2xl border border-white/10 active:scale-98 transition-all"
    >
      <!-- Background Image -->
      <img
        :src="items[currentIndex].backdrop || items[currentIndex].poster"
        :alt="items[currentIndex].title"
        class="w-full h-full object-cover object-center group-hover:scale-105 transition-transform duration-700"
      />

      <!-- Dark Gradient Overlay -->
      <div class="absolute inset-0 bg-gradient-to-t from-background via-background/40 to-transparent"></div>

      <!-- Badge (top-right / top-left based on RTL) -->
      <div class="absolute top-4 left-4">
        <span
          :class="items[currentIndex].type === 'movie' ? 'bg-red-500/90 text-white' : 'bg-cyan-500/90 text-white'"
          class="px-3 py-1 rounded-xl text-xs font-bold shadow-md backdrop-blur-md"
        >
          {{ items[currentIndex].badge }}
        </span>
      </div>

      <!-- Bottom Meta -->
      <div class="absolute bottom-4 inset-x-4 text-right">
        <h2 class="text-xl sm:text-2xl font-black text-white drop-shadow-md mb-1.5 leading-tight">
          {{ items[currentIndex].title }}
        </h2>
        <div class="flex items-center gap-2 text-xs text-gray-300 font-medium">
          <span class="flex items-center gap-1 text-amber-400 font-bold">
            <span>{{ items[currentIndex].rating }}</span>
            <span>★</span>
          </span>
          <span>•</span>
          <span>الولايات المتحدة الأمريكية</span>
          <span>•</span>
          <span>{{ items[currentIndex].year }}</span>
        </div>
      </div>
    </div>

    <!-- Pagination Dots -->
    <div class="flex items-center justify-center gap-1.5 mt-3">
      <button
        v-for="(_, idx) in items"
        :key="idx"
        @click="currentIndex = idx"
        class="h-1.5 rounded-full transition-all duration-300"
        :class="currentIndex === idx ? 'w-6 bg-primary' : 'w-2 bg-gray-600/50'"
      ></button>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'

const props = defineProps({
  items: {
    type: Array,
    default: () => []
  }
})

const router = useRouter()
const currentIndex = ref(0)
let timer = null

onMounted(() => {
  timer = setInterval(() => {
    if (props.items.length) {
      currentIndex.value = (currentIndex.value + 1) % props.items.length
    }
  }, 4500)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})

const openDetails = (item) => {
  router.push(`/details/${item.type}/${item.id}`)
}
</script>

