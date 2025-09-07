import threading
from datetime import datetime
from typing import Optional

import requests
from flask import Flask, request, render_template
from flask_socketio import SocketIO
from requests.auth import HTTPBasicAuth

app = Flask(__name__)
app.config['SECRET_KEY'] = 'docker-streamer-secret'
# Use threading async mode so this works without eventlet/gevent (useful on Windows)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Store user sessions and their docker clients
user_sessions: dict[str, dict] = {}
# active_streams maps "sid:container_id" -> {"thread": Thread, "stop_event": Event}
active_streams: dict[str, dict] = {}


class DockerAPIClient:
    def __init__(self, base_url: str, username: Optional[str] = None, password: Optional[str] = None):
        self.base_url = base_url.rstrip('/')
        self.auth = HTTPBasicAuth(username, password) if username and password else None

    def list_containers(self, all: bool = False):
        url = f"{self.base_url}/containers/json"
        params = {'all': str(all).lower()}
        try:
            kwargs = {'params': params, 'timeout': 10}
            if self.auth:
                kwargs['auth'] = self.auth

            response = requests.get(url, **kwargs)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Error listing containers: {e}")
            return None

    def stream_logs_generator(self, container_id: str, stop_event: threading.Event, follow: bool = True, tail: int = 50):
        """
        Generator that yields log lines until stop_event is set.
        Handles Docker's multiplexed stdout/stderr header if present.
        """
        url = f"{self.base_url}/containers/{container_id}/logs"
        params = {
            'follow': str(follow).lower(),
            'stdout': 'true',
            'stderr': 'true',
            'timestamps': 'false',
            'tail': str(tail)
        }

        try:
            kwargs = {
                'params': params,
                'stream': True,
                'timeout': (10, None)  # connect timeout, no read timeout
            }
            if self.auth:
                kwargs['auth'] = self.auth

            response = requests.get(url, **kwargs)
            response.raise_for_status()

            buffer = b''
            for chunk in response.iter_content(chunk_size=1024):
                if stop_event.is_set():
                    break
                if not chunk:
                    continue
                buffer += chunk
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    if not line:
                        continue
                    try:
                        # Docker may return multiplexed frames: 8-byte header then payload
                        # header format: 1 byte stream type (0=stdin,1=stdout,2=stderr), 3 bytes zeros, 4 byte length
                        # We operate on raw bytes:
                        payload = line
                        if len(payload) >= 8:
                            first_byte = payload[0]
                            # if first_byte looks like 1 or 2 (stdout/stderr), assume multiplexed -> strip 8
                            if first_byte in (1, 2):
                                payload = payload[8:]

                        decoded_line = payload.decode('utf-8', errors='ignore').rstrip('\r')
                        cleaned_line = decoded_line.strip()
                        if cleaned_line:
                            yield cleaned_line
                    except Exception as e:
                        print(f"Error decoding log line: {e}")
                        # skip problematic line
                        continue

                # check stop_event again in case it's set while processing buffer
                if stop_event.is_set():
                    break

        except Exception as e:
            print(f"Error streaming logs from Docker API: {e}")
            # propagate to caller so they can emit error
            raise

@app.route('/')
def index():
    return render_template("index.html")


def get_user_session(sid: str):
    """Get or create user session"""
    if sid not in user_sessions:
        user_sessions[sid] = {
            'docker_client': None,
            'docker_url': None,
            'connected': False
        }
    return user_sessions[sid]


@socketio.on('connect_docker')
def connect_docker(data):
    sid = request.sid
    session = get_user_session(sid)

    try:
        url = data['url'].strip()
        username = data.get('username')
        password = data.get('password')

        # Create Docker client for this user
        docker_client = DockerAPIClient(url, username, password)

        # Test connection
        containers = docker_client.list_containers()
        if containers is None:
            raise Exception("Failed to connect to Docker API")

        # Store successful connection
        session['docker_client'] = docker_client
        session['docker_url'] = url
        session['connected'] = True

        # Emit only to this client
        socketio.emit('docker_connected', {'url': url, 'containers_count': len(containers)}, room=sid)
        print(f"User {sid} connected to Docker at {url}")

    except Exception as e:
        error_msg = f"Docker connection failed: {str(e)}"
        print(f"Docker connection error for user {sid}: {error_msg}")
        socketio.emit('error', {'message': error_msg}, room=sid)

    finally:
        # signal client UI to re-enable connect button
        socketio.emit('docker_connect_ready', room=sid)


