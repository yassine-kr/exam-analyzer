# Exam Analyzer

A small web application to upload **exam documents** and view their **analysis results**.
This is the standalone version (v0.1): the real analysis pipeline will be added in later labs.
For now each upload stores the file and shows basic metadata (type, size, word count for `.txt`).

- **Stack:** Python 3.12, FastAPI, SQLite, HTML templates
- **Storage:** everything lives in `/data` inside the container (DB + uploaded files)
- **Endpoints:** `/` (UI), `/health`, `/api/documents`, `/api/documents/{id}`

---

## Lab: run it on an EC2 instance

### 0. Launch the EC2 instance

- AMI: **Amazon Linux 2023**, type: `t3.micro` (or `t2.micro`)
- Key pair: create or reuse one
- **Security group, inbound rules:**

| Type  | Port | Source    | Used in |
|-------|------|-----------|---------|
| SSH   | 22   | My IP     | always  |
| HTTP  | 80   | 0.0.0.0/0 | step 1 & 2 |
| HTTPS | 443  | 0.0.0.0/0 | step 2 |

Connect: `ssh -i my-key.pem ec2-user@<PUBLIC_IP>` (or use **EC2 Instance Connect** in the console).

### 1. Install Docker and Git

```bash
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
newgrp docker            # apply the group without logging out

# Docker Compose plugin (needed for step 2)
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -sSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

docker --version && docker compose version
```

### 2. Get the application

```bash
git clone https://github.com/yassine-kr/exam-analyzer.git
cd exam-analyzer
```

---

## Step 1 — HTTP (the app alone)

```bash
docker build -t exam-analyzer .
docker run -d --name exam-analyzer -p 80:8000 -v exam-data:/data exam-analyzer
docker ps
```

Test:

- From the instance: `curl http://localhost/health`
- From your browser: **http://&lt;PUBLIC_IP&gt;** → upload a document, open it, see the analysis.

The header shows a red **"HTTP (not encrypted)"** badge: traffic between you and the server is plain text.

Useful commands:

```bash
docker logs -f exam-analyzer        # follow logs
docker exec -it exam-analyzer sh    # shell inside the container
docker stop exam-analyzer && docker rm exam-analyzer
```

> `-p 80:8000` maps port **80 of the EC2 host** to port **8000 of the container**.
> `-v exam-data:/data` keeps your documents when the container is removed.

---

## Step 2 — HTTPS (Nginx reverse proxy + TLS)

Stop the step-1 container first (it uses port 80):

```bash
docker stop exam-analyzer && docker rm exam-analyzer
docker compose up -d --build
docker compose ps
```

Architecture:

```
Browser ──HTTPS:443──▶ [ nginx ] ──HTTP:8000──▶ [ app ] ──▶ /data (volume)
        ──HTTP:80────▶ redirect to HTTPS
```

Test:

- **http://&lt;PUBLIC_IP&gt;** → redirected to **https://&lt;PUBLIC_IP&gt;**
- The browser shows a **security warning**: the certificate is **self-signed** (generated
  automatically at first start, no domain name). Click *Advanced → Proceed*.
- The header now shows a green **"HTTPS 🔒"** badge.
- From the instance: `curl -k https://localhost/health` (`-k` accepts the self-signed cert)

Inspect the certificate:

```bash
openssl s_client -connect localhost:443 </dev/null 2>/dev/null | openssl x509 -noout -subject -dates
```

Stop everything: `docker compose down` (add `-v` to also delete the data and certificate).

> In production you would use a real domain and a trusted certificate
> (Let's Encrypt, or AWS Certificate Manager on a Load Balancer / CloudFront).

---

## Run locally without Docker

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./data uvicorn app.main:app --reload
# open http://localhost:8000
```

## Configuration

| Variable        | Default | Meaning                         |
|-----------------|---------|---------------------------------|
| `DATA_DIR`      | `/data` | Where the DB and uploads live   |
| `MAX_UPLOAD_MB` | `20`    | Max upload size                 |

Allowed file types: `.pdf .docx .txt .png .jpg .jpeg`

## Roadmap

- [ ] Real analysis pipeline (`analyze_document` in `app/main.py`)
- [ ] Store files in S3 and metadata in DynamoDB
- [ ] Asynchronous processing (queue + worker)
- [ ] Deployment on ECS / Fargate
