FROM python:3.12-slim

# 브리즈번(QUT) 기준 시간대 — 캘린더/날짜 계산이 로컬과 일치하도록
ENV TZ=Australia/Brisbane
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 의존성 먼저 설치 (레이어 캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 코드만 복사 — 비밀정보(credentials/token/secrets)와 상태(state/)는 넣지 않는다.
# 이 파일들은 런타임에 볼륨/환경변수로 주입된다 (docker-compose.yml 참고).
COPY *.py ./

CMD ["python", "scheduler.py"]
