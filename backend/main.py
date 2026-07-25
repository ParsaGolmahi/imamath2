import os
import json
import hashlib
import secrets
import base64
import uuid
import threading
import asyncio
import re 
from typing import List, Optional, AsyncGenerator
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from openai import OpenAI, AsyncOpenAI

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import jwt

# ─── دیتابیس ───
from database import (
    SessionLocal, get_db, User, UserSession, UserProgress, AIConversation, AIMessage,
    Question, Quiz, StudentAnswer, Grade, QuestionDifficulty
)
from sqlalchemy.orm import Session

# ─── تنظیمات اولیه ───
load_dotenv()

app = FastAPI(title="IMA Backend", version="2.0.1")

# ─── CORS ───
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
# Clean up origins
ALLOWED_ORIGINS = [origin.strip() for origin in ALLOWED_ORIGINS if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
    max_age=3600,
)

# ─── امنیت ───
SECRET_KEY = os.getenv("SECRET_KEY", "ima-very-secret-key-change-in-production-2024")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

security = HTTPBearer(auto_error=False)

# ─── کلاینت‌های OpenAI ───
client = OpenAI(
    api_key=os.getenv("GAPGPT_API_KEY"),
    base_url="https://api.gapgpt.app/v1",
    timeout=30.0,
    max_retries=2
)

async_client = AsyncOpenAI(
    api_key=os.getenv("GAPGPT_API_KEY"),
    base_url="https://api.gapgpt.app/v1",
    timeout=30.0,
    max_retries=2
)

AI_MODEL = "gapgpt-qwen-3.6"
AI_MODEL_FAST = "gapgpt-qwen-3.6"
TTS_MODEL = "tts-1"

# ═══════════════════════════════════════════
# 🛡️ توابع امنیتی - باید قبل از دیتابیس تعریف بشن
# ═══════════════════════════════════════════

def hash_password(password: str) -> str:
    """هش کردن رمز عبور با SHA256 + salt"""
    salt = secrets.token_hex(16)
    hashed = hashlib.sha256((password + salt).encode()).hexdigest()
    return f"{salt}${hashed}"

def verify_password(password: str, hashed: str) -> bool:
    """بررسی رمز عبور"""
    try:
        salt, original_hash = hashed.split("$")
        new_hash = hashlib.sha256((password + salt).encode()).hexdigest()
        return new_hash == original_hash
    except:
        return False

# ═══════════════════════════════════════════
# 📦 سیستم کش
# ═══════════════════════════════════════════

class ResponseCache:
    """کش ساده برای پاسخ‌های AI"""
    def __init__(self, max_size=100):
        self.cache = {}
        self.max_size = max_size
        self.lock = threading.Lock()
    
    def get_cache_key(self, messages: list, temperature: float) -> str:
        """ساخت کلید کش بر اساس محتوای پیام"""
        content = json.dumps(messages, sort_keys=True) + str(temperature)
        return hashlib.md5(content.encode()).hexdigest()
    
    def get(self, key: str) -> Optional[dict]:
        with self.lock:
            return self.cache.get(key)
    
    def set(self, key: str, value: dict):
        with self.lock:
            if len(self.cache) >= self.max_size:
                oldest_key = next(iter(self.cache))
                del self.cache[oldest_key]
            self.cache[key] = {
                "data": value,
                "timestamp": datetime.now(timezone.utc)
            }

chat_cache = ResponseCache(max_size=50)

# ═══════════════════════════════════════════
# 🗄️ دیتابیس در حافظه - حالا می‌تونیم از hash_password استفاده کنیم
# ═══════════════════════════════════════════

db_lock = threading.Lock()

fake_db = {
    "users": [
        {
            "id": "1",
            "username": "admin",
            "email": "admin@ima.com",
            "full_name": "مدیر سیستم",
            "hashed_password": hash_password("admin123"),  # ✅ حالا کار میکنه
            "avatar": None,
            "role": "admin",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_login": None,
            "chat_count": 0
        }
    ]
}

# ═══════════════════════════════════════════
# 📋 مدل‌های Pydantic
# ═══════════════════════════════════════════

class UserRegister(BaseModel):
    username: str
    email: str
    full_name: str
    password: str

class UserLogin(BaseModel):
    username: str
    password: str

class UserResponse(BaseModel):
    id: str
    username: str
    email: str
    full_name: str
    avatar: Optional[str] = None
    role: str
    created_at: str
    chat_count: int

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    temperature: float = 0.7
    max_tokens: int = 800
    stream: bool = False

