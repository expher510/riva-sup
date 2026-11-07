# 🎬 Riva Cinema - Telegram Mini App (مشروع منفصل)

تطبيق تليجرام مصغر (Telegram Mini App) مستقل ومتكامل لمشاهدة وطلب الأفلام والمسلسلات، مبني ليعمل مع البوت الأساسي لـ Riva ويعتمد على نفس قاعدة بيانات PostgreSQL (`riva_production`) دون تكرار أو مساس بمشروع ريفا القائم.

---

## 🌟 المميزات والمواصفات (مطابق للصور المرفقة)

1. **الاشتراك الإجباري بالقناة الرسمية (Force Join Gate):**
   * نافذة قفل تمنع الدخول حتى ينضم المستخدم لقناة البوت الرسمية مع زر التحقق الفوري.
2. **الصفحة الرئيسية (Home Screen):**
   * كاروسيل بانرات متحرك يعرض أحدث وأعلى الأعمال تقييماً مع بوسترات TMDB الأصلية.
   * زر مخصص لـ **صفحة الطلبات** لطلب إضافة أي فيلم أو مسلسل غير متوفر.
   * أقسام أفقية متجاوبة: **المضاف حديثاً**، **أحدث المسلسلات**، والبحث السريع.
   * شريط تنقل سفلي زجاجي عائم (Bottom Nav).
3. **صفحة تفاصيل العمل (Media Details):**
   * بوستر أنيق، العنوان بالعربية والإنجليزية، وسنة الإنتاج، والتصنيفات، وعدد المواسم/المدة.
   * مبوبة لاختيار المواسم والحلقات للمسلسلات مع زر تشغيل لكل حلقة.
4. **بوابة المشاهدة المزدوجة (Watch & Monetization Gate):**
   * **الخيار المجاني (إعلانات):** مشاهدة إعلان فيديو بمكافأة (Adsgram Rewarded Video) لتسليم الفيديو فوراً إلى شات المستخدم بجودة 1080p.
   * **خيار الـ VIP (100 نجمة تليجرام شهرياً):** اشتراك بدون إعلانات نهائياً عبر Telegram Stars بضغطة واحدة وبث فوري لا محدود.
5. **تسليم الفيديو المباشر:**
   * عند إتمام الإعلان أو للمشترك VIP، يرسل البوت الفرعي (`@Rivaa1_bot`) نسخة فورية من الفيديو من **خزينة العرض (`-1004488480136`)** إلى محادثة المستخدم الشخصية عبر `copyMessage`.

---

## 🛠️ المكدس التقني (Tech Stack)

* **الواجهة الأمامية (Frontend):** Vue 3 (Composition API) + Vite + Tailwind CSS + Lucide Icons + Pinia. (أسرع وأخف إطار عمل لتليجرام، حجم الحزمة 74KB فقط).
* **الواجهة الخلفية (Backend):** FastAPI (Python 3.11) + SQLAlchemy + Asyncpg (اتصال متزامن بقاعدة `riva_production`).
* **الدفع والإعلانات:** Telegram Stars API (XTR) + Adsgram SDK.

---

## 🚀 التشغيل والتطوير المحلي

### 1. تشغيل الـ Backend:
```bash
cd backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### 2. تشغيل الـ Frontend:
```bash
cd frontend
npm install
npm run dev
```

---

## 🌐 النشر على سيرفر VPS (Docker Compose)

```bash
cd /root/riva-miniapp
docker compose up -d --build
```

---

## 🤖 ربط التطبيق بالبوت الأساسي (@riva108_bot)

يمكن ربط الميني أب كزر دائم أسفل المحادثة في البوت عبر استدعاء Telegram Bot API:

```bash
curl -X POST "https://api.telegram.org/bot9110792514:AAENq1z9SHe8uaJcQkLDc23dKaMw0IddpsI/setChatMenuButton" \
     -H "Content-Type: application/json" \
     -d '{
       "menu_button": {
         "type": "web_app",
         "text": "🎬 افتح ريفا سينما",
         "web_app": {
           "url": "https://YOUR_DOMAIN.COM"
         }
       }
     }'
```

