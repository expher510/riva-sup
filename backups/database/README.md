# 💾 PostgreSQL Production Database Backup & Restore Guide

هذا المجلد يحتوي على نسخة احتياطية كاملة ومقسمة من قاعدة بيانات الإنتاج (`riva_production`):
- **الملفات:** `riva_production_backup.sql.gz.part1` إلى `part5`
- **الحجم الإجمالي:** ~8.7 ميجابايت مضغوطة (~120 ميجابايت غير مضغوطة).
- **المحتوى:**
  - جميع الأفلام (`movies`) والمسلسلات (`series`) والمواسم (`seasons`) والحلقات (`episodes`).
  - جميع الأرشيفات المرفوعة في خزائن تليجرام (`telegram_archives`).
  - جميع روابط السيرفرات النشطة (`streaming_links`).
  - جداول تطبيق تليجرام المصغر الجديدة (`miniapp_users`, `miniapp_subscriptions`, `miniapp_requests`).
  - جداول قسم الـ +18 ونجوم تليجرام (`adult18_access`, `adult18_movies`).

---

## 🚀 كيفية التجميع والاستعادة على أي سيرفر جديد بأمر واحد

### 1. عبر سيرفر لينكس / VPS:
```bash
cd backups/database
chmod +x combine_db.sh
./combine_db.sh
```
أو يدوياً:
```bash
cat riva_production_backup.sql.gz.part* > riva_production_backup.sql.gz
gunzip -c riva_production_backup.sql.gz | docker exec -i riva_postgres psql -U riva_admin -d riva_production
```

### 2. على ويندوز (CMD / PowerShell):
```cmd
cd backups\database
combine_db.bat
```
