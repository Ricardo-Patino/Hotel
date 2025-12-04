from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

db = SQLAlchemy(connect_args={"ssl":{"ca":"ca-certificate.crt"}})
migrate = Migrate()
