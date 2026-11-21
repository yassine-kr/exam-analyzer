# Exam Analyzer

A small web application to upload **exam documents** and view their **analysis results**.

- **v0.2 (current):** documents stored in **Amazon S3**, metadata in **Amazon DynamoDB**
- **v0.1:** standalone version (local SQLite + local files): `git checkout v0.1.0`

The real analysis pipeline will be added in later labs. For now each upload is stored
and gets basic metadata (type, size, word count for `.txt`).

```
                       ┌──────────────── EC2 instance (IAM role) ────────────────┐
Browser ──HTTP/HTTPS──▶│ [ nginx ] ──▶ [ app container ] ──boto3──┐               │
                       └──────────────────────────────────────────┼───────────────┘
                                                  ┌───────────────┴──────────────┐
                                                  ▼                              ▼
                                        S3 bucket: the files        DynamoDB table: metadata
                                        uploads/<id>.pdf            id, title, course, s3_key, ...
```

- **Stack:** Python 3.12, FastAPI, boto3, HTML templates
- **Endpoints:** `/` (UI), `/health` (app up), `/health/aws` (S3 + DynamoDB reachable),
  `/api/documents`, `/api/documents/{id}`
- **No AWS keys in the code or the image:** credentials come from the **EC2 IAM role**.

---

## Part A — Create the AWS resources (console, region **eu-central-1 Frankfurt**)

### A1. S3 bucket
**S3 → Create bucket**
- Name: `exam-analyzer-<your-name>` (must be globally unique, lowercase)
- Region: `eu-central-1`
- Keep **Block all public access** ✅ (the bucket stays private; downloads use temporary pre-signed URLs)

### A2. DynamoDB table
**DynamoDB → Create table**
- Table name: `exam-documents`
- Partition key: `id` — type **String**
- Table settings: default (on-demand)

### A3. IAM role for EC2
**IAM → Roles → Create role**
1. Trusted entity: **AWS service → EC2**
2. Skip the managed policies → name it `exam-analyzer-ec2-role` → **Create role**
3. Open the role → **Add permissions → Create inline policy → JSON**, paste
   [`iam-policy.json`](iam-policy.json) and **replace `BUCKET_NAME`** with your bucket name
   (2 places) → name it `exam-analyzer-access` → **Create policy**

The policy gives **only** what the app needs: put/get/delete objects under `uploads/` in
your bucket, and put/get/delete/scan items in the `exam-documents` table.

---

## Part B — Launch the EC2 instance

**EC2 → Launch instance**
- AMI: **Ubuntu Server 24.04 LTS**, type: `t3.micro`
- Key pair: create or reuse one
- **Security group, inbound rules:**

| Type  | Port | Source    | Used in |
|-------|------|-----------|---------|
| SSH   | 22   | My IP     | always  |
| HTTP  | 80   | 0.0.0.0/0 | step 1 & 2 |
| HTTPS | 443  | 0.0.0.0/0 | step 2 |

- **Advanced details:**
  - **IAM instance profile:** `exam-analyzer-ec2-role`
  - **Metadata response hop limit:** `2` ⚠️

> **Why hop limit 2?** The app runs *inside a Docker container*, one network "hop"
> further than the instance itself. With the default limit (1) the container cannot
> reach the instance metadata service, so it gets **no credentials** from the role.
>
> Already launched? Attach the role with **Actions → Security → Modify IAM role**, and
> set the hop limit with **Actions → Instance settings → Modify instance metadata options**,
> or with the CLI:
> `aws ec2 modify-instance-metadata-options --instance-id i-xxxx --http-put-response-hop-limit 2 --http-tokens required`

### Install Docker, Docker Compose and Git

Connect: `ssh -i my-key.pem ubuntu@<PUBLIC_IP>` (or **EC2 Instance Connect**).

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
newgrp docker            # apply the group without logging out

