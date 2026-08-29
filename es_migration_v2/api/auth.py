import os
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import bcrypt
from jose import jwt, JWTError
from datetime import datetime, timedelta
from typing import Optional

# JWT Configuration
SECRET_KEY = os.getenv("SECRET_KEY", "7d4b29c1e309f4a56b2f76ea10c48e89ad720d5c80a2b5ef1e0d3f829f0c765a")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

# SMTP Configuration
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587")) if os.getenv("SMTP_PORT") else 587
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USERNAME or "no-reply@brainlynsearch.com")

def hash_password(password: str) -> str:
    # bcrypt expects bytes, so we encode string to bytes first
    password_bytes = password.encode('utf-8')
    # Generate salt and hash the password
    hashed_bytes = bcrypt.hashpw(password_bytes, bcrypt.gensalt())
    # Return as string (which is stored in SQLite)
    return hashed_bytes.decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        plain_bytes = plain_password.encode('utf-8')
        hashed_bytes = hashed_password.encode('utf-8')
        return bcrypt.checkpw(plain_bytes, hashed_bytes)
    except Exception:
        return False

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def decode_access_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None

def generate_otp() -> str:
    return "".join(random.choices("0123456789", k=6))

def send_otp_email(email: str, otp: str):
    """
    Sends the OTP verification code via email.
    If SMTP settings are not configured, prints the OTP to console for debugging.
    """
    subject = "Brainlynsearch - Email Verification Code"
    body = f"""
    <html>
      <body style="font-family: Arial, sans-serif; background-color: #0d1117; color: #c9d1d9; padding: 20px; text-align: center;">
        <div style="max-width: 500px; margin: 0 auto; background-color: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 30px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);">
          <h2 style="color: #ff8c00; margin-bottom: 20px;">Brainlynsearch Registration</h2>
          <p style="font-size: 16px; margin-bottom: 20px;">Use the following One-Time Password (OTP) to verify your account. This code is valid for 1 minute.</p>
          <div style="font-size: 32px; font-weight: bold; letter-spacing: 5px; color: #ff8c00; background-color: #0d1117; border: 1px dashed #30363d; padding: 15px; border-radius: 8px; display: inline-block; margin-bottom: 20px;">
            {otp}
          </div>
          <p style="font-size: 12px; color: #8b949e; margin-top: 20px;">If you did not request this verification, please ignore this email.</p>
        </div>
      </body>
    </html>
    """
    
    # Always print OTP to stdout/console for local development/debugging
    print(f"\n==========================================")
    print(f"[OTP] GENERATED OTP FOR {email}: {otp}")
    print(f"==========================================\n")
    
    if not SMTP_HOST or not SMTP_USERNAME or not SMTP_PASSWORD:
        print("[WARNING] SMTP credentials not fully configured. Email not sent. Use the OTP from the console above.")
        return
        
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = email
        
        part = MIMEText(body, "html")
        msg.attach(part)
        
        # Connect and send
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM, email, msg.as_string())
        server.quit()
        print(f"[SUCCESS] OTP successfully sent to email {email}")
    except Exception as e:
        print(f"[ERROR] Failed to send OTP email to {email}: {e}")
