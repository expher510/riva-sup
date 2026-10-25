# خطة الباك إند الشاملة ومعمارية منصة Riva VOD Engine

وثيقة التصميم الهندسي وخطة العمل لبناء محرك بث وتوزيع فيديو متكامل، يتفوق على المواقع التقليدية (مثل Egybest) في حجم المكتبة وسرعة التحديث وجودة المشاهدة، مع تأمين كامل ضد حظر الـ IP وفقدان الملفات.

---

## 1. الرؤية الهندسية ومخطط المعمارية الشامل (System Architecture)

يعتمد النظام على **3 ركائز رئيسية**:
1. **مصادر متعددة قابلة للتبديل (Scrapers Pool)** لتجنب الاعتماد على موقع واحد.
2. **تخزين هجين ذكي**: تليجرام كخزينة دائمة لا تُحذف + DoodStream/Streamtape لجني الأرباح.
3. **مشغل متعدد الخيارات**: بث مباشر نقي (HLS) بدون إعلانات + سيرفرات بديلة.

```mermaid
flowchart TD
    subgraph Sources["1. مجمّع المصادر المتعددة (Multi-Provider Scrapers)"]
        C4U["Cima4u (كوري/أنمي/أجنبي)"]
        AKW["Akwam (عربي/أجنبي)"]
        WEC["WeCima / MyCima (شامل)"]
        WIT["WitAnime / Okanime (أنمي حصري)"]
    end

    subgraph Protection["طبقة الأمان وتفادي الحظر"]
        Proxy["Proxy Rotator / Safe Human Delays"]
        Checkpoints[("Scraper Checkpoints DB\n(استئناف تلقائي من آخر صفحة)")]
    end

    subgraph RivaCore["2. قلب المحرك والباك إند (Riva Core API & Worker)"]
        FastAPI["FastAPI 0.115 Core Engine"]
        Celery["Celery Workers (Concurrency = 4)"]
        Redis[("Redis 7 (Metadata & Rate Limits)")]
        DB[("PostgreSQL 16 / SQLite\n(Series, Episodes, Checkpoints)")]
        Transit["Scratch Disk (/tmp/riva_transit)"]
    end

    subgraph StorageLayer["3. استراتيجية التخزين الهجين (Dual-Storage)"]
        subgraph Vault["الخزينة الدائمة (Cold Storage)"]
            TG["قنوات تليجرام خاصة (Private Telegram Vault)\n- مساحة غير محدودة مجاناً\n- لا تحذف أبداً (Permanent Backup)"]
        end
        subgraph Monetization["منظومة الأرباح (Hot Streaming)"]
            Dood["DoodStream (Pay-Per-View)"]
            Tape["Streamtape (Pay-Per-View)"]
        end
        subgraph DirectStream["البث النقي السريع"]
            HLS["Direct HLS Streams (master.m3u8)\n- مشغل Riva بدون إعلانات نهائياً"]
        end
    end

    subgraph AutoHeal["نظام التعافي التلقائي (Auto-Healing Engine)"]
        Healer["Celery Beat Periodic Checker:\nإذا مات رابط في DoodStream، يسحبه فوراً من تليجرام ويعيد رفعه في 10 ثوانٍ!"]
    end

    Sources --> Protection
    Protection --> RivaCore
    RivaCore --> Transit
    Transit --> TG
    Transit --> Dood
    Transit --> Tape
    Transit --> HLS
    TG -.-> Healer
    Healer -.-> Dood
```

---

## 2. هيكل البيانات الموحد (Unified Database Schema)

النظام مصمم لتوحيد جميع المصادر في نموذج مركزي نظيف:

```mermaid
erDiagram
    Series ||--o{ Season : has
    Season ||--o{ Episode : contains
    Episode ||--o{ StreamingLink : delivers
    Movie ||--o{ StreamingLink : delivers
    Series ||--o| ScraperCheckpoint : tracks
    Episode ||--o| TelegramArchive : backed_up_in

    Series {
        int id PK
        string title
        string category "kdrama / anime / turkish / western / arabic"
        int year
        string poster_path
        string status "ongoing / completed"
        int tmdb_id
    }

    Season {
        int id PK
        int series_id FK
        int season_number
        string title
    }

    Episode {
        int id PK
        int season_id FK
        int episode_number
        string title
        string source_url "Unique Checkpoint Key"
        bool is_filler
        string status "active / processing"
    }

    StreamingLink {
        int id PK
        int movie_id FK "Nullable"
        int episode_id FK "Nullable"
        string provider_name "doodstream / streamtape / direct-hls"
        string embed_url
        string direct_url
        string quality "1080p / 720p"
        string file_code
        string status "active / dead"
    }

    TelegramArchive {
        int id PK
        int episode_id FK
        string telegram_file_id
        int message_id
        string channel_id
        int file_size_bytes
    }
```

