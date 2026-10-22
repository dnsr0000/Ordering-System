from app.extensions import db


class AuthThrottle(db.Model):
    """
    登入失敗計數與鎖定 (PIN 登入)。存在資料庫而非行程記憶體：
    重啟不會清空、多個行程 (線上部署) 也共用同一份狀態。
    key 例如 "pin:acct:<店家>:<手機>"、"pin:dev:<店家>:<手機>:<IP>"、"pin:ip:<店家>:<IP>"。
    """
    __tablename__ = 'auth_throttle'

    key = db.Column(db.String(200), primary_key=True)
    fails = db.Column(db.Integer, nullable=False, default=0)
    strikes = db.Column(db.Integer, nullable=False, default=0)
    window_start = db.Column(db.DateTime, nullable=True)
    locked_until = db.Column(db.DateTime, nullable=True)
