import time
import json
import uuid
import zmq
import threading

class TelemetryPublisher:
    def __init__(self, port=5555):
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PUB)
        # We bind to tcp to allow subscribers to connect
        self.socket.bind(f"tcp://127.0.0.1:{port}")
        
        self.sequence = 0
        self.source_id = str(uuid.uuid4())
        self.lock = threading.Lock()
        
    def publish(self, source: str, event_type: str, level: str, payload: dict):
        with self.lock:
            self.sequence += 1
            seq = self.sequence
            
        event = {
            "schema_version": 1,
            "timestamp": time.time(),
            "sequence": seq,
            "source_id": self.source_id,
            "source": source,
            "type": event_type,
            "level": level.upper(),
            "payload": payload
        }
        
        # ZeroMQ PUB/SUB allows prefix filtering. 
        # We use 'telemetry' as the root topic.
        message = f"telemetry {json.dumps(event)}"
        self.socket.send_string(message)

# Global Singleton for PHOENIX
_instance = None
_instance_lock = threading.Lock()

def get_publisher():
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = TelemetryPublisher()
    return _instance

def publish_event(source: str, event_type: str, level: str, payload: dict = None):
    if payload is None:
        payload = {}
    try:
        pub = get_publisher()
        pub.publish(source, event_type, level, payload)
    except Exception as e:
        # Telemetry should NEVER crash the main process
        print(f"[TELEMETRY ERROR] {e}")
