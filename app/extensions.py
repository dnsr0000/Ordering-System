import threading
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
ORDER_CHECKOUT_LOCK = threading.Lock()
LLM_LOCK = threading.Lock()