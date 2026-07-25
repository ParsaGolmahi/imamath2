# 📊 راهنمای تنظیم PostgreSQL

## مرحله 1: تنظیم متغیرهای محیط

1. یک کپی از `.env.example` بسازید و نام آن را `.env` کنید:
```bash
copy .env.example .env
```

2. فایل `.env` را باز کنید و اطلاعات PostgreSQL خود را وارد کنید:
```
DB_USER=postgres
DB_PASSWORD=your_password_here
DB_HOST=localhost
DB_PORT=5432
DB_NAME=ima_db
```

## مرحله 2: بررسی نصب PostgreSQL

PostgreSQL 18 را بررسی کنید:
```powershell
psql --version
```

## مرحله 3: نصب کتابخانه‌های پایتون

```bash
pip install -r requirements.txt
```

## مرحله 4: اجرای سکریپت راه‌اندازی دیتابیس

```bash
python setup_db.py
```

این سکریپت:
- ✅ دیتابیس `ima_db` را ایجاد می‌کند
- ✅ تمام جداول لازم را می‌سازد
- ✅ پیام‌های راهنما نمایش می‌دهد

## جداول ایجاد‌شده

### 1. **users** - جدول کاربران
```sql
- id (Primary Key)
- username (Unique)
- email (Unique)
- password_hash
- full_name
- role (STUDENT, TEACHER, ADMIN)
- is_active
- created_at, updated_at
```

### 2. **user_sessions** - جلسات کاربری
```sql
- id
- user_id (Foreign Key)
- token
- ip_address
- user_agent
- expires_at
- is_active
```

### 3. **user_progress** - پیشرفت کاربر
```sql
- id
- user_id
- course_name (mathlab, mathplanet, etc.)
- lessons_completed
- total_lessons
- progress_percentage
- last_accessed
```

### 4. **ai_conversations** - گفتگوهای AI
```sql
- id
- user_id (nullable)
- session_id
- title
- model
- created_at, updated_at
```

### 5. **ai_messages** - پیام‌های AI
```sql
- id
- conversation_id
- role (user/assistant)
- content
- tokens_used
- created_at
```

### 6. **audit_logs** - ثبت فعالیت‌ها
```sql
- id
- user_id
- action
- details
- ip_address
- created_at
```

## استفاده در FastAPI

```python
from database import SessionLocal, get_db
from fastapi import Depends

@app.post("/api/register")
def register(username: str, email: str, password: str, db: Session = Depends(get_db)):
    # اینجا کوده‌ای برای ذخیره کاربر
    from models import User
    user = User(username=username, email=email, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    return {"message": "کاربر با موفقیت ثبت شد"}
```

## دستورات مفید PostgreSQL

```sql
-- متصل شدن به دیتابیس
psql -U postgres -d ima_db

-- مشاهده تمام جداول
\dt

-- مشاهده ساختار جدول
\d users

-- حذف دیتابیس
DROP DATABASE ima_db;
```

## رفع مشکلات

### خطای: "could not translate host name"
**حل:** بررسی کنید که PostgreSQL روی `localhost` اجرا می‌شود:
```bash
psql -U postgres
```

### خطای: "password authentication failed"
**حل:** رمز عبور خود را در `.env` بررسی کنید

### خطای: "database ima_db already exists"
**حل:** دیتابیس قبلاً ایجاد شده است. این خطا قابل نادیدگیری است.

---

**نکته:** فایل `.env` را در `.gitignore` قرار دهید تا رمز عبور نشر نشود!
