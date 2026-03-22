from flask_socketio import SocketIO
from flask_wtf.csrf import CSRFProtect

socketio = SocketIO()
csrf = CSRFProtect()

# Maps device_code -> Socket.IO session id for connected agents
agent_sids = {}
