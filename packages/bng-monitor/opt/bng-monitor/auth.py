"""Authentication helpers."""
import time
from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from passlib.context import CryptContext
from config import SECRET_KEY, ACCESS_TOKEN_EXPIRE_MINUTES, DEFAULT_ADMIN_USER, DEFAULT_ADMIN_PASS

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)

def create_access_token(username: str, role: str = "admin") -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": username, "role": role, "exp": expire}, SECRET_KEY, algorithm="HS256")

def decode_token(token: str) -> dict | None:
    """Returns dict with username and role, or None."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        username = payload.get("sub")
        if not username:
            return None
        return {"username": username, "role": payload.get("role", "admin")}
    except JWTError:
        return None

ALL_PAGES = '["dashboard","sessions","trace","br-mgmt","alerts","history","radius","traffic","users"]'

async def ensure_admin_user(db):
    """Create default admin if no users exist."""
    row = await db.execute("SELECT COUNT(*) as cnt FROM users")
    count = (await row.fetchone())[0]
    if count == 0:
        hashed = hash_password(DEFAULT_ADMIN_PASS)
        await db.execute(
            "INSERT INTO users (username, hashed_password, created_at, role, allowed_pages) VALUES (?, ?, ?, ?, ?)",
            (DEFAULT_ADMIN_USER, hashed, time.time(), "admin", ALL_PAGES)
        )
        await db.commit()
