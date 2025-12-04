import os
from urllib.parse import quote_plus
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


class Config:
    
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
    MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587")) # 587 TLS, 465 SSL
    MAIL_USERNAME = os.getenv("MAIL_USERNAME", "hotelvillagrace@gmail.com")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "lnad ndxy nhyw knro")
    MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "1") == "1"
    MAIL_USE_SSL = os.getenv("MAIL_USE_SSL", "0") == "1"
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER", "hotelvillagrace@gmail.com")
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    UPLOAD_FOLDER_CLIENTES = os.path.join(BASE_DIR, 'uploads', 'clientes')
    ALLOWED_EXTENSIONS = {'.pdf', '.jpg', '.jpeg', '.png'}
    DB_USER_RAW = os.environ.get("DB_USER", "root")
    DB_PASSWORD_RAW = os.environ.get("DB_PASSWORD", "1234")
    DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
    DB_PORT = os.environ.get("DB_PORT", "3306")
    DB_NAME = os.environ.get("DB_NAME", "Hotel_VillaGrace")
    DB_USER = quote_plus(DB_USER_RAW)
    DB_PASSWORD = quote_plus(DB_PASSWORD_RAW)
    SQLALCHEMY_DATABASE_URI = (
        f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
        "?charset=utf8mb4"
    )

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Moneda base del hotel
    BASE_CURRENCY = "CRC"

    # API de tipo de cambio (base USD, incluye CRC)
    FX_API_URL = "https://open.er-api.com/v6/latest/USD"

    # Esta API NO requiere key, así que lo dejamos vacío
    FX_API_KEY = None



    print(
    "DB ->",
    os.environ.get("DB_USER", "root"),
    "@",
    os.environ.get("DB_HOST", "127.0.0.1"),
    ":",
    os.environ.get("DB_PORT", "3306"),
    "/",
    os.environ.get("DB_NAME", "Hotel_VillaGrace"),
)
