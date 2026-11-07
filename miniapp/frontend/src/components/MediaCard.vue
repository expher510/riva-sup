<template>
  <div
    @click="openDetails"
    class="flex-shrink-0 w-32 sm:w-36 cursor-pointer group active:scale-95 transition-all text-right"
  >
    <!-- Poster Container -->
    <div class="relative w-full aspect-[2/3] rounded-2xl overflow-hidden bg-card border border-white/5 shadow-md mb-2">
      <img
        :src="item.poster || 'https://via.placeholder.com/300x450/1e293b/94a3b8?text=No+Poster'"
        :alt="item.title"
        loading="lazy"
        class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500"
      />

      <!-- Type Badge -->
      <span
        :class="item.badge_color === 'red' || item.type === 'movie' ? 'bg-red-500/90' : 'bg-cyan-500/90'"
        class="absolute top-2 left-2 px-2 py-0.5 rounded-lg text-[10px] font-bold text-white shadow backdrop-blur-sm"
      >
        {{ item.badge || (item.type === 'movie' ? 'فيلم' : 'مسلسل') }}
      </span>

      <!-- Rating Pill -->
      <div v-if="item.rating" class="absolute bottom-2 right-2 px-1.5 py-0.5 rounded-md bg-black/70 backdrop-blur-md text-[10px] text-amber-400 font-bold flex items-center gap-0.5">
        <span>{{ item.rating }}</span>
        <span>★</span>
      </div>
    </div>

    <!-- Title & Year -->
    <h3 class="text-xs font-bold text-white truncate group-hover:text-primary transition-colors">
      {{ item.title }}
    </h3>
    <p class="text-[11px] text-gray-400 mt-0.5">
      {{ item.year || '' }}
    </p>
  </div>
</template>

<script setup>
import { useRouter } from 'vue-router'

const props = defineProps({
  item: {
    type: Object,
    required: true
  }
})

const router = useRouter()

const openDetails = () => {
  router.push(`/details/${props.item.type}/${props.item.id}`)
}
</script>