docker --version && docker compose version
```

### Get the application and configure it

```bash
git clone https://github.com/yassine-kr/exam-analyzer.git
cd exam-analyzer
cp .env.example .env
nano .env                # set S3_BUCKET to your bucket name
```

`.env`:

```ini
AWS_REGION=eu-central-1
S3_BUCKET=exam-analyzer-<your-name>
DYNAMODB_TABLE=exam-documents
```

---

## Step 1 — HTTP (the app alone)

```bash
docker build -t exam-analyzer .
docker run -d --name exam-analyzer -p 80:8000 --env-file .env exam-analyzer
docker ps
```

Test:

1. `curl http://localhost/health/aws` → must show `"s3":"ok"` and `"dynamodb":"ok"`
2. Browser: **http://&lt;PUBLIC_IP&gt;** → upload a document, open it, download it.
3. Verify in the AWS console:
   - **S3 → your bucket → `uploads/`**: the file is there
   - **DynamoDB → Tables → exam-documents → Explore table items**: one item per document
4. **Stateless test:** delete and recreate the container — your documents are still there,
   because nothing is stored in the container anymore:
   ```bash
   docker rm -f exam-analyzer
   docker run -d --name exam-analyzer -p 80:8000 --env-file .env exam-analyzer
   ```

The header shows a red **"HTTP (not encrypted)"** badge.

Useful commands:

```bash
docker logs -f exam-analyzer        # follow logs
docker exec -it exam-analyzer sh    # shell inside the container
docker rm -f exam-analyzer          # stop + remove
```

---

## Step 2 — HTTPS (Nginx reverse proxy + TLS)

```bash
docker rm -f exam-analyzer          # free port 80
docker compose up -d --build
docker compose ps
```

- **http://&lt;PUBLIC_IP&gt;** → redirected to **https://&lt;PUBLIC_IP&gt;**
- The browser shows a **security warning**: the certificate is **self-signed** (generated
  automatically at first start). Click *Advanced → Proceed*.
- The header shows a green **"HTTPS 🔒"** badge, and the same documents as in step 1
  (they live in S3 / DynamoDB, not in the container).
- From the instance: `curl -k https://localhost/health/aws`

Stop everything: `docker compose down`.

> In production you would use a real domain and a trusted certificate
> (Let's Encrypt, or AWS Certificate Manager on a Load Balancer / CloudFront).

---

## Troubleshooting

The app shows a red **"Cannot reach AWS"** page, and `/health/aws` tells what is wrong:

| Message | Fix |
|---|---|
| `S3_BUCKET and DYNAMODB_TABLE ... must be set` | `.env` missing or not passed (`--env-file .env`) |
| `No AWS credentials found` | Attach the IAM role to the instance **and** set hop limit to 2 |
| `AccessDenied` / `AccessDeniedException` | Inline policy missing, or `BUCKET_NAME` not replaced |
| `NoSuchBucket` / `404` on S3 | Bucket name typo, or bucket in another region |
| `ResourceNotFoundException` | Table name typo, or table in another region |
| `ValidationException` | Table partition key must be `id` (String) |

After changing the role or the hop limit, just retry — no restart needed.

---

## Clean up (avoid costs)

1. EC2: **Terminate** the instance
2. S3: **Empty** then **Delete** the bucket
3. DynamoDB: **Delete** the table
4. IAM: delete the role `exam-analyzer-ec2-role`

---

## Run locally (developers)

Uses your own AWS credentials (`aws configure` or `AWS_PROFILE`):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export AWS_REGION=eu-central-1 S3_BUCKET=... DYNAMODB_TABLE=exam-documents
uvicorn app.main:app --reload       # http://localhost:8000
```

## Configuration

| Variable         | Default        | Meaning                        |
|------------------|----------------|--------------------------------|
| `S3_BUCKET`      | — (required)   | Bucket for the uploaded files  |
| `DYNAMODB_TABLE` | — (required)   | Table for the metadata (key `id`, String) |
| `AWS_REGION`     | `eu-central-1` | Region of the bucket and table |
| `S3_PREFIX`      | `uploads/`     | Key prefix inside the bucket   |
| `MAX_UPLOAD_MB`  | `20`           | Max upload size                |

Allowed file types: `.pdf .docx .txt .png .jpg .jpeg`

## Roadmap

- [x] v0.1 standalone (SQLite + local files)
- [x] v0.2 files in S3, metadata in DynamoDB
- [ ] Real analysis pipeline (`analyze_document` in `app/main.py`)
- [ ] Asynchronous processing (S3 event → queue → worker / Lambda)
- [ ] Deployment on ECS / Fargate