---

## 3. خطة التعامل مع حظر الـ IP (Anti-Ban & Stealth Scraping)

لمنع تكرار حظر الـ IP الذي حدث أثناء التجارب:
1. **الـ Safe Random Delays**:
   * وضع فاصل زمني عشوائي بين كل طلب وآخر (`random.uniform(2.0, 4.5)` ثانية) مع رأس متصفح كامل (`User-Agent`, `Sec-Ch-Ua`, `Accept-Language`).
2. **نظام استئناف العمل (Checkpoints Resumption Engine)**:
   * حفظ رقم الصفحة الحالية وعدد الحلقات المسحوبة في جدول `scraper_checkpoints`.
   * في حال توقف السحب أو حدث حظر مؤقت، ينام الاسكرابر ويستأنف فوراً من نفس الصفحة والمسلسل دون تكرار أي محتوى تم إنجازه.
3. **دعم البروكسي الدوار (Rotating Proxy)**:
   * إضافة متغير `SCRAPER_PROXY_URL` في إعدادات الباك إند، لتمرير الطلبات عبر شبكات بروكسي دوارة في حال سحب آلاف الصفحات دفعة واحدة.

---

## 4. خطة التخزين في تليجرام (Telegram Master Vault)

### لماذا تليجرام هو صمام الأمان الحقيقي للمشروع؟
* **المشكلة في مواقع الرفع (DoodStream / Streamtape)**: تحذف الملفات بعد 30 إلى 60 يوماً إذا لم تحصل على مشاهدات مستمرة، بالإضافة لبلاغات حقوق الملكية.
* **الحل عبر تليجرام**:
  1. إنشاء **قناة تليجرام خاصة (Private Channel)** مغلقة لا يدخلها أي شخص.
  2. يقوم بوت الباك إند برفع نسخة الفيديو الأصلية إلى القناة وتخزين الـ `telegram_file_id`.
  3. **النتيجة**: مكتبة سحابية كاملة مجانية للأبد لا يمكن لأحد حذفها أو الوصول إليها.
  4. **محرك التعافي التلقائي (Self-Healing)**: إذا تعطل أو حُذف رابط حلقة من DoodStream أو Streamtape، يسحب السيرفر الحلقة فوراً من تليجرام في ثوانٍ ويعيد رفعها برابط جديد دون أن يشعر الزائر بأي عطل.

---

## 5. خطة مراحل التنفيذ (Implementation Roadmap)

| المرحلة | المهام المنجزة والمخططة | الحالة |
| :--- | :--- | :--- |
| **المرحلة 1: المعمارية الأساسية** | - تصميم جداول المسلسلات والمواسم والحلقات.<br>- بناء اسكرابر Cima4u الذكي مع فك تشفير HLS.<br>- إثبات الرفع الحقيقي إلى DoodStream. | **تمت بنجاح ✅** |
| **المرحلة 2: حماية الأمان والمصادر المتعددة** | - دمج فواصل الأمان (Safe Human Delays) لمنع حظر الـ IP.<br>- بناء اسكرابر بديل (Akwam / WeCima) داخل `worker/scrapers/`.<br>- نظام التبديل الآلي بين المواقع عند تعطل أحدها. | **جاهزة للتنفيذ 🚀** |
| **المرحلة 3: تكامل خزينة تليجرام** | - إنشاء وحدة `worker/telegram_vault.py` للرفع الآلي للقناة الخاصة.<br>- ربط نظام التعافي التلقائي (Auto-Healer) لإعادة توليد الروابط الميتة. | **مجدولة للتنفيذ ⏳** |
| **المرحلة 4: النشر على سيرفر Hostinger VPS** | - تشغيل الحاويات عبر Docker Compose على سيرفر هوستنجر.<br>- تشغيل العمال (Celery Workers) لجمع المحتوى بسرعة 1Gbps في الخلفية 24/7. | **جاهزة عند توفر السيرفر 🌐** |

---

## 6. أوامر التشغيل والاستخدام

```bash
# 1. تهيئة جداول قاعدة البيانات
python init_db.py

# 2. تشغيل سحب المسلسلات الكورية والآسيوية (استئناف تلقائي من آخر صفحة)
python -m worker.scrapers.cima4u_crawler asian-series 5

# 3. تشغيل سحب مسلسلات الأنمي
python -m worker.scrapers.cima4u_crawler anime-series 5

# 4. تشغيل سحب كافة الأقسام تباعاً
python -m worker.scrapers.cima4u_crawler --all 5

# 5. تشغيل سيرفر الـ API لمتابعة البيانات
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
