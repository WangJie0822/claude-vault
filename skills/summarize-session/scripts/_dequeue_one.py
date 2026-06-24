"""bash 调用入口:出队指定 session_id。用法: python3 _dequeue_one.py <queue> <session_id>"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _auto_queue import dequeue_by_session

if __name__ == "__main__":
    queue_path = Path(sys.argv[1])
    session_id = sys.argv[2]
    dequeue_by_session(queue_path, [session_id])
