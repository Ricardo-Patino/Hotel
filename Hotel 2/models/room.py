from datetime import datetime
from extensions import db

class Room(db.Model):
    __tablename__ = "rooms"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)    
    name = db.Column(db.String(120), nullable=False)                 
    capacity = db.Column(db.Integer, nullable=False, default=2)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<Room {self.code} - {self.name}>"