# ─── مدل‌های معلم ───
class QuestionCreate(BaseModel):
    title: str
    description: Optional[str] = None
    content: str
    category: str
    difficulty: str = "medium"
    correct_answer: str
    options: Optional[str] = None  # JSON array
    explanation: Optional[str] = None

class QuestionResponse(BaseModel):
    id: int
    teacher_id: int
    title: str
    description: Optional[str]
    content: str
    category: str
    difficulty: str
    created_at: datetime
    
    class Config:
        from_attributes = True

class QuizCreate(BaseModel):
    title: str
    description: Optional[str] = None
    category: str
    question_ids: List[int]
    time_limit: Optional[int] = None

class QuizResponse(BaseModel):
    id: int
    teacher_id: int
    title: str
    description: Optional[str]
    category: str
    time_limit: Optional[int]
    is_active: bool
    created_at: datetime
    
    class Config:
        from_attributes = True

class StudentAnswerCreate(BaseModel):
    question_id: int
    user_answer: str
    is_correct: bool
    score: float = 0

class GradeResponse(BaseModel):
    id: int
    student_id: int
    quiz_id: int
    score: float
    max_score: float
    percentage: float
    submitted_at: datetime
    
    class Config:
        from_attributes = True

# ═══════════════════════════════════════════
# 🔧 توابع کمکی
# ═══════════════════════════════════════════

def create_access_token(data: dict) -> str:
    """ساخت توکن JWT"""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    """رمزگشایی توکن JWT"""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="توکن منقضی شده")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="توکن نامعتبر")

def get_user_by_username(username: str) -> Optional[dict]:
    with db_lock:
        for user in fake_db["users"]:
            if user["username"] == username:
                return user
    return None

def get_user_by_email(email: str) -> Optional[dict]:
    with db_lock:
        for user in fake_db["users"]:
            if user["email"] == email:
                return user
    return None

def user_to_response(user: dict) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "full_name": user["full_name"],
        "avatar": user.get("avatar"),
        "role": user.get("role", "user"),
        "created_at": user["created_at"],
        "chat_count": user.get("chat_count", 0)
    }

async def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security), db: Session = Depends(get_db)):
    """احراز هویت اجباری"""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="لطفاً وارد حساب کاربری خود شوید",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    payload = decode_token(credentials.credentials)
    username = payload.get("sub")
    
    if not username:
        raise HTTPException(status_code=401, detail="توکن نامعتبر")
    
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=401, detail="کاربر پیدا نشد")
    
    return user

# ═══════════════════════════════════════════
# 🌐 APIهای عمومی
# ═══════════════════════════════════════════

@app.get("/")
async def root(db: Session = Depends(get_db)):
    users_count = db.query(User).count()
    return {
        "message": "IMA Backend is running ✅",
        "version": "2.0.1",
        "model": AI_MODEL,
        "users_count": users_count
    }

@app.get("/api/ping")
async def ping():
    """بررسی سرعت پاسخ سرور"""
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/api/health")
async def health_check(db: Session = Depends(get_db)):
    """بررسی سلامت کامل سیستم"""
    try:
        start = datetime.now()
        test_response = client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "user", "content": "test"}],
            max_tokens=10
        )
        latency = (datetime.now() - start).total_seconds()
        users_count = db.query(User).count()
        
        return {
            "status": "healthy",
            "ai_model": AI_MODEL,
            "ai_latency": f"{latency:.2f}s",
            "users_count": users_count,
            "cache_size": len(chat_cache.cache)
        }
    except Exception as e:
        return {
            "status": "degraded",
            "error": str(e)
        }

# ═══════════════════════════════════════════
# 🔐 APIهای احراز هویت
# ═══════════════════════════════════════════

# ۱. آپدیت مدل Pydantic برای دریافت نقش از فرانت‌‌اند
class UserRegister(BaseModel):
    username: str
    email: str
    full_name: str
    password: str
    role: Optional[str] = "student"  # مقدار پیش‌فرض دانش‌آموز است

