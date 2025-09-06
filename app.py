from flask import Flask, render_template_string
from flask_socketio import SocketIO, emit
import docker
import threading
import json
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'docker-streamer-secret'
socketio = SocketIO(app, cors_allowed_origins="*")

# Initialize Docker client
try:
    # For Docker on custom port 2375
    client = docker.DockerClient(base_url='tcp://localhost:2375')
    client.ping()
    print("✅ Docker connection successful on port 2375")
except Exception as e:
    print(f"❌ Docker connection failed: {e}")
    try:
        # Fallback to default socket
        client = docker.from_env()
        client.ping()
        print("✅ Docker connection successful using default socket")
    except Exception as e2:
        print(f"❌ All Docker connection methods failed: {e2}")
        client = None

# Store active streams
active_streams = {}

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>Docker Log Streamer</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <style>
        body { 
            font-family: monospace; 
            background: #1a1a1a; 
            color: #fff; 
            margin: 0; 
            padding: 20px; 
        }
        .header { 
            margin-bottom: 20px; 
        }
        .container { 
            display: flex; 
            height: 80vh; 
        }
        .sidebar { 
            width: 300px; 
            background: #333; 
            padding: 10px; 
            margin-right: 20px; 
            border-radius: 5px;
            overflow-y: auto;
        }
        .logs { 
            flex: 1; 
            background: #000; 
            padding: 10px; 
            border-radius: 5px;
            overflow-y: auto;
            font-size: 12px;
            line-height: 1.4;
        }
        .container-item { 
            padding: 10px; 
            background: #555; 
            margin: 5px 0; 
            border-radius: 3px; 
            cursor: pointer; 
        }
        .container-item:hover { 
            background: #666; 
        }
        .container-item.active { 
            background: #007acc; 
        }
        .log-line { 
            margin: 2px 0; 
        }
        .stderr { 
            color: #ff6b6b; 
        }
        .stdout { 
            color: #fff; 
        }
        .system { 
            color: #4CAF50; 
            font-style: italic; 
        }
        button { 
            background: #007acc; 
            color: white; 
            border: none; 
            padding: 8px 15px; 
            margin: 5px; 
            border-radius: 3px; 
            cursor: pointer; 
        }
        button:disabled { 
            background: #555; 
            cursor: not-allowed; 
        }
        .status { 
            display: inline-block; 
            padding: 5px 10px; 
            border-radius: 3px; 
            font-size: 12px; 
            font-weight: bold; 
        }
        .status.connected { 
            background: #4CAF50; 
            color: white; 
        }
        .status.disconnected { 
            background: #f44336; 
            color: white; 
        }
        .loading { 
            text-align: center; 
            color: #999; 
            padding: 20px; 
        }
    </style>
</head>
<body>
    <div class="header">
        <h2>🐳 Docker Log Streamer</h2>
        <button onclick="refreshContainers()">Refresh</button>
        <button id="connectBtn" onclick="toggleConnection()" disabled>Connect</button>
        <button onclick="clearLogs()">Clear</button>
        <span id="status" class="status disconnected">Disconnected</span>
    </div>

    <div class="container">
        <div class="sidebar">
            <div id="containers" class="loading">Loading containers...</div>
        </div>
        <div class="logs" id="logs">Select a container to view logs</div>
    </div>

    <script>
        const socket = io();
        let selectedContainer = null;
        let isConnected = false;

        socket.on('connect', function() {
            refreshContainers();
        });

        socket.on('containers', function(containers) {
            const containersDiv = document.getElementById('containers');
            if (containers.length === 0) {
                containersDiv.innerHTML = '<div>No running containers</div>';
                return;
            }

            containersDiv.innerHTML = containers.map(container => 
                `<div class="container-item" onclick="selectContainer('${container.id}', '${container.name}')" data-id="${container.id}">
                    <strong>${container.name}</strong><br>
                    <small>${container.id.substring(0, 12)}</small><br>
                    <small>${container.image}</small>
                </div>`
            ).join('');
        });

        socket.on('log', function(data) {
            const logsDiv = document.getElementById('logs');
            const logLine = document.createElement('div');
            logLine.className = `log-line ${data.stream}`;
            logLine.textContent = `[${new Date(data.timestamp).toLocaleTimeString()}] ${data.message}`;
            logsDiv.appendChild(logLine);
            logsDiv.scrollTop = logsDiv.scrollHeight;
        });

        socket.on('error', function(data) {
            const logsDiv = document.getElementById('logs');
            const logLine = document.createElement('div');
            logLine.className = 'log-line system';
            logLine.textContent = `ERROR: ${data.message}`;
            logsDiv.appendChild(logLine);
        });

        function refreshContainers() {
            socket.emit('get_containers');
        }

        function selectContainer(containerId, containerName) {
            selectedContainer = { id: containerId, name: containerName };

            // Update UI
            document.querySelectorAll('.container-item').forEach(item => {
                item.classList.remove('active');
            });
            document.querySelector(`[data-id="${containerId}"]`).classList.add('active');
            document.getElementById('connectBtn').disabled = false;

            if (isConnected) {
                toggleConnection(); // Disconnect first
            }
        }

        function toggleConnection() {
            if (isConnected) {
                socket.emit('stop_logs');
                isConnected = false;
                document.getElementById('connectBtn').textContent = 'Connect';
                document.getElementById('status').textContent = 'Disconnected';
                document.getElementById('status').className = 'status disconnected';
            } else {
                if (selectedContainer) {
                    socket.emit('start_logs', { container_id: selectedContainer.id });
                    isConnected = true;
                    document.getElementById('connectBtn').textContent = 'Disconnect';
                    document.getElementById('status').textContent = 'Connected';
                    document.getElementById('status').className = 'status connected';
                }
            }
        }

        function clearLogs() {
            document.getElementById('logs').innerHTML = '';
        }
    </script>
