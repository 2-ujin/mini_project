# 이메일 에이전트 (MVP / 1단계) — Google Gemini 무료 API 버전

Gmail 최근 메일을 읽어 Gemini AI로 자동 분류합니다.
(현재 단계: 분류 결과를 화면에 출력만. 캘린더/라벨은 다음 단계)

## 준비물 2가지 (둘 다 무료, 신용카드 불필요)

### 1) Gemini API 키 (AI 분류용, 무료)
1. https://aistudio.google.com/apikey 접속 → Google 계정 로그인
2. **Create API key** 클릭 → 키 복사 (`AIza...`)
3. PowerShell에서 환경변수로 등록:
   ```powershell
   setx GEMINI_API_KEY "AIza여기에-복사한-키"
   ```
   → 등록 후 **터미널을 새로 열어야** 적용됩니다.

> 무료 티어에는 분당/일당 요청 제한이 있지만, 이메일 몇 개 분류하는 정도는 충분합니다.

### 2) Gmail API 접근 (credentials.json)
1. https://console.cloud.google.com 접속
2. 새 프로젝트 생성 (예: "email-agent")
3. **API 및 서비스 → 라이브러리** → "Gmail API" 검색 → **사용 설정**
4. **API 및 서비스 → OAuth 동의 화면** → External → 앱 이름 입력 → 본인 이메일을 테스트 사용자로 추가
5. **사용자 인증 정보 → 사용자 인증 정보 만들기 → OAuth 클라이언트 ID**
   - 애플리케이션 유형: **데스크톱 앱**
6. 생성된 클라이언트의 **JSON 다운로드** → 파일 이름을 `credentials.json`으로 바꿔
   이 폴더(`C:\Users\yujin\email-agent`)에 넣기

## 실행 방법

```powershell
cd C:\Users\yujin\email-agent
python -m pip install -r requirements.txt
python classify.py
```

- 최초 실행 시 브라우저가 열리며 Gmail 접근을 승인하라고 합니다 → 본인 계정으로 승인
- 승인 후 `token.json`이 자동 생성됩니다 (다음부터는 승인 불필요)

## 보안 주의
- `credentials.json`, `token.json`, API 키는 **절대 공유하지 마세요.**
- 이 파일들은 남에게 보내거나 GitHub에 올리면 안 됩니다.

## 다음 단계 (예정)
- 2단계: Gmail 라벨 자동 부착
- 3단계: 과제 마감일 추출 → Google 캘린더 일정 등록
- 4단계: Docker로 포장 + 주기적 자동 실행
