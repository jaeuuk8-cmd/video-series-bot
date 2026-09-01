# 개인 영상 시리즈 텔레그램 봇

여러 영상을 한꺼번에 보내면 하나의 시리즈로 등록하고, 실제 다운로드 파일명을 `1.mp4`, `2.mp4`처럼 다시 부여합니다. MP4·MOV·AVI를 지원하며, 형식이 다른 영상은 재인코딩하지 않고 `1.mov`, `2.avi`처럼 실제 확장자를 유지합니다. 대표 썸네일과 시리즈 목록은 텔레그램 Mini App에서 봅니다.

## 구성

- `app`: 봇, SQLite, 썸네일 생성, Mini App API
- `telegram-bot-api`: 2GB 파일 처리를 위한 로컬 Telegram Bot API
- `caddy`: HTTPS와 Mini App 공개 주소
- `data`: SQLite, 썸네일, Telegram 처리 임시 파일 (서버에서만 보관)

원본 영상은 이름 변경 후 텔레그램에 문서로 재업로드하고, 서버의 임시 원본은 삭제합니다. SQLite와 썸네일만 `data/`에 남습니다.

로컬 Bot API와 앱은 `data/jobs`를 함께 마운트합니다. 이 공유 경로가 있어야 순번을 바꾼 영상을 Telegram에 다시 업로드할 수 있습니다.

## 서버 준비

1. Ubuntu 24.04 Lightsail 4GB/80GB 인스턴스를 만들고 80, 443 포트를 엽니다.
2. 도메인의 A 레코드를 이 서버 IP로 연결합니다.
3. Docker Engine과 Docker Compose plugin을 설치합니다.
4. `.env.example`을 `.env`로 복사해 값을 입력합니다. 비밀값은 Git에 올리지 않습니다.
5. `docker compose up -d --build`를 실행합니다.

## 필요한 비밀값

- `BOT_TOKEN`: 새 영상 시리즈 봇의 BotFather 토큰
- `OWNER_TELEGRAM_ID`: 본인의 숫자 Telegram ID
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`: [my.telegram.org](https://my.telegram.org)의 API development tools에서 발급
- `PUBLIC_HOST`: Mini App에 쓸 도메인

## 사용 방법

1. 새 봇에 **파일(문서) 형식**으로 MP4, MOV 또는 AVI 여러 개를 연달아 보냅니다.
2. 마지막 전송 후 5초가 지나면 봇이 시리즈 제목을 묻습니다.
3. 제목을 보내면 업로드 순서대로 `1.mp4`, `2.mp4` …로 재업로드됩니다.
4. `/library`를 누르면 썸네일 목록이 열립니다.

기본 대표 이미지는 첫 영상입니다. 이후 `/cover 시리즈번호 영상번호`로 변경할 수 있습니다. 예를 들어 `/cover 1 3`은 1번 시리즈의 3번째 영상을 대표 썸네일로 설정합니다.

`/cancel`은 대기 중인 작업을 취소하며, 이미 처리 중이면 현재 파일을 마친 뒤 중지합니다. 일부 파일만 실패하면 `/retry`로 실패한 파일만 다시 처리할 수 있습니다.

등록 작업과 각 파일의 상태는 SQLite에 저장됩니다. 서버나 컨테이너가 재시작되어도 제목 대기·처리 대기 작업을 복구하고, 이미 완료된 파일은 중복 등록하지 않습니다. Telegram의 일시적인 연결 오류와 429/5xx 응답은 지수 백오프로 최대 4회 자동 재시도합니다.

## 운영 메모

- 1GB 이상의 파일을 처리하므로 Lightsail 4GB/80GB보다 작은 서버는 권장하지 않습니다.
- `data/library.db`는 매일 서버 외부에 백업하세요.
- 기존 `@Thumbnailmakemkbot`은 이 프로젝트와 별개로 유지할 수 있습니다. 두 봇은 각자 다른 `BOT_TOKEN`을 씁니다.