</body>
</html>
'''


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@socketio.on('get_containers')
def get_containers():
    if not client:
        emit('error', {'message': 'Docker not connected'})
        return

    try:
        containers = client.containers.list(filters={'status': 'running'})
        container_list = []

        for container in containers:
            container_list.append({
                'id': container.id,
                'name': container.name,
                'image': container.image.tags[0] if container.image.tags else 'unknown'
            })

        emit('containers', container_list)
    except Exception as e:
        emit('error', {'message': f'Failed to get containers: {str(e)}'})


@socketio.on('start_logs')
def start_logs(data):
    if not client:
        emit('error', {'message': 'Docker not connected'})
        return

    container_id = data['container_id']

    # Stop existing stream if any
    if container_id in active_streams:
        stop_stream(container_id)

    try:
        container = client.containers.get(container_id)

        def stream_logs():
            try:
                for log in container.logs(stream=True, follow=True, stdout=True, stderr=True):
                    if container_id not in active_streams:
                        break

                    message = log.decode('utf-8', errors='ignore').strip()
                    if message:
                        socketio.emit('log', {
                            'message': message,
                            'stream': 'stdout',
                            'timestamp': datetime.now().isoformat()
                        })

            except Exception as e:
                socketio.emit('error', {'message': f'Log stream error: {str(e)}'})
            finally:
                if container_id in active_streams:
                    del active_streams[container_id]

        # Start streaming in background thread
        thread = threading.Thread(target=stream_logs)
        thread.daemon = True
        active_streams[container_id] = thread
        thread.start()

        emit('log', {
            'message': f'Connected to container: {container.name}',
            'stream': 'system',
            'timestamp': datetime.now().isoformat()
        })

    except Exception as e:
        emit('error', {'message': f'Failed to start logs: {str(e)}'})


@socketio.on('stop_logs')
def stop_logs():
    # Stop all active streams
    for container_id in list(active_streams.keys()):
        stop_stream(container_id)


def stop_stream(container_id):
    if container_id in active_streams:
        del active_streams[container_id]


@socketio.on('disconnect')
def disconnect():
    # Clean up streams when client disconnects
    stop_logs()


if __name__ == '__main__':
    print("🐳 Starting Docker Log Streamer...")
    print("📱 Open http://localhost:5000 in your browser")
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)

# Installation Instructions
"""
1. Install dependencies:
   pip install flask flask-socketio docker

2. Make sure Docker is running on your system

3. Run the application:
   python app.py

4. Open http://localhost:5000 in your browser

Features:
- Lists running Docker containers
- Real-time log streaming
- Simple, clean interface  
- Minimal code for prototype use

Note: Make sure your user has Docker permissions:
sudo usermod -aG docker $USER
(then logout and login again)
"""