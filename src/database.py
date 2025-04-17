from sqlalchemy import create_engine, Column, String, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from cryptography.fernet import Fernet
import os

# Configure encryption key (in production, set via env var)
# Example: export ENCRYPTION_KEY=$(openssl rand -base64 32)
# Must be a base64-encoded 32-byte string => 44 characters in base64 form.
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "2TEevbgZHhJE2yz8z3dCC66ekbnHf53Yb_MkhiUw2aY=")
fernet = Fernet(ENCRYPTION_KEY)

# Configure SQLAlchemy
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///plaidify.db")
engine = create_engine(DATABASE_URL, echo=False)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def init_db():
    Base.metadata.create_all(bind=engine)

class Link(Base):
    __tablename__ = "links"
    link_token = Column(String, primary_key=True, index=True)
    site = Column(String, nullable=False)

class AccessToken(Base):
    __tablename__ = "access_tokens"
    token = Column(String, primary_key=True, index=True)
    link_token = Column(String)
    username_encrypted = Column(Text)
    password_encrypted = Column(Text)

    instructions = Column(Text, nullable=True)

def encrypt_password(plaintext: str) -> str:
    return fernet.encrypt(plaintext.encode()).decode()

def decrypt_password(ciphertext: str) -> str:
    return fernet.decrypt(ciphertext.encode()).decode()