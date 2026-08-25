# Lightsail 배포 순서

## 1. 인스턴스

- Ubuntu 24.04, 4GB RAM / 80GB SSD를 선택합니다.
- 네트워킹에서 TCP 80, 443을 엽니다. SSH 22는 본인 IP만 허용합니다.
- 도메인의 A 레코드를 인스턴스 공인 IP로 연결합니다.

## 2. Docker 설치

```bash
sudo apt update
sudo apt install -y ca-certificates curl git sqlite3
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
newgrp docker
docker compose version
```

## 3. 프로젝트 설정

```bash
sudo mkdir -p /opt/video-series-bot
sudo chown "$USER":"$USER" /opt/video-series-bot
cd /opt/video-series-bot
# 이 프로젝트 파일을 업로드 또는 git clone 합니다.
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f video-series
```

`.env`에는 새 영상 시리즈 봇의 토큰만 넣습니다. 기존 `@Thumbnailmakemkbot` 토큰은 넣지 않습니다.

## 4. BotFather 설정

새 영상 시리즈 봇에서 `/setmenubutton`을 선택한 뒤 `Web App`을 지정하고 URL에 `https://도메인`을 넣습니다.

## 5. SQLite 백업

```bash
crontab -e
# 매일 새벽 3시
0 3 * * * /opt/video-series-bot/scripts/backup-sqlite.sh
```

백업 폴더는 주기적으로 서버 밖 저장소로 복사합니다. 이 프로젝트는 서버에서 처리한 원본 영상을 보관하지 않으며, SQLite와 썸네일만 백업합니다.
