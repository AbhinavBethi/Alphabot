"""
auth.py
───────
All authentication logic lives here:
  - Password hashing with bcrypt
  - JWT token creation
  - JWT token verification
  - get_current_user() FastAPI dependency
    (used in every protected route)
"""

import os
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import User

load_dotenv()

# ─────────────────────────────────────────────
#  Config from .env
# ─────────────────────────────────────────────
JWT_SECRET          = os.getenv("JWT_SECRET", "change_this_in_production")
JWT_ALGORITHM       = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES  = int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))  # 24 hours

# ─────────────────────────────────────────────
#  Password hashing
#  bcrypt automatically salts passwords —
#  two identical passwords produce different
#  hashes, protecting against rainbow tables.
# ─────────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    """Hash a plain-text password. Store the result, never the plain text."""
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if plain matches the stored hash."""
    return pwd_context.verify(plain, hashed)


# ─────────────────────────────────────────────
#  JWT helpers
# ─────────────────────────────────────────────
def create_access_token(user_id: int, username: str) -> str:
    """
    Create a signed JWT containing:
      sub  → user_id  (standard JWT subject claim)
      name → username (for convenience in frontend)
      exp  → expiry timestamp
    """
    expire  = datetime.utcnow() + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {
        "sub":  str(user_id),
        "name": username,
        "exp":  expire,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """
    Decode and verify a JWT.
    Returns the payload dict on success, None on failure.
    """
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None


# ─────────────────────────────────────────────
#  OAuth2 scheme
#  Tells FastAPI to look for:
#    Authorization: Bearer <token>
#  in request headers for protected routes.
# ─────────────────────────────────────────────
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


# ─────────────────────────────────────────────
#  get_current_user dependency
#  Add `current_user: User = Depends(get_current_user)`
#  to any route that requires authentication.
# ─────────────────────────────────────────────
def get_current_user(
    token: str          = Depends(oauth2_scheme),
    db:    Session      = Depends(get_db),
) -> User:
    """
    FastAPI dependency:
      1. Extracts Bearer token from Authorization header
      2. Decodes and verifies JWT signature + expiry
      3. Looks up user in DB
      4. Returns User ORM object, or raises 401

    Usage in a router:
        @router.get("/me")
        def my_profile(current_user: User = Depends(get_current_user)):
            return current_user
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials. Please log in again.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exception

    user_id: str = payload.get("sub")
    if user_id is None:
        raise credentials_exception

    user = db.query(User).filter(User.id == int(user_id)).first()
    if user is None:
        raise credentials_exception

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated.",
        )

    return user