@socketio.on('disconnect_docker')
def disconnect_docker():
    sid = request.sid
    session = get_user_session(sid)

    # Stop any active log streams for this user
    stop_user_streams(sid)

    # Clear docker connection
    session['docker_client'] = None
    session['docker_url'] = None
    session['connected'] = False

    socketio.emit('docker_disconnected', room=sid)
    print(f"User {sid} disconnected from Docker")


@socketio.on('get_containers')
def get_containers():
    sid = request.sid
    session = get_user_session(sid)

    if not session['connected'] or not session['docker_client']:
        socketio.emit('error', {'message': 'Not connected to Docker'}, room=sid)
        return

    try:
        containers_data = session['docker_client'].list_containers(all=True)
        if containers_data is None:
            socketio.emit('error', {'message': 'Failed to get containers from Docker API'}, room=sid)
            return

        container_list = []
        for container in containers_data:
            container_list.append({
                'id': container.get('Id'),
                'name': (container.get('Names') or [''])[0].lstrip('/') if container.get('Names') else '',
                'image': container.get('Image'),
                'state': container.get('State')
            })

        socketio.emit('containers', container_list, room=sid)

    except Exception as e:
        print(f"Error getting containers for user {sid}: {e}")
        socketio.emit('error', {'message': f'Failed to get containers: {str(e)}'}, room=sid)


@socketio.on('start_logs')
def start_logs(data):
    sid = request.sid
    session = get_user_session(sid)
    container_id = data.get('container_id')

    if not container_id:
        socketio.emit('error', {'message': 'No container_id provided'}, room=sid)
        return

    if not session['connected'] or not session['docker_client']:
        socketio.emit('error', {'message': 'Not connected to Docker'}, room=sid)
        return

    # Create unique stream key for this user and container
    stream_key = f"{sid}:{container_id}"

    # Stop existing stream if any
    if stream_key in active_streams:
        stop_stream(stream_key)

    try:
        stop_event = threading.Event()

        def stream_logs(stop_event: threading.Event):
            try:
                # Send connection message
                socketio.emit('log', {
                    'message': f'Connected to container logs...',
                    'stream': 'system',
                    'timestamp': datetime.now().isoformat()
                }, room=sid)

                # Iterate over lines from Docker API
                for log_message in session['docker_client'].stream_logs_generator(container_id, stop_event, follow=True, tail=50):
                    if stop_event.is_set():
                        break

                    if log_message and log_message.strip():
                        socketio.emit('log', {
                            'message': log_message,
                            'stream': 'stdout',
                            'timestamp': datetime.now().isoformat()
                        }, room=sid)

            except Exception as e:
                print(f"Log streaming error for user {sid}: {e}")
                socketio.emit('error', {'message': f'Log stream error: {str(e)}'}, room=sid)
            finally:
                # Clean up mapping if present
                if stream_key in active_streams:
                    try:
                        del active_streams[stream_key]
                    except KeyError:
                        pass
                socketio.emit('log', {
                    'message': 'Log stream ended',
                    'stream': 'system',
                    'timestamp': datetime.now().isoformat()
                }, room=sid)

        # Start the thread
        thread = threading.Thread(target=stream_logs, args=(stop_event,), daemon=True)
        active_streams[stream_key] = {'thread': thread, 'stop_event': stop_event}
        thread.start()

    except Exception as e:
        print(f"Error starting logs for user {sid}: {e}")
        socketio.emit('error', {'message': f'Failed to start logs: {str(e)}'}, room=sid)


@socketio.on('stop_logs')
def handle_stop_logs():
    sid = request.sid
    # Stop all streams for this user
    stop_user_streams(sid)

    socketio.emit('log', {
        'message': 'Disconnected from logs',
        'stream': 'system',
        'timestamp': datetime.now().isoformat()
    }, room=sid)


def stop_stream(stream_key: str):
    """Signal the stream's stop_event and remove it from active_streams."""
    info = active_streams.get(stream_key)
    if not info:
        return
    ev = info.get('stop_event')
    if isinstance(ev, threading.Event):
        ev.set()
    # Remove mapping immediately; thread's finally will tolerate absence
    try:
        del active_streams[stream_key]
    except KeyError:
        pass


def stop_user_streams(sid: str):
    keys = [k for k in list(active_streams.keys()) if k.startswith(f"{sid}:")]
    for k in keys:
        stop_stream(k)


@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    print(f"Client {sid} disconnected")

    # Clean up user session streams
    stop_user_streams(sid)

    # Remove user session
    if sid in user_sessions:
        del user_sessions[sid]


if __name__ == '__main__':
    print("🐳 Starting Multi-User Docker Log Streamer...")
    print("📱 Open http://localhost:5000 in your browser")
    print("⚙️ Configure your Docker connection in the Settings panel")

    # debug=True is fine for development
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
