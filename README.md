# Exam Analyzer

A small web application to upload **exam documents** and view their **analysis results**.

- **v0.3 (current):** documents analysed by **Claude (Sonnet) on Amazon Bedrock**, asynchronously:
  S3 → SQS → Lambda → Bedrock → DynamoDB
- **v0.2:** documents in **Amazon S3**, metadata in **Amazon DynamoDB**: `git checkout v0.2.0`
- **v0.1:** standalone version (local SQLite + local files): `git checkout v0.1.0`

```
                       ┌──────────────── EC2 instance (IAM role) ────────────────┐
Browser ──HTTP/HTTPS──▶│ [ nginx ] ──▶ [ app container ] ──boto3──┐               │
                       └──────────────────────────────────────────┼───────────────┘
                                   ① put file    ┌────────────────┴───────────┐ ② item status = "queued"
                                                 ▼                            ▼
                                 S3 bucket: uploads/<id>.pdf      DynamoDB table: metadata + analysis
                                                 │ ③ event notification       ▲
                                                 ▼                            │ ⑥ status = "done" + result
                                        SQS queue (+ dead-letter queue)       │
                                                 │ ④ trigger                  │
                                                 ▼                            │
                                        Lambda "exam-processor" ──────────────┘
                                                 │ ⑤ PDF + prompt + JSON schema
                                                 ▼
                                     Amazon Bedrock: Claude Sonnet
```

The web app never waits for the AI: it stores the file, answers immediately, and the
document page refreshes until the Lambda has written the result.

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
If your table is in another region or has another name, also fix the DynamoDB ARN.

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

## Step 3 — AI analysis: S3 → SQS → Lambda → Bedrock

Everything below is in the **same region** as your bucket and table.
Replace `<name>`, `REGION`, `ACCOUNT_ID`, `BUCKET_NAME`, `TABLE_NAME` with your values.

### 3.1 Enable the model in Amazon Bedrock
**Amazon Bedrock → Model catalog → Claude Sonnet 5.5** (`anthropic.claude-sonnet-5-5`).
The first time an account uses an Anthropic model, Bedrock asks for a short
**use-case form**: fill it in, then test the model once in the **Playground** with
any prompt. Access can take a few minutes to become active.

> No access yet? `anthropic.claude-sonnet-5` is open to all accounts: set it as
> `MODEL_ID` in step 3.5 meanwhile.

### 3.2 SQS queues
**SQS → Create queue** (type **Standard**), twice:

1. Dead-letter queue: name `exam-analyzer-dlq-<name>`, defaults → **Create**
2. Main queue: name `exam-analyzer-jobs-<name>`
   - **Visibility timeout: 5 minutes** (must be ≥ the Lambda timeout)
   - **Access policy → Advanced:** replace with [`lambda/sqs-access-policy.json`](lambda/sqs-access-policy.json)
     (fill in `REGION`, `ACCOUNT_ID`, `QUEUE_NAME`, `BUCKET_NAME`): it allows **only your
     bucket** to send messages
   - **Dead-letter queue: Enabled** → `exam-analyzer-dlq-<name>`, **Maximum receives: 3**
   - **Create queue**

### 3.3 S3 event notification
**S3 → your bucket → Properties → Event notifications → Create event notification**
- Name `new-upload`, **Prefix** `uploads/`
- Event types: **All object create events**
- Destination: **SQS queue** → `exam-analyzer-jobs-<name>` → **Save**

(If saving fails with *Unable to validate destination configurations*, the queue access
policy is wrong.) S3 sends a test message to the queue; the Lambda ignores it.

### 3.4 IAM role for the Lambda
**IAM → Roles → Create role** → trusted entity **AWS service → Lambda**
1. Add the managed policy **`AWSLambdaSQSQueueExecutionRole`** (read the queue + write logs)
2. Name it `exam-processor-lambda-role` → **Create role**
3. Open it → **Add permissions → Create inline policy → JSON**: paste
   [`lambda/lambda-policy.json`](lambda/lambda-policy.json), fill in `BUCKET_NAME`, `REGION`,
   `ACCOUNT_ID`, `TABLE_NAME` → name it `exam-processor-access`