# ۲. اصلاح اِندپوینت ثبت‌نام برای اعمال نقش در دیتابیس
@app.post("/api/auth/register", response_model=TokenResponse)
async def register(data: UserRegister, db: Session = Depends(get_db)):
    """ثبت نام کاربر جدید با نقش مشخص شده (معلم یا دانش‌آموز)"""
    
    if len(data.username) < 3:
        raise HTTPException(status_code=400, detail="نام کاربری باید حداقل ۳ کاراکتر باشد")
    if len(data.password) < 6:
        raise HTTPException(status_code=400, detail="رمز عبور باید حداقل ۶ کاراکتر باشد")
    if "@" not in data.email:
        raise HTTPException(status_code=400, detail="ایمیل نامعتبر است")
    
    # بررسی تکراری نبودن
    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(status_code=400, detail="این نام کاربری قبلاً ثبت شده")
    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(status_code=400, detail="این ایمیل قبلاً ثبت شده")

    # معتبرسازی نقش ورودی
    target_role = data.role if data.role in ["student", "teacher", "admin"] else "student"

    # ایجاد کاربر جدید با نقش انتخابی
    new_user = User(
        username=data.username,
        email=data.email,
        full_name=data.full_name,
        password_hash=hash_password(data.password),
        role=target_role # 👈 نقش اینجا به دیتابیس پاس داده می‌شود
    )
    
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    token = create_access_token({"sub": new_user.username})
    
    # اصلاح فرمت پاسخ خروجی (استفاده از رشته مستقیم به جای Enum.value برای فرانت‌اند)
    user_role_str = new_user.role if isinstance(new_user.role, str) else new_user.role.value

    user_dict = {
        "id": str(new_user.id),
        "username": new_user.username,
        "email": new_user.email,
        "full_name": new_user.full_name,
        "avatar": None,
        "role": user_role_str,
        "created_at": new_user.created_at.isoformat(),
        "chat_count": 0
    }
    
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": UserResponse(**user_dict)
    }

@app.post("/api/auth/login", response_model=TokenResponse)
async def login(data: UserLogin, db: Session = Depends(get_db)):
    """ورود کاربر به سیستم"""
    
    user = db.query(User).filter(User.username == data.username).first()
    
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="نام کاربری یا رمز عبور اشتباه است")

    token = create_access_token({"sub": user.username})
    
    print(f"✅ کاربر وارد شد: {user.username}")
    
    # داخل متد login بخش user_dict را به این صورت اصلاح کن:
    user_role_str = user.role if isinstance(user.role, str) else user.role.value

    user_dict = {
        "id": str(user.id),
        "username": user.username,
        "email": user.email,
        "full_name": user.full_name,
        "avatar": None,
        "role": user_role_str, # گرفتن مقدار رشته‌ای نقش برای جلوگیری از اختلال در فرانت‌افت
        "created_at": user.created_at.isoformat(),
        "chat_count": 0
    }
    
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": UserResponse(**user_dict)
    }

