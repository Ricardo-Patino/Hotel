import os
from urllib.parse import quote_plus


try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


class Config:
    
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

   
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


    
