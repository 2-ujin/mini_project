"""
30분마다 run_all 을 실행하는 간단한 루프 (Docker 컨테이너의 기본 실행 명령).
무료 한도 보호는 run_all 의 캐시(state/processed.json)와 일일 하드 캡(state/quota.json)에 맡긴다.
한 사이클이 실패해도 루프가 죽지 않도록 예외를 잡는다.
로그는 print 로 남겨 `docker compose logs` 에서 보이게 한다.
"""

import sys
import time
import traceback
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")

import run_all

INTERVAL_SECONDS = 30 * 60  # 30분


def main():
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] scheduler 시작 — {INTERVAL_SECONDS//60}분 간격", flush=True)
    while True:
        try:
            run_all.main()
        except Exception:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 사이클 오류 (다음 사이클 계속):", flush=True)
            traceback.print_exc()
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 다음 실행까지 {INTERVAL_SECONDS//60}분 대기...", flush=True)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
