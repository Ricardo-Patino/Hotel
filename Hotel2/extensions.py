from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

db = SQLAlchemy(engine_options=dict(ssl_ca="ca-certificate.crt"))
migrate = Migrate()
