<template>
  <div class="min-h-screen pb-24 px-4 text-right">
    <!-- Top Bar -->
    <div class="flex items-center justify-between py-3 mb-2 sticky top-0 z-30 bg-background/80 backdrop-blur-md">
      <button
        @click="$router.back()"
        class="w-9 h-9 rounded-xl bg-card border border-white/5 flex items-center justify-center text-gray-300 hover:text-white active:scale-90 transition"
      >
        <ArrowRight class="w-5 h-5" />
      </button>
      <span class="text-sm font-bold text-gray-300">صفحة الطلبات</span>
      <div class="w-9"></div>
    </div>

    <div class="max-w-md mx-auto pt-2">
      <!-- Info Header -->
      <div class="p-4 rounded-3xl glass-panel border border-white/10 text-center mb-6">
        <div class="w-12 h-12 rounded-2xl bg-indigo-600/20 text-indigo-400 flex items-center justify-center mx-auto mb-3">
          <MessageSquarePlus class="w-6 h-6" />
        </div>
        <h1 class="text-lg font-black text-white mb-1">اطلب فيلمك أو مسلسلك المفضل</h1>
        <p class="text-xs text-gray-400 leading-relaxed">
          اكتب اسم العمل وسنقوم بالبحث عنه ورفعه بأعلى جودة وإتاحته لك فوراً.
        </p>
      </div>

      <!-- Form -->
      <form @submit.prevent="handleSubmit" class="space-y-4">
        <!-- Media Type Selector -->
        <div class="grid grid-cols-2 gap-3">
          <button
            type="button"
            @click="form.type = 'movie'"
            class="py-3 rounded-2xl text-xs font-bold transition border"
            :class="form.type === 'movie' ? 'bg-primary text-white border-primary shadow-lg shadow-primary/20' : 'bg-card text-gray-400 border-white/5'"
          >
            🎬 فيلم سينمائي
          </button>
          <button
            type="button"
            @click="form.type = 'series'"
            class="py-3 rounded-2xl text-xs font-bold transition border"
            :class="form.type === 'series' ? 'bg-primary text-white border-primary shadow-lg shadow-primary/20' : 'bg-card text-gray-400 border-white/5'"
          >
            📺 مسلسل تلفزيوني
          </button>
        </div>

        <!-- Title Input -->
        <div>
          <label class="block text-xs font-bold text-gray-300 mb-1.5">اسم العمل باللغة العربية أو الإنجليزية *</label>
          <input
            v-model="form.title"
            required
            type="text"
            placeholder="مثال: Inception أو الحفرة"
            class="w-full py-3.5 px-4 rounded-2xl bg-card border border-white/10 text-white text-sm focus:outline-none focus:border-primary transition"
          />
        </div>

        <!-- Notes Input -->
        <div>
          <label class="block text-xs font-bold text-gray-300 mb-1.5">ملاحظات إضافية (سنة الإنتاج أو الموسم)</label>
          <textarea
            v-model="form.notes"
            rows="3"
            placeholder="مثال: موسم محدد أو سنة معينة..."
            class="w-full py-3 px-4 rounded-2xl bg-card border border-white/10 text-white text-sm focus:outline-none focus:border-primary transition resize-none"
          ></textarea>
        </div>

        <!-- Submit Button -->
        <button
          type="submit"
          :disabled="submitting"
          class="w-full py-4 rounded-2xl font-bold text-white bg-gradient-to-r from-indigo-600 to-primary hover:opacity-95 shadow-lg shadow-indigo-600/30 transition active:scale-98 disabled:opacity-50 text-sm flex items-center justify-center gap-2"
        >
          <span>{{ submitting ? 'جارٍ الإرسال...' : 'إرسال الطلب الآن' }}</span>
        </button>
      </form>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight, MessageSquarePlus } from 'lucide-vue-next'
import { RequestService } from '../services/api'
import { useUserStore } from '../stores/user'

const router = useRouter()
const userStore = useUserStore()

const form = reactive({
  title: '',
  type: 'movie',
  notes: ''
})
const submitting = ref(false)

const handleSubmit = async () => {
  if (!form.title.trim()) return
  userStore.haptic('medium')
  submitting.value = true

  try {
    const res = await RequestService.submit({
      telegram_id: userStore.telegramId,
      title: form.title,
      type: form.type,
      notes: form.notes
    })
    userStore.showNotification(res.message || 'تم إرسال طلبك بنجاح!', 'success')
    router.push('/')
  } catch (e) {
    userStore.showNotification('تعذر إرسال الطلب، حاول مرة أخرى', 'error')
  } finally {
    submitting.value = false
  }
}
</script>