It can **read** the uploaded files, **update** items, and call Claude on Bedrock
(`bedrock-mantle:CreateInference`). It cannot delete anything.

### 3.5 Lambda layer + function
The function uses the **Anthropic Python SDK**, which is not in the Lambda runtime, so it
is added as a **layer**.

1. Get `anthropic-layer.zip` (≈ 25 MB): download it from the
   [GitHub release](https://github.com/yassine-kr/exam-analyzer/releases/tag/v0.3.0), or build it on
   your EC2 instance with `sudo apt install -y python3-pip zip && bash lambda/build-layer.sh`
2. **Lambda → Layers → Create layer**: name `anthropic-sdk`, upload the zip,
   compatible runtime **Python 3.12**, architecture **x86_64**
3. **Lambda → Functions → Create function**
   - Name `exam-processor-<name>`, runtime **Python 3.12**, architecture **x86_64**
   - **Execution role → Use an existing role** → `exam-processor-lambda-role`
4. In the function:
   - **Code:** replace the content of `lambda_function.py` with
     [`lambda/lambda_function.py`](lambda/lambda_function.py) → **Deploy**
   - **Code → Layers → Add a layer → Custom layers** → `anthropic-sdk`, version 1
   - **Configuration → General configuration:** timeout **3 min**, memory **512 MB**
   - **Configuration → Environment variables:**

     | Key | Value |
     |---|---|
     | `DYNAMODB_TABLE` | your table name |
     | `MODEL_ID` | `anthropic.claude-sonnet-5-5` |

   - **Add trigger → SQS** → `exam-analyzer-jobs-<name>`, **batch size 1** → **Add**

### 3.6 Test
1. Upload the exam PDF in the web app: status **queued** → **processing** → **done**
   (≈ 30–90 s; the page refreshes by itself), then the analysis appears: summary,
   questions, Bloom level, topic coverage, issues and suggestions.
2. **DynamoDB → Explore items**: the item now has `analysis_json`, `model_id`,
   `input_tokens`, `output_tokens`, `processed_at`.
3. **Lambda → Monitor → View CloudWatch logs**: one log stream per execution.

| Status | Meaning |
|---|---|
| `queued` | stored, waiting for the Lambda |
| `processing` | Lambda is calling Claude |
| `retrying` | temporary error (throttling…): SQS will deliver the message again |
| `done` | analysis saved |
| `failed` | permanent error, message shown on the page (no access to the model, bad file…) |

A document stuck in `queued` means the event never reached the Lambda: check the S3
event notification, the trigger, and the messages in the queue / dead-letter queue.

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

Analysis problems (shown on the document page, details in the Lambda's CloudWatch logs):

| Symptom | Fix |
|---|---|
| Stays `queued` | No S3 event notification / no SQS trigger on the Lambda / wrong prefix (`uploads/`) |
| `failed: Bedrock error 403` | Model access not granted (step 3.1), or `bedrock-mantle:CreateInference` missing |
| `failed: Bedrock error 404` | Wrong `MODEL_ID`, or the model is not offered in your region |
| Lambda log: `AccessDenied ... s3:GetObject` / `dynamodb:UpdateItem` | Placeholders not replaced in `lambda-policy.json` |
| Lambda log: `No module named 'anthropic'` | Layer not added to the function |
| Lambda log: `Task timed out` | Increase the timeout (3 min) and keep the queue visibility timeout above it |

---

## Clean up (avoid costs)

1. EC2: **Terminate** the instance
2. Lambda: delete the function and the `anthropic-sdk` layer
3. SQS: delete both queues
4. S3: **Empty** then **Delete** the bucket
5. DynamoDB: **Delete** the table
6. IAM: delete the roles `exam-analyzer-ec2-role` and `exam-processor-lambda-role`

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
- [x] v0.3 asynchronous analysis: S3 event → SQS → Lambda → Claude on Bedrock
- [ ] Deployment on ECS / Fargate
