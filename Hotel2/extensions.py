from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_mail import Mail

db = SQLAlchemy(db = SQLAlchemy(engine_options={"connect_args": {"ssl": {"ca": "ca-certificate.crt"}}}))
migrate = Migrate()
mail = Mail()