@app.get("/api/auth/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """اطلاعات کاربر فعلی"""
    return UserResponse(
        id=str(current_user.id),
        username=current_user.username,
        email=current_user.email,
        full_name=current_user.full_name,
        avatar=None,
        role=current_user.role,
        created_at=current_user.created_at.isoformat(),
        chat_count=0
    )

# ═══════════════════════════════════════════
# 💬 APIهای چت
# ═══════════════════════════════════════════

@app.post("/api/chat")
async def chat(request: ChatRequest, current_user: User = Depends(get_current_user)):
    """چت با معلم ریاضی"""
    try:
        messages = []
        for m in request.messages:
            content = m.content
            if len(content) > 2000:
                content = content[:2000] + "..."
            messages.append({"role": m.role, "content": content})

        if not any(m["role"] == "system" for m in messages):
            messages.insert(0, {
                "role": "system",
                "content": "تو ایما هستی، معلم ریاضی. پاسخ‌های کوتاه و مفید بده. مستقیم برو سر اصل مطلب."
            })

        # بررسی کش
        cache_key = chat_cache.get_cache_key(messages, request.temperature)
        cached_response = chat_cache.get(cache_key)
        
        if cached_response:
            print(f"⚡ پاسخ از کش برای {current_user.username}")
            return cached_response["data"]

        print(f"📝 {current_user.username} چت میکنه...")
        
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            temperature=min(request.temperature, 0.5),
            max_tokens=min(request.max_tokens, 800),
            presence_penalty=0,
            frequency_penalty=0,
        )

        result = response.model_dump()
        
        # ذخیره در کش
        chat_cache.set(cache_key, result)
        
        return result

    except Exception as e:
        print(f"❌ Chat Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest, current_user: User = Depends(get_current_user)):
    """چت استریم - نمایش لحظه‌ای پاسخ"""
    try:
        messages = [{"role": m.role, "content": m.content} for m in request.messages]

        if not any(m["role"] == "system" for m in messages):
            messages.insert(0, {
                "role": "system",
                "content": "تو ایما هستی، معلم ریاضی. پاسخ‌های مختصر و مفید بده."
            })

        async def generate():
            try:
                stream = client.chat.completions.create(
                    model=AI_MODEL,
                    messages=messages,
                    temperature=0.5,
                    max_tokens=600,
                    stream=True
                )
                
                for chunk in stream:
                    if chunk.choices[0].delta.content:
                        content = chunk.choices[0].delta.content
                        yield f"data: {json.dumps({'content': content})}\n\n"
                
                yield "data: [DONE]\n\n"
                
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/chat/fast")
async def chat_fast(question: str = Form(...), current_user: User = Depends(get_current_user)):
    """چت سریع - فقط یک سوال و پاسخ کوتاه"""
    try:
        if not question.strip():
            raise HTTPException(status_code=400, detail="سوال نمی‌تواند خالی باشد")
        
        question = question[:500]
        
        messages = [
            {"role": "system", "content": "تو ایما هستی. فقط پاسخ کوتاه و مستقیم بده، بدون توضیح اضافه."},
            {"role": "user", "content": question}
        ]

        # بررسی کش
        cache_key = chat_cache.get_cache_key(messages, 0.3)
        cached = chat_cache.get(cache_key)
        if cached:
            return cached["data"]

        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            temperature=0.3,
            max_tokens=300,
            presence_penalty=0,
            frequency_penalty=0,
        )

        result = response.model_dump()
        chat_cache.set(cache_key, result)

        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/chat/vision")
async def chat_vision(
    question: str = Form(""),
    file: Optional[UploadFile] = File(None),
    current_user: User = Depends(get_current_user)
):
    """چت با عکس"""
    try:
        messages = [{
            "role": "system",
            "content": "تو ایما هستی. سوال ریاضی را از تصویر بخوان و پاسخ کوتاه بده."
        }]

        user_content = []

        if file:
            if not file.content_type or not file.content_type.startswith("image/"):
                raise HTTPException(status_code=400, detail="فقط فایل‌های تصویری مجاز هستند")
            
            contents = await file.read()
            
            if len(contents) > 10 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="حجم تصویر باید کمتر از 10 مگابایت باشد")
            
            b64 = base64.b64encode(contents).decode('utf-8')
            mime = file.content_type or "image/jpeg"
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"}
            })

        text = question or "لطفاً این سوال ریاضی را حل کن و پاسخ کوتاه بده."
        user_content.append({"type": "text", "text": text[:500]})
        messages.append({"role": "user", "content": user_content})

        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            max_tokens=600,
            temperature=0.5,
        )
        
        return response.model_dump()

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════════
# 🎵 APIهای صوت
# ═══════════════════════════════════════════

@app.post("/api/tts")
async def tts(
    text: str = Form(""),
    voice: str = Form("alloy"),
    speed: float = Form(1.0),
    current_user: User = Depends(get_current_user)
):
    """متن به صوت"""
    try:
        if not text.strip():
            raise HTTPException(status_code=400, detail="متن نمی‌تواند خالی باشد")
        
        if len(text) > 1000:
            text = text[:1000] + "..."
        
        fixed = text
        for old, new in [("^2", " به توان دو "), ("^3", " به توان سه "), ("π", " پی ")]:
            fixed = fixed.replace(old, new)

        response = client.audio.speech.create(
            model=TTS_MODEL,
            input=fixed,
            voice=voice,
            speed=float(speed),
            response_format="mp3"
        )

        return Response(
            content=response.content,
            media_type="audio/mpeg",
            headers={
                "Cache-Control": "public, max-age=3600",
            }
        )

    except Exception as e:
        print(f"❌ TTS Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/stt")
async def stt(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    """صوت به متن"""
    try:
        allowed_types = ["audio/webm", "audio/mp3", "audio/wav", "audio/mpeg", "audio/ogg"]
        if file.content_type not in allowed_types:
            raise HTTPException(status_code=400, detail="فرمت فایل صوتی پشتیبانی نمی‌شود")
        
        audio_bytes = await file.read()
        temp_path = f"temp_{uuid.uuid4()}.webm"

        try:
            with open(temp_path, "wb") as f:
                f.write(audio_bytes)

            with open(temp_path, "rb") as f:
                result = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=(temp_path, f, file.content_type or "audio/webm"),
                    language="fa"
                )

            return {"text": result.text}
        
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    except Exception as e:
        print(f"❌ STT Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════════
# 🏆 مسابقه
# ═══════════════════════════════════════════

@app.post("/api/quiz/generate")
async def quiz(
    topic: str = Form(...),
    level: str = Form("medium"),
    count: int = Form(20),
    current_user: User = Depends(get_current_user)
):
    """ساخت سوالات"""
    if count < 1 or count > 50:
        raise HTTPException(status_code=400, detail="تعداد سوالات باید بین 1 تا 50 باشد")
    
    if level not in ["easy", "medium", "hard"]:
        raise HTTPException(status_code=400, detail="سطح نامعتبر است")
    
    levels = {"easy": "آسان", "medium": "متوسط", "hard": "سخت"}
    level_text = levels.get(level, "متوسط")

    prompt = f"""{count} سوال چهارگزینه‌ای ریاضی درباره "{topic}" سطح {level_text}.
فقط JSON خالص برگردون، بدون هیچ توضیح اضافه:
[{{"question":"...","options":["الف","ب","ج","د"],"correct":0,"explanation":"توضیح مختصر"}}]"""

    try:
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": "تو یک تولیدکننده سوالات ریاضی هستی. فقط JSON خالص و معتبر برگردون."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )

        text = response.choices[0].message.content
        text = text.replace("```json", "").replace("```", "").strip()
        
        try:
            questions = json.loads(text)
        except json.JSONDecodeError as e:
            print(f"❌ JSON Parse Error: {str(e)}")
            print(f"🔍 Raw response: {text[:200]}...")
            raise HTTPException(status_code=500, detail="خطا در پردازش پاسخ مدل")
        
        if not isinstance(questions, list):
            raise HTTPException(status_code=500, detail="فرمت پاسخ نامعتبر است")
        
        for q in questions:
            if not all(k in q for k in ["question", "options", "correct"]):
                raise HTTPException(status_code=500, detail="ساختار سوالات نامعتبر است")
        
        return {"questions": questions}

    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="خطا در تولید سوالات. لطفاً دوباره تلاش کنید.")
    except Exception as e:
        print(f"❌ Quiz Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    
# پس از سایر endpointها، قبل از if __name__ ...
@app.post("/api/support/chat")
async def support_chat(request: ChatRequest):
    """چت پشتیبانی بدون نیاز به لاگین - برای ویجت سایت"""
    try:
        messages = []
        for m in request.messages:
            content = m.content
            if len(content) > 2000:
                content = content[:2000] + "..."
            messages.append({"role": m.role, "content": content})

        if not any(m["role"] == "system" for m in messages):
            messages.insert(0, {
                "role": "system",
                "content": "تو ایما هستی، دستیار پشتیبانی و معلم ریاضی. پاسخ‌های کوتاه، مفید و با حوصله بده. اگر سوال خارج از ریاضیات مدرسه باشد، مودبانه راهنمایی کن که در حیطه تخصصی من نیست."
            })

        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            temperature=0.5,
            max_tokens=800,
            presence_penalty=0,
            frequency_penalty=0,
        )

        return response.model_dump()

    except Exception as e:
        print(f"❌ Support Chat Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════════
# 📚 پنل معلم - سوالات و آزمون‌ها
# ═══════════════════════════════════════════

@app.post("/api/teacher/questions/create")
async def create_question(
    data: QuestionCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """ایجاد سوال جدید توسط معلم"""
    # بررسی اینکه کاربر معلم است
    if current_user.role.value != "teacher":
        raise HTTPException(status_code=403, detail="فقط معلم‌ها می‌توانند سوال ایجاد کنند")
    
    question = Question(
        teacher_id=current_user.id,
        title=data.title,
        description=data.description,
        content=data.content,
        category=data.category,
        difficulty=data.difficulty,
        correct_answer=data.correct_answer,
        options=data.options,
        explanation=data.explanation
    )
    
    db.add(question)
    db.commit()
    db.refresh(question)
    
    return {
        "id": question.id,
        "title": question.title,
        "category": question.category,
        "difficulty": question.difficulty,
        "created_at": question.created_at
    }

@app.get("/api/teacher/questions")
async def get_teacher_questions(
    category: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """دریافت تمام سوالات معلم"""
    if current_user.role.value != "teacher":
        raise HTTPException(status_code=403, detail="فقط معلم‌ها می‌توانند سوالات خود را ببینند")
    
    query = db.query(Question).filter(Question.teacher_id == current_user.id)
    
    if category:
        query = query.filter(Question.category == category)
    
    questions = query.all()
    
    return [
        {
            "id": q.id,
            "title": q.title,
            "category": q.category,
            "difficulty": q.difficulty,
            "created_at": q.created_at
        }
        for q in questions
    ]

@app.get("/api/teacher/questions/{question_id}")
async def get_question_detail(
    question_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """دریافت جزئیات سوال"""
    question = db.query(Question).filter(Question.id == question_id).first()
    
    if not question:
        raise HTTPException(status_code=404, detail="سوال پیدا نشد")
    
    if question.teacher_id != current_user.id and current_user.role.value != "admin":
        raise HTTPException(status_code=403, detail="دسترسی رد شد")
    
    return {
        "id": question.id,
        "title": question.title,
        "description": question.description,
        "content": question.content,
        "category": question.category,
        "difficulty": question.difficulty,
        "correct_answer": question.correct_answer,
        "options": question.options,
        "explanation": question.explanation,
        "created_at": question.created_at
    }

@app.delete("/api/teacher/questions/{question_id}")
async def delete_question(
    question_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """حذف سوال"""
    question = db.query(Question).filter(Question.id == question_id).first()
    
    if not question:
        raise HTTPException(status_code=404, detail="سوال پیدا نشد")
    
    if question.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="فقط معلم مالک می‌تواند سوال را حذف کند")
    
    db.delete(question)
    db.commit()
    
    return {"message": "سوال با موفقیت حذف شد"}

@app.post("/api/teacher/quizzes/create")
async def create_quiz(
    data: QuizCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """ایجاد آزمون جدید"""
    if current_user.role.value != "teacher":
        raise HTTPException(status_code=403, detail="فقط معلم‌ها می‌توانند آزمون ایجاد کنند")
    
    # بررسی اینکه تمام سوالات متعلق به معلم است
    questions = db.query(Question).filter(
        Question.id.in_(data.question_ids),
        Question.teacher_id == current_user.id
    ).all()
    
    if len(questions) != len(data.question_ids):
        raise HTTPException(status_code=400, detail="برخی از سوالات متعلق به شما نیست")
    
    quiz = Quiz(
        teacher_id=current_user.id,
        title=data.title,
        description=data.description,
        category=data.category,
        question_ids=json.dumps(data.question_ids),
        time_limit=data.time_limit
    )
    
    db.add(quiz)
    db.commit()
    db.refresh(quiz)
    
    return {
        "id": quiz.id,
        "title": quiz.title,
        "category": quiz.category,
        "question_count": len(data.question_ids),
        "created_at": quiz.created_at
    }

@app.get("/api/teacher/quizzes")
async def get_teacher_quizzes(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """دریافت تمام آزمون‌های معلم"""
    if current_user.role.value != "teacher":
        raise HTTPException(status_code=403, detail="فقط معلم‌ها می‌توانند این را ببینند")
    
    quizzes = db.query(Quiz).filter(Quiz.teacher_id == current_user.id).all()
    
    return [
        {
            "id": q.id,
            "title": q.title,
            "category": q.category,
            "question_count": len(json.loads(q.question_ids)) if q.question_ids else 0,
            "is_active": q.is_active,
            "created_at": q.created_at
        }
        for q in quizzes
    ]

@app.get("/api/teacher/quizzes/{quiz_id}/results")
async def get_quiz_results(
    quiz_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """دریافت نتایج آزمون"""
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    
    if not quiz:
        raise HTTPException(status_code=404, detail="آزمون پیدا نشد")
    
    if quiz.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="دسترسی رد شد")
    
    grades = db.query(Grade).filter(Grade.quiz_id == quiz_id).all()
    
    return {
        "quiz_id": quiz_id,
        "quiz_title": quiz.title,
        "total_students": len(grades),
        "average_score": sum(g.percentage for g in grades) / len(grades) if grades else 0,
        "results": [
            {
                "student_id": g.student_id,
                "score": g.score,
                "percentage": g.percentage,
                "submitted_at": g.submitted_at
            }
            for g in grades
        ]
    }

# این دو مسیر را در فایل main.py جایگزین مسیرهای قبلی تولید سوال کنید

@app.post("/api/teacher/questions/generate")
async def generate_questions_ai(
    category: str = Form(...),
    difficulty: str = Form(...),
    count: int = Form(default=3),
    q_type: str = Form(default="test"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db) # اضافه شدن دیتابیس برای ذخیره خودکار
):
    """تولید سوال با هوش مصنوعی و ذخیره خودکار و آنی در بانک سوالات همگانی"""
    user_role = current_user.role if isinstance(current_user.role, str) else current_user.role.value
    if user_role != "teacher" and user_role != "admin":
        raise HTTPException(status_code=403, detail="دسترسی رد شد")
    
    type_instructions = {
        "test": "سوالات چهارگزینه‌ای. باید options (لیست 4 گزینه) داشته باشد.",
        "blank": "سوالات جای خالی. یک جای خالی با .......... در متن سوال بگذار. فیلد options نیاز نیست.",
        "tf": "سوالات درست/غلط. گزینه‌ها نیاز نیست. پاسخ صحیح فقط کلمه 'درست' یا 'غلط' باشد.",
        "match": "سوالات وصل کردنی. گزینه‌ها نیاز نیست.",
        "concept": "سوالات تشریحی و مفهومی. گزینه‌ها نیاز نیست.",
        "compute": "مسائل محاسباتی و حل‌کردنی. گزینه‌ها نیاز نیست."
    }
    instruction = type_instructions.get(q_type, type_instructions["test"])
    
    try:
        prompt = f"""
        {count} سوال ریاضی در سطح {difficulty} برای مبحث "{category}" تولید کن.
        نوع سوال: {instruction}
        فرمت خروجی حتماً یک آرایه JSON شامل آبجکت‌هایی با کلیدهای: question, correct_answer, explanation باشد. اگر چهارگزینه‌ای است کلید options را هم بگذار.
        تمام بک‌اسلش‌های LaTeX را دوبار تکرار کن (مثل \\\\frac).
        """
        
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": "You are a math test generator. Always return pure JSON arrays. Double escape backslashes."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )
        
        text = response.choices[0].message.content.replace("```json", "").replace("```", "").strip()
        text = re.sub(r'\\(?![/"\\bfnrt])', r'\\\\', text)
        
        questions = json.loads(text)
        if not isinstance(questions, list):
            questions = [questions]
            
        # 🌟 ذخیره خودکار در دیتابیس همگانی سیستم
        diff_map = {"easy": QuestionDifficulty.EASY, "medium": QuestionDifficulty.MEDIUM, "hard": QuestionDifficulty.HARD}
        db_difficulty = diff_map.get(difficulty.lower(), QuestionDifficulty.MEDIUM)
        
        for q in questions:
            opts_json = json.dumps(q.get("options")) if q.get("options") else None
            
            new_question = Question(
                teacher_id=current_user.id,
                title=f"سوال هوشمند - {category}",
                content=q.get("question") or q.get("content"),
                category=category,
                difficulty=db_difficulty,
                correct_answer=str(q.get("correct_answer")),
                options=opts_json,
                explanation=q.get("explanation")
            )
            db.add(new_question)
        
        db.commit() # ذخیره همه سوالات در دیتابیس
        return {"questions": questions}
        
    except Exception as e:
        print(f"❌ AI Generate & AutoSave Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/questions/global")
async def get_global_question_bank(
    category: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """دریافت کل بانک سوالات پلتفرم برای همه معلم‌ها"""
    query = db.query(Question)
    if category:
        query = query.filter(Question.category.ilike(f"%{category}%"))
        
    questions = query.order_by(Question.created_at.desc()).all()
    
    result = []
    for q in questions:
        # مدیریت ایمن ساختار آپشن‌ها
        parsed_opts = None
        if q.options:
            try: parsed_opts = json.loads(q.options)
            except: parsed_opts = [q.options]

        result.append({
            "id": q.id,
            "title": q.title,
            "content": q.content,
            "category": q.category,
            "difficulty": q.difficulty.value if hasattr(q.difficulty, 'value') else str(q.difficulty),
            "options": parsed_opts,
            "correct_answer": q.correct_answer,
            "explanation": q.explanation
        })
    return result

@app.post("/api/teacher/lesson-plan/generate")
async def generate_lesson_plan_ai(
    topic: str = Form(...),
    grade: str = Form(...),
    time: int = Form(...),
    current_user: User = Depends(get_current_user)
):
    """مسیر کاملاً جدید برای تولید طرح درس هوشمند"""
    try:
        prompt = f"""
        یک طرح درس ریاضی حرفه‌ای برای مبحث '{topic}' پایه '{grade}' با مدت زمان {time} دقیقه بنویس.
        فقط یک شیء (Object) با فرمت JSON خالص برگردون که دقیقاً شامل کلیدهای زیر باشد:
        "goals": (آرایه‌ای از ۳ هدف آموزشی)
        "intro": (پاراگراف ایجاد انگیزه)
        "activity": (مراحل تدریس و فعالیت کلاسی همراه با فرمول در صورت نیاز)
        "evaluation": (نحوه ارزشیابی پایانی)
        "homework": (تکلیف خانه)
        
        دقت کن تمام بک‌اسلش‌های فرمول‌های LaTeX باید دبل‌اسکیپ شوند (مثلا \\\\frac).
        """
        
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": "You are a professional math teacher assistant. Return pure JSON. Double escape backslashes."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )
        
        text = response.choices[0].message.content
        text = text.replace("```json", "").replace("```", "").strip()
        text = re.sub(r'\\(?![/"\\bfnrt])', r'\\\\', text)
        
        parsed_plan = json.loads(text)
        return parsed_plan
        
    except Exception as e:
        print(f"❌ Lesson Plan Error: {str(e)}")
        raise HTTPException(status_code=500, detail="خطا در تولید طرح درس")
@app.post("/api/teacher/lesson-plan/generate")
async def generate_lesson_plan_ai(
    topic: str = Form(...),
    grade: str = Form(...),
    time: int = Form(...),
    current_user: User = Depends(get_current_user)
):
    """تولید طرح درس هوشمند با هوش مصنوعی"""
    try:
        prompt = f"""
        یک طرح درس ریاضی حرفه‌ای برای مبحث '{topic}' پایه '{grade}' با مدت زمان {time} دقیقه بنویس.
        فقط یک شیء (Object) با فرمت JSON خالص برگردون که دقیقاً شامل کلیدهای زیر باشد:
        "goals": (آرایه‌ای از ۳ هدف آموزشی)
        "intro": (پاراگراف ایجاد انگیزه)
        "activity": (مراحل تدریس و فعالیت کلاسی همراه با فرمول در صورت نیاز)
        "evaluation": (نحوه ارزشیابی پایانی)
        "homework": (تکلیف خانه)
        
        دقت کن تمام بک‌اسلش‌های فرمول‌های LaTeX باید دبل‌اسکیپ شوند (مثلا \\\\frac).
        """
        
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": "You are a professional math teacher assistant. Return pure valid JSON. Double escape backslashes."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )
        
        text = response.choices[0].message.content
        text = text.replace("```json", "").replace("```", "").strip()
        
        # import re اینجا قرار داده شده تا مطمئن باشیم کتابخانه لود می‌شود
        import re
        text = re.sub(r'\\(?![/"\\bfnrt])', r'\\\\', text)
        
        parsed_plan = json.loads(text)
        return parsed_plan
        
    except Exception as e:
        print(f"❌ Lesson Plan Error: {str(e)}")
        raise HTTPException(status_code=500, detail="خطا در تولید طرح درس")
    
@app.get("/api/teacher/quizzes/export-html")

async def export_quiz_html(
    quiz_ids: str, # لیستی از IDها که با کاما جدا شده‌اند (مثلاً "1,5,8")
    include_answers: bool = True,
    db: Session = Depends(get_db)
):
    """تولید قالب HTML استاندارد برای چاپ PDF آزمون"""
    ids = [int(i) for i in quiz_ids.split(",")]
    questions = db.query(Question).filter(Question.id.in_(ids)).all()
    
    # ساخت قالب HTML ساده و مرتب برای چاپ
    html = f"""
    <html lang="fa" dir="rtl">
    <head><style>
        body {{ font-family: 'Vazirmatn', Tahoma, sans-serif; padding: 20mm; }}
        .q-item {{ margin-bottom: 25px; page-break-inside: avoid; }}
        .header {{ text-align: center; border-bottom: 2px solid #000; padding-bottom: 20px; }}
        .answer-key {{ margin-top: 10px; color: #555; border-top: 1px dashed #ccc; pt: 10px; }}
        {"" if include_answers else ".answer-key { display: none; }"}
    </style></head>
    <body>
        <div class="header"><h1>آزمون ریاضیات ایما</h1></div>
        { "".join([f'<div class="q-item"><h3>سوال {i+1}:</h3><p>{q.content}</p><div class="answer-key">پاسخ: {q.correct_answer}</div></div>' for i, q in enumerate(questions)]) }
    </body>
    </html>
    """
    return Response(content=html, media_type="text/html")

@app.post("/api/teacher/quizzes/save")
async def save_quiz_to_db(
    data: QuizCreate, # از همان مدلی که قبلا داشتی استفاده کن
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """ذخیره سبد سوالات به عنوان یک آزمون در دیتابیس"""
    quiz = Quiz(
        teacher_id=current_user.id,
        title=data.title,
        category=data.category,
        question_ids=json.dumps(data.question_ids), # ذخیره لیست ID سوالات
        time_limit=data.time_limit
    )
    db.add(quiz)
    db.commit()
    return {"quiz_id": quiz.id}
# ═══════════════════════════════════════════
# ═══════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    import sys
    import platform
    
    print("=" * 50)
    print("🚀 IMA Backend v2.0.1 (Speed Optimized)")
    print(f"🤖 Model: {AI_MODEL}")
    print(f"🔊 TTS: {TTS_MODEL}")
    print(f"👥 Users: {len(fake_db['users'])}")
    print(f"🔑 Admin: admin / admin123")
    print(f"💻 OS: {platform.system()}")
    print("=" * 50)
    
    # تنظیمات مخصوص ویندوز
    is_windows = platform.system() == "Windows"
    
    uvicorn.run(
        "main:app",
        host="127.0.0.1",  # استفاده از localhost به جای 0.0.0.0
        port=8000,
        reload=True,  # فعال کردن reload برای توسعه
        workers=1 if is_windows else 4,  # ویندوز: فقط 1 worker
        log_level="info",
        # حذف backlog مشکل‌ساز
    )