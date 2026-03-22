from flask_socketio import SocketIO

socketio = SocketIO()

# Maps device_code -> Socket.IO session id for connected agents
agent_sids = {}
