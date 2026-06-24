"""bash 调用入口:增加 failure_count,打印新值。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _auto_queue import increment_failure

if __name__ == "__main__":
    queue_path = Path(sys.argv[1])
    session_id = sys.argv[2]
    new_value = increment_failure(queue_path, session_id)
    print(new_value)
