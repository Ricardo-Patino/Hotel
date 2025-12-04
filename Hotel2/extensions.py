from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

db = SQLAlchemy(engine_options={"connect_args":{"ssl":{"ca":"ca-certificate.crt"}}})
migrate = Migrate()
