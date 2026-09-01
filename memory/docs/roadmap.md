# Jarvis-Inspired Local AI Operating System — Detailed Roadmap

## Vision

This project is inspired by the idea of persistent assistants like JARVIS and operator-style AI systems seen in fiction, but grounded in practical, self-hosted engineering.

The goal is not to build AGI or a fictional omniscient entity.

The goal is to build a:

* persistent local AI assistant
* memory-aware system
* infrastructure-aware operator
* private AI layer for daily life
* long-term personal knowledge system
* automation-capable assistant

that runs entirely on local hardware.

The system should eventually:

* remember conversations
* understand projects and goals
* track infrastructure state
* retrieve historical context naturally
* assist with development workflows
* automate operational tasks
* support voice interactions
* evolve into a persistent AI operating layer

This is not being built merely as a chatbot.

It is being built as a long-term personal AI infrastructure project.

---

# Why Build This?

## 1. Privacy and Ownership

Most AI assistants today:

* depend on cloud APIs
* store data externally
* have no true persistent memory ownership
* cannot deeply integrate with private infrastructure

This system is designed to be:

* local-first
* self-hosted
* privacy-preserving
* fully controllable

All conversations, memories, embeddings, workflows, and infrastructure awareness remain on local hardware.

---

## 2. Persistent Long-Term Memory

Most chatbots are stateless.

Even modern assistants usually:

* lose historical context
* forget long-term goals
* cannot maintain continuity over years
* lack semantic memory retrieval

This project aims to solve that using:

* structured markdown memory
* semantic retrieval
* vector search
* metadata indexing
* memory summarization

The assistant should eventually behave more like a persistent operating companion rather than a temporary chatbot session.

---

## 3. Infrastructure Awareness

The assistant should understand:

* Proxmox infrastructure
* VMs and LXCs
* Docker containers
* GPU workloads
* networking
* logs
* metrics
* automation workflows

This transforms the assistant from:

```text
Question Answering AI
```

into:

```text
Operational Intelligence Layer
```

---

## 4. Human-Readable Memory

A major design principle of this architecture is:

```text
Markdown files are the canonical source of truth.
```

This ensures:

* memories remain editable
* memories remain portable
* memories remain inspectable
* future migrations remain possible
* the system avoids opaque vendor lock-in

Instead of storing important long-term memory only inside embeddings or hidden databases, the memory remains:

* versionable
* understandable
* durable

---

## 5. Separation of Responsibilities

The architecture intentionally separates:

| Layer              | Responsibility                          |
| ------------------ | --------------------------------------- |
| Telegram / Discord | user-facing interface (cloud-hosted)    |
| OpenClaw           | orchestration, bot handler, workflows   |
| FastAPI            | middleware and abstraction              |
| Qdrant             | semantic retrieval                      |
| PostgreSQL         | structured metadata                     |
| llama.cpp          | inference                               |
| Markdown Files     | canonical memory                        |

> **What is OpenClaw?** OpenClaw is the custom-built AI orchestration system at the core of this project. It is not an existing open-source tool — it must be designed and developed as part of this roadmap. It is the primary application that manages agents, workflows, memory writing, and personality persistence. A dedicated development phase (Phase 0) must precede its deployment.

This separation keeps the system:

* modular
* debuggable
* replaceable
* scalable
* maintainable

---

> **Important:** OpenClaw is the custom software being built by this project. The roadmap includes a deployment phase (Phase 4), but this presupposes that OpenClaw has already been developed. A Phase 0 — Core OpenClaw Development — must be completed before Phase 4 can proceed.

---

# Final Architecture

## Core Request/Response Flow (Text)

```text
┌──────────────────────────────────────────────────────────────┐
│            USER  (Telegram or Discord mobile/desktop)         │
└───────────────────┬──────────────────────────────────────────┘
                    │ message sent via Telegram / Discord app
                    ▼
┌──────────────────────────────────────────────────────────────┐
│         TELEGRAM BOT API  /  DISCORD GATEWAY  (cloud)         │
│                                                               │
│  Telegram: api.telegram.org  (long-poll or webhook)          │
│  Discord:  gateway.discord.gg  (WebSocket)                   │
└───────────────────┬──────────────────────────────────────────┘
                    │ outbound HTTPS delivery to OpenClaw VM
                    │ (OpenClaw polls Telegram or receives webhook)
                    ▼
┌──────────────────────────────────────────────────────────────┐
│                  OPENCLAW VM  (192.0.2.5 )                    │
│                                                               │
│  ┌────────────────────────────────────────────────────┐     │
│  │             Bot Gateway Layer                        │     │
│  │  Telegram handler (aiogram / python-telegram-bot)  │     │
│  │  Discord handler  (discord.py)                     │     │
│  │  Authenticates sender by Telegram/Discord user ID  │     │
│  └──────────────────────┬─────────────────────────────┘     │
│                          │ verified message                   │
│                          ▼                                    │
│  ┌──────────────┐   ┌─────────────┐  ┌──────────────┐      │
│  │  Agent Loop  │──▶│   Memory    │  │  Workflow    │      │
│  │  (LangGraph/ │   │   Writer    │  │  Executor    │      │
│  │  custom)     │   │  (watchdog) │  │              │      │
│  └──────┬───────┘   └──────┬──────┘  └──────┬───────┘      │
│         │                  │                 │               │
│         └──────────────────┼─────────────────┘               │
│                            │                                  │
│                    /memory/ filesystem                        │
│          (markdown source of truth on disk)                   │
└──────────────────────────┬───────────────────────────────────┘
                           │ HTTP :8080  (internal VLAN only)
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                   FASTAPI LXC  (192.0.2.4 )                   │
│                                                               │
│  POST /memory/search    POST /memory/store                   │
│  POST /memory/update    POST /llm/infer                      │
│  GET  /infra/status     POST /embed                          │
│                                                               │
│  RAG Pipeline · JWT Auth · Embedding Proxy                   │
└──────┬──────────────────────────────────┬────────────────────┘
       │                                  │
       │ HTTP :6333 / :5432 / :6379       │ HTTP :8080 / :8081
       ▼                                  ▼
┌────────────────────────┐   ┌────────────────────────────────┐
│  MEMORY LXC             │   │  AI VM  (192.0.2.2 )           │
│  (192.0.2.3 )           │   │                                │
│                         │   │  llama-server  :8080           │
│  Qdrant      :6333      │   │  (OpenAI-compatible API)       │
│  PostgreSQL  :5432      │   │                                │
│  Redis       :6379      │   │  Embedding model :8081         │
│                         │   │  (nomic-embed-text-v1.5)       │
│  AOF enabled on Redis   │   │                                │
└────────────────────────┘   │  NVIDIA A5000 (24GB VRAM)      │
                              │  CUDA 12.x · llama.cpp         │
                              └────────────────────────────────┘
```

> **Why the LLM has no direct line to Qdrant:** The LLM (llama-server) is a stateless token predictor — it does not make HTTP calls. It only reads the text in its context window. The RAG pattern works by having FastAPI retrieve relevant memory chunks from Qdrant and inject them as text into the prompt *before* the LLM ever sees the request. The LLM responds to a prompt that already contains the relevant context. There is no need for a runtime connection from the AI VM to Qdrant. See the [Request flow](architecture/request-flow.md) section for full detail.

> **Telegram/Discord security:** The OpenClaw VM needs outbound internet access *only* to `api.telegram.org` (port 443) and/or Discord's API (`discord.com`, port 443). All other outbound traffic from the OpenClaw VM must be blocked by OPNsense. Incoming messages arrive via long-polling or webhook — no inbound internet port needs to be opened on the OpenClaw VM for Telegram (long-poll is purely outbound). For Discord, the bot uses a persistent outbound WebSocket — also no inbound port required.

## Full System — All Services at a Glance

```text
┌──────────────────────────── Proxmox Host ──────────────────────────────────┐
│                                                                              │
│  ┌───────────────────────┐   ┌─────────────────────────────────────────┐  │
│  │  AI VM  192.0.2.2     │   │  OpenClaw VM  192.0.2.5                 │  │
│  │                       │   │                                         │  │
│  │  llama-server  :8080  │   │  Bot Gateway (Telegram + Discord)       │  │
│  │  embed model   :8081  │   │  OpenClaw agent loop (LangGraph)        │  │
│  │  DCGM exporter :9400  │   │  Memory writer + watchdog               │  │
│  │  NVIDIA A5000         │   │  Workflow executor                      │  │
│  │                       │   │  /memory/ filesystem                    │  │
│  └───────────────────────┘   └─────────────────────────────────────────┘  │
│                                                                              │
│  ┌───────────────────────┐   ┌─────────────────────────────────────────┐  │
│  │  Memory LXC 192.0.2.3 │   │  FastAPI LXC  192.0.2.4                 │  │
│  │                       │   │                                         │  │
│  │  Qdrant      :6333    │   │  FastAPI (Uvicorn)  :8080               │  │
│  │  PostgreSQL  :5432    │   │  RAG pipeline · JWT auth                │  │
│  │  Redis       :6379    │   │  Embedding proxy                        │  │
│  └───────────────────────┘   └─────────────────────────────────────────┘  │
│                                                                              │
│  ┌───────────────────────┐   ┌─────────────────────────────────────────┐  │
│  │  Monitoring LXC       │   │  OPNsense (gateway / firewall)          │  │
│  │  192.0.2.6            │   │                                         │  │
│  │                       │   │  VLAN routing · Firewall rules          │  │
│  │  Grafana       :3000  │   │  DNS (Unbound) · NGINX reverse proxy    │  │
│  │  Prometheus    :9090  │   │  Outbound allowlist:                    │  │
│  │  node_exporter :9100  │   │    api.telegram.org :443                │  │
│  │  PVE exporter  :9221  │   │    discord.com      :443                │  │
│  └───────────────────────┘   └─────────────────────────────────────────┘  │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘

  ┌────────────────────────────────────────────────────────┐
  │             EXTERNAL  (internet — outbound only)        │
  │                                                         │
  │   Telegram Bot API  api.telegram.org  :443              │
  │   Discord Gateway   gateway.discord.gg :443             │
  │                                                         │
  │  ← OpenClaw VM polls/connects outbound only            │
  │    No inbound internet ports required                  │
  └────────────────────────────────────────────────────────┘
```

## Future Voice-Enabled Architecture

```text
┌────────────────────────────────────────────────────────────────┐
│                        USER (microphone)                        │
└────────────────────────────┬───────────────────────────────────┘
                             │ raw audio stream (WebSocket / WebRTC)
                             ▼
┌────────────────────────────────────────────────────────────────┐
│               WHISPER STT  (AI VM — faster-whisper)             │
│                     streaming transcription                      │
└────────────────────────────┬───────────────────────────────────┘
                             │ text transcript (partial + final)
                             ▼
┌────────────────────────────────────────────────────────────────┐
│                       OPENCLAW VM                               │
│             wake-word detection → agent loop                     │
└──────────────┬─────────────────────────────────────────────────┘
               │ POST /memory/search  +  POST /llm/infer
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                        FASTAPI LXC                               │
│          RAG context injection → streaming LLM call              │
└──────────────┬──────────────────────────────────────────────────┘
               │ Server-Sent Events / token stream
               ▼
┌─────────────────────────────────────────────────────────────────┐
│             llama-server (AI VM) — streaming tokens              │
│         sentence boundary detection in FastAPI layer             │
└──────────────┬──────────────────────────────────────────────────┘
               │ sentence chunks as they complete
               ▼
┌─────────────────────────────────────────────────────────────────┐
│               PIPER TTS  (AI VM or dedicated Whisper VM)         │
│     per-sentence synthesis → audio chunks sent back to client    │
└──────────────┬──────────────────────────────────────────────────┘
               │ audio stream (WAV chunks / WebSocket)
               ▼
                          USER SPEAKER
```

> **Streaming Strategy:** The TTS step must not wait for the full LLM response. FastAPI detects sentence boundaries (`.`, `?`, `!`) in the token stream and dispatches each completed sentence to Piper immediately. This reduces time-to-first-audio to roughly one sentence latency rather than full-response latency.

---

# Infrastructure Layout

## Physical Infrastructure

### Primary Host Machine

The entire platform will run on a dedicated workstation/server configured as the central AI and homelab node.

Primary responsibilities of the host:

* virtualization
* GPU passthrough
* storage management
* network segmentation
* service isolation
* orchestration foundation

The host itself should remain minimal.

Avoid installing AI workloads directly on the Proxmox host.

---

# Hypervisor Layer

## Proxmox

The system uses Proxmox as the virtualization platform.

### Why Proxmox?

Because it provides:

* lightweight LXC containers
* full VMs for GPU workloads
* snapshots
* backups
* VLAN-aware networking
* PCI passthrough
* isolated infrastructure management
* resource control

The architecture intentionally separates:

* inference workloads
* memory infrastructure
* orchestration
* agent systems

into isolated services.

---

# Recommended VM and LXC Layout

| Instance       | Type    | Purpose                  |
| -------------- | ------- | ------------------------ |
| AI VM          | Full VM | GPU inference            |
| OpenClaw VM    | Full VM | orchestration and agents |
| Memory LXC     | LXC     | Qdrant/Postgres/Redis    |
| FastAPI LXC    | LXC     | middleware APIs          |
| Monitoring LXC | LXC     | Grafana/Prometheus       |

---

# Recommended Resource Allocation

## AI VM

| Resource | Recommendation | Notes                                              |
| -------- | -------------- | -------------------------------------------------- |
| CPU      | 16 vCPU        | Model loading and tokenization benefit from cores  |
| RAM      | 64GB           | llama.cpp loads model weights into RAM before GPU  |
| GPU      | NVIDIA A5000   | 24GB VRAM — see constraint table below             |
| Disk     | 1TB NVMe SSD   | Models are large: Qwen2.5-14B ~9GB, reserve space |

Purpose:

* local LLM inference (`llama-server`)
* embedding generation
* future STT (faster-whisper) and TTS (Piper)

This VM should remain inference-focused only.

> **VRAM Constraint:** The NVIDIA A5000 has 24GB VRAM. A 70B parameter model at 4-bit quantization requires approximately 35–40GB VRAM and cannot fit. A 32B model at 4-bit quantization requires ~20GB and is borderline. Practical model choices at 4-bit are 7B (~4GB), 13B (~8GB), and up to 30B (~18GB). Select a Qwen model version (e.g., Qwen2.5-14B or Qwen2.5-32B-Q4) based on this constraint. Document your chosen model and quantization level explicitly before Phase 2.

---

## OpenClaw VM

| Resource | Recommendation | Notes                                              |
| -------- | -------------- | -------------------------------------------------- |
| CPU      | 8 vCPU         | Agent loop is single-threaded but workflow concurrency benefits from cores |
| RAM      | 16–24GB        | 16GB minimum; 24GB if running heavy workflow loads |
| Disk     | 200GB NVMe SSD | Memory filesystem grows over time; SSD preferred for watchdog I/O |

Purpose:

* OpenClaw application (agent loop, memory writer, workflow executor)
* `/memory/` filesystem (all markdown memory files)
* Python watchdog process monitoring `/memory/`

---

## Memory LXC

| Resource | Recommendation     | Notes                                           |
| -------- | ------------------ | ----------------------------------------------- |
| CPU      | 4 cores            | Qdrant and PostgreSQL are CPU-light at this scale |
| RAM      | 8–16GB             | Qdrant loads index into RAM; 16GB recommended   |
| Disk     | 200GB NVMe SSD     | Fast random I/O required for Qdrant + Postgres  |

Purpose:

* vector retrieval (Qdrant)
* metadata storage (PostgreSQL)
* retry queue and cache (Redis)

> Allocate at least 200GB. Qdrant vector storage grows proportionally with memory corpus size. At 1024-dim float32 vectors, 1 million chunks ≈ 4GB vector data alone (before payload overhead).

---

## FastAPI LXC

| Resource | Recommendation | Notes                                           |
| -------- | -------------- | ----------------------------------------------- |
| CPU      | 4 cores        | Handles async HTTP routing only                 |
| RAM      | 8GB            | Stateless; 8GB is comfortable                   |
| Disk     | 40GB           | OS + app only; no persistent data stored here   |

Purpose:

* middleware
* JWT authentication and rate limiting
* RAG pipeline coordination
* proxying embedding and inference requests to AI VM

> FastAPI LXC is intentionally lightweight and stateless. It must not store embeddings, model weights, or persistent data. It is safe to recreate without data loss.

---

# Networking Architecture

## Internal AI VLAN

Create a dedicated internal VLAN/subnet for AI workloads.

### Service IP and Port Map

| Service                | IP           | Port(s)           | Protocol | Notes                                |
| ---------------------- | ------------ | ----------------- | -------- | ------------------------------------ |
| AI VM — llama-server   | 192.0.2.2   | 8080              | HTTP     | OpenAI-compatible `/v1/chat`         |
| AI VM — embed model    | 192.0.2.2   | 8081              | HTTP     | `/embed` endpoint                    |
| AI VM — DCGM exporter  | 192.0.2.2   | 9400              | HTTP     | Prometheus scrape target             |
| Memory LXC — Qdrant    | 192.0.2.3   | 6333 (HTTP)       | HTTP     | REST + gRPC :6334                    |
| Memory LXC — Postgres  | 192.0.2.3   | 5432              | TCP      | Internal only, never exposed         |
| Memory LXC — Redis     | 192.0.2.3   | 6379              | TCP      | Internal only, AOF enabled           |
| FastAPI LXC            | 192.0.2.4   | 8080              | HTTP     | Uvicorn, internal VLAN only          |
| OpenClaw VM            | 192.0.2.5   | 8000              | HTTP     | Exposed via reverse proxy            |
| Monitoring LXC — Grafana    | 192.0.2.6 | 3000            | HTTP     | Dashboard                            |
| Monitoring LXC — Prometheus | 192.0.2.6 | 9090            | HTTP     | Metrics scraping                     |
| Reverse Proxy (NGINX)  | gateway IP   | 443 / 80          | HTTPS    | Only public-facing entry point       |

This VLAN should:

* remain internal-only
* not expose databases publicly
* isolate AI services from general LAN traffic

---

# OPNsense Configuration

Use OPNsense as the firewall and routing layer.

Responsibilities:

* VLAN management
* firewall segmentation
* reverse proxy routing
* traffic isolation
* DNS handling
* internal service protection

---

# Firewall Philosophy

## Allow Rules (OPNsense — explicit allowlist)

| Source              | Destination                   | Port(s) | Purpose                                      |
| ------------------- | ----------------------------- | ------- | -------------------------------------------- |
| User VLAN           | Reverse Proxy                 | 443     | HTTPS access to Grafana / admin dashboards   |
| Reverse Proxy       | Monitoring LXC :3000          | 3000    | Grafana dashboard                            |
| OpenClaw VM         | FastAPI LXC :8080             | 8080    | Orchestration → middleware                   |
| OpenClaw VM         | Memory LXC :6379              | 6379    | Retry queue access for memory writer         |
| **OpenClaw VM**     | **api.telegram.org :443**     | **443** | **Telegram Bot API (long-poll / webhook)**   |
| **OpenClaw VM**     | **discord.com :443**          | **443** | **Discord bot WebSocket gateway**            |
| FastAPI LXC         | AI VM :8080                   | 8080    | LLM inference requests                       |
| FastAPI LXC         | AI VM :8081                   | 8081    | Embedding generation requests                |
| FastAPI LXC         | Memory LXC :6333              | 6333    | Qdrant vector search                         |
| FastAPI LXC         | Memory LXC :5432              | 5432    | PostgreSQL metadata reads/writes             |
| FastAPI LXC         | Memory LXC :6379              | 6379    | Redis cache / retry queue                    |
| Monitoring LXC      | All VMs/LXCs                  | 9100    | node_exporter Prometheus scrape              |
| Monitoring LXC      | AI VM :9400                   | 9400    | DCGM GPU exporter Prometheus scrape          |
| Monitoring LXC      | Proxmox host API              | 8006    | Proxmox metrics via API                      |

> **Telegram long-poll vs webhook:** Long-polling (OpenClaw continuously calls `getUpdates`) requires only outbound internet. A webhook (Telegram pushes messages to a URL you expose) requires an inbound HTTPS endpoint — which means punching a hole in the firewall or using a reverse proxy with a public domain. **Recommend long-polling** to avoid any inbound exposure.

> **Discord:** Uses a persistent outbound WebSocket to `gateway.discord.gg`. No inbound port needed.

## Deny Rules (default-deny for everything else)

| Source              | Destination                          | Reason                                          |
| ------------------- | ------------------------------------ | ----------------------------------------------- |
| Internet            | Qdrant :6333                         | Vector DB must never be public                  |
| Internet            | PostgreSQL :5432                     | Database must never be public                   |
| Internet            | Redis :6379                          | Cache/queue must never be public                |
| Internet            | AI VM (any port)                     | Inference endpoint is internal only             |
| Internet            | OpenClaw VM (any inbound port)       | Bot uses outbound polling, no inbound needed    |
| OpenClaw VM         | any internet except Telegram/Discord | Block all other outbound from OpenClaw VM       |
| Any                 | Proxmox host (direct)                | Host management restricted to admin VLAN only   |

Only Grafana (via reverse proxy on the internal network) is web-accessible. The primary user interface (Telegram/Discord) requires **zero inbound internet ports**.

Deploy a reverse proxy using:

* NGINX
  or
* Traefik

Recommended responsibilities:

* HTTPS termination
* authentication
* internal routing
* rate limiting
* API protection

Example exposed services:

```text
https://ai.local
https://memory.local
https://openclaw.local
```

---

# GPU Passthrough Setup

## Goal

Pass the NVIDIA A5000 directly into the AI VM.

This provides:

* near-native inference performance
* isolated CUDA environment
* stable GPU workloads
* clean driver separation

---

# Recommended GPU Workflow

## On Proxmox Host

Enable:

```text
IOMMU
VFIO
PCI passthrough
```

Blacklist host NVIDIA drivers.

Bind GPU to VFIO.

---

## On AI VM

Install:

* NVIDIA drivers
* CUDA toolkit
* llama.cpp CUDA build
* Run llama.cpp in server mode (`llama-server`) to expose an OpenAI-compatible HTTP API on the internal VLAN (default port 8080)

Verify using:

```bash
nvidia-smi
```

> **GPU Exclusivity:** Once the GPU is passed through to the AI VM via VFIO, the Proxmox host and all other VMs/LXCs lose access to it entirely. The GPU cannot be shared across VMs simultaneously. Plan accordingly — all GPU workloads (inference, embeddings, future Whisper STT) must run inside this single AI VM.

---

# Storage Design

## Recommended Layout

### NVMe SSD

Use for:

* models
* vector database
* PostgreSQL
* embeddings
* active memory

### HDD / Archive Storage

Use for:

* backups
* archived chats
* voice recordings
* logs
* historical memory

---

# Backup Strategy

## Critical Data

Back up:

* markdown memory files
* PostgreSQL
* Qdrant snapshots
* OpenClaw configs
* FastAPI configs
* infrastructure configs

---

# Recommended Backup Layers

## Daily

* markdown memory repo
* database dumps

## Weekly

* Proxmox snapshots
* VM backups

## Monthly

* offline archive export

---

# Monitoring Stack

Deploy a dedicated Monitoring LXC (192.0.2.6) for aggregation and dashboards.

## Service Placement

| Service                        | Runs On         | Port  | Notes                                |
| ------------------------------ | --------------- | ----- | ------------------------------------ |
| Grafana                        | Monitoring LXC  | 3000  | Dashboards                           |
| Prometheus                     | Monitoring LXC  | 9090  | Scrapes all exporters                |
| node_exporter                  | Every VM/LXC    | 9100  | CPU, RAM, disk, network per host     |
| NVIDIA DCGM Exporter           | AI VM only      | 9400  | GPU VRAM, utilization, temperature   |
| Proxmox PVE Exporter           | Monitoring LXC  | 9221  | Scrapes Proxmox API                  |

> **Important:** The NVIDIA DCGM Exporter must run on the AI VM (where the GPU is physically present). Prometheus in the Monitoring LXC scrapes it remotely at `http://192.0.2.2:9400/metrics`. Placing it in the Monitoring LXC would yield no GPU data.

## Prometheus Scrape Config Example

```yaml
scrape_configs:
  - job_name: node
    static_configs:
      - targets:
          - 192.0.2.2:9100   # AI VM
          - 192.0.2.3:9100   # Memory LXC
          - 192.0.2.4:9100   # FastAPI LXC
          - 192.0.2.5:9100   # OpenClaw VM

  - job_name: nvidia_gpu
    static_configs:
      - targets:
          - 192.0.2.2:9400   # AI VM DCGM exporter

  - job_name: proxmox
    static_configs:
      - targets:
          - 192.0.2.6:9221   # PVE exporter (queries Proxmox API)
```

## Key Metrics to Dashboard

| Metric                      | Source                  | Alert Threshold          |
| --------------------------- | ----------------------- | ------------------------ |
| GPU VRAM used               | DCGM Exporter           | > 90% — reduce parallel  |
| GPU utilization %           | DCGM Exporter           | Sustained 100% → bottleneck |
| GPU temperature             | DCGM Exporter           | > 80°C → check cooling   |
| llama-server inference p99  | custom exporter or logs | > 30s → investigate      |
| Qdrant collection size      | Qdrant metrics          | Growth rate tracking     |
| PostgreSQL connections      | node_exporter           | Watch for connection leaks |
| Redis memory used           | Redis INFO              | > 80% → review TTLs      |
| OpenClaw VM RAM             | node_exporter           | > 90% → OOM risk         |

---

# Internal Service Communication

## Communication Philosophy

All services should communicate over:

```text
Internal IPs only
```

Avoid:

* public database exposure
* public inference endpoints
* public vector DB access

---

# Containerization Strategy

## Prefer Docker Compose Inside LXCs

For:

* Qdrant
* PostgreSQL
* Redis
* Grafana
* FastAPI

Benefits:

* easy upgrades
* portability
* reproducibility
* easier backups

---

# Infrastructure Growth Strategy

The architecture is intentionally modular.

Future additions may include:

| Future Service | Purpose               |
| -------------- | --------------------- |
| Whisper VM     | speech-to-text        |
| OCR LXC        | document ingestion    |
| Agent Workers  | distributed workflows |
| Event Bus      | automation events     |
| Security Agent | anomaly detection     |
| Mobile Gateway | phone integration     |

The infrastructure should support incremental evolution without redesigning the core architecture.

---

# Component Responsibilities

## OpenClaw VM

Primary entry point and brain of the system.

### Internal Architecture

```text
┌──────────────────────────────────────────────────────────────┐
│                      OPENCLAW APP                             │
│                                                               │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                 Bot Gateway Layer                      │   │
│  │                                                        │   │
│  │  TelegramHandler (aiogram or python-telegram-bot)     │   │
│  │    - long-polls api.telegram.org                      │   │
│  │    - verifies sender by Telegram user ID whitelist    │   │
│  │    - supports text, voice messages, commands          │   │
│  │                                                        │   │
│  │  DiscordHandler (discord.py)                          │   │
│  │    - WebSocket to Discord gateway                     │   │
│  │    - verifies sender by Discord user ID whitelist     │   │
│  │    - listens to DMs or a specific private server      │   │
│  │                                                        │   │
│  │  Both handlers normalize input into a unified         │   │
│  │  Message object before passing to the Agent Loop      │   │
│  └──────────────────────────┬───────────────────────────┘   │
│                              │ normalized Message             │
│                              ▼                                │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                    Agent Loop                          │   │
│  │  receive Message → classify intent                    │   │
│  │  → retrieve memory (conditional)                     │   │
│  │  → build prompt → infer → check for tool calls       │   │
│  │  → execute tools if needed (agentic RAG loop)        │   │
│  │  → respond via same channel (Telegram or Discord)    │   │
│  │  → decide: should I persist this?                    │   │
│  └──────────────────────────┬───────────────────────────┘   │
│                              │                                │
│  ┌───────────────┐  ┌────────▼──────────┐                   │
│  │  Workflow     │  │  Memory Writer    │                   │
│  │  Executor     │  │                   │                   │
│  │               │  │  Formats YAML     │                   │
│  │  Tool calls   │  │  frontmatter md   │                   │
│  │  Approval     │  │  Writes to disk   │                   │
│  │  gating       │  │  watchdog picks   │                   │
│  └───────────────┘  │  up the change    │                   │
│                      └───────────────────┘                   │
│                                                               │
│  ┌──────────────────────────────────────────────────────┐   │
│  │           Personality / Context Layer                  │   │
│  │  rolling conversation buffer (last N turns)           │   │
│  │  per-user session context (keyed by user ID)          │   │
│  │  session goals tracking                               │   │
│  │  user preference state                                │   │
│  └──────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

Responsibilities:

* receive messages from Telegram and/or Discord (outbound polling, no inbound ports)
* authenticate senders via user ID whitelist (only your Telegram/Discord account can drive the system)
* normalize messages from both platforms into a unified internal format
* classify intent: conversational / memory query / tool execution / automation
* manage rolling conversation buffer (last N turns, token-budget aware, per user ID)
* build RAG-augmented prompts and send to FastAPI
* handle agentic RAG: if the LLM returns a `search_memory` tool call, execute it and continue the loop
* write structured markdown memory when conversations yield persistent facts
* execute workflows with human-in-the-loop approval gates (sent back as Telegram/Discord messages)
* maintain personality consistency via persistent system prompt files
* coordinate future voice interaction (STT input → TTS output)
* reply to the correct platform and user (Telegram message → Telegram reply; Discord message → Discord reply)

Memory directory layout on disk:

```text
/memory/
  projects/         ← active and past project states
  goals/            ← short-term and long-term goals
  history/          ← compressed conversation summaries
  reflections/      ← periodic self-generated summaries
  people/           ← context about people the user mentions
  infra/            ← tracked infrastructure state
  preferences/      ← learned user preferences and patterns
```

---

## FastAPI LXC

Acts as middleware, abstraction, and security boundary between OpenClaw and all backend services.

### Internal Request Routing

```text
Incoming request from OpenClaw
         │
         ▼
  JWT validation
  (reject → 401)
         │
         ▼
  Route to handler:

  /memory/search  → embed query → Qdrant search → re-rank → return
  /memory/store   → parse body → write Qdrant + PostgreSQL
  /memory/update  → lookup by file_path → update Qdrant + PostgreSQL
  /llm/infer      → forward to llama-server :8080 (stream passthrough)
  /embed          → forward to embed model :8081
  /infra/status   → query Proxmox API + Docker APIs → aggregate
```

### Authentication

* Service-to-service: short-lived JWT tokens signed with HMAC-SHA256
* Token lifespan: 15 minutes, refreshed by OpenClaw before expiry
* Secrets stored as environment variables (never hardcoded)
* Rate limiting: per-endpoint limits enforced at FastAPI middleware layer (e.g. 60 req/min for `/llm/infer`)

Full API surface:

```text
POST   /memory/search        Search memory by semantic query
POST   /memory/store         Store a new memory chunk
POST   /memory/update        Update existing memory by file_path
DELETE /memory/delete        Delete memory by file_path
POST   /llm/infer            Forward inference request (streaming)
POST   /embed                Generate embeddings via AI VM
GET    /infra/status         Aggregate homelab status
GET    /infra/vm/{id}        Status of a specific Proxmox VM
GET    /health               Liveness check
```

---

## Memory LXC

### Services

* Qdrant
* PostgreSQL
* Redis

### Qdrant

Semantic retrieval engine running as a Docker container.

Used for:

* contextual search across all memory chunks
* fuzzy memory retrieval by semantic similarity
* long-term recall of past conversations and decisions

Qdrant collection schema:

```text
Collection: "memory"

Point {
  id:        sha256(file_path + chunk_index)
  vector:    float32[1024]  (or 768, match embed model dims)
  payload: {
    file_path:   string,
    chunk_index: int,
    chunk_text:  string,
    type:        string,    (project / goal / history / reflection / ...)
    tags:        string[],
    priority:    string,    (high / medium / low)
    updated_at:  timestamp
  }
}
```

### PostgreSQL

Structured metadata layer running as a Docker container.

Full schema:

```sql
-- memory_chunks: one row per indexed chunk
CREATE TABLE memory_chunks (
    chunk_id     TEXT PRIMARY KEY,   -- sha256(file_path + chunk_index)
    file_path    TEXT NOT NULL,
    chunk_index  INTEGER NOT NULL,
    chunk_text   TEXT,
    type         TEXT,
    tags         TEXT[],
    priority     TEXT,
    summary      TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    indexed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- memory_files: one row per markdown file
CREATE TABLE memory_files (
    file_path    TEXT PRIMARY KEY,
    type         TEXT,
    tags         TEXT[],
    priority     TEXT,
    updated_at   TIMESTAMPTZ,
    chunk_count  INTEGER
);

-- entities: extracted named entities for relationship modeling (Phase 8)
CREATE TABLE entities (
    entity_id    SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    type         TEXT,             -- person / project / place / concept
    first_seen   TIMESTAMPTZ,
    last_seen    TIMESTAMPTZ
);

-- entity_mentions: links entities to memory chunks
CREATE TABLE entity_mentions (
    chunk_id     TEXT REFERENCES memory_chunks(chunk_id),
    entity_id    INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (chunk_id, entity_id)
);
```

### Redis

Caching layer and asynchronous retry queue running as a Docker container.

> **Persistence:** Redis must be configured with AOF (Append-Only File) persistence enabled (`appendonly yes`) to survive restarts. Without this, all in-flight retry queues and cache entries are lost on container restart, which breaks the ingestion pipeline's failure recovery mechanism.

Key namespaces:

```text
ingest:retry          List of failed ingestion jobs (LPUSH/RPOP)
session:context:<id>  Rolling conversation context cache (TTL: 24h)
embed:cache:<hash>    Cached embedding vectors (TTL: 7d)
infra:status          Cached infra status snapshot (TTL: 30s)
```

---

## AI VM

Dedicated inference machine. GPU-exclusive. No orchestration logic.

### Services

* `llama-server` — OpenAI-compatible LLM API on `:8080`
* embedding model server — on `:8081` (e.g. `nomic-embed-text-v1.5` via llama.cpp)
* `nvidia-dcgm-exporter` — GPU metrics on `:9400` for Prometheus scraping
* (Phase 7) `faster-whisper` server — STT on `:8082`
* (Phase 7) `piper` TTS server — on `:8083`

### Responsibilities

* LLM inference (all `/v1/chat/completions` calls)
* embedding generation (all `/embed` calls)
* GPU workload management (VRAM budget, concurrent request limits)
* model loading and hot-swap between models (if needed)

### llama-server startup example

```bash
llama-server \
  --model /models/qwen2.5-14b-instruct-q4_k_m.gguf \
  --ctx-size 32768 \
  --n-gpu-layers 999 \
  --host 0.0.0.0 \
  --port 8080 \
  --parallel 2 \
  --cont-batching
```

No orchestration or automation logic should exist on the AI VM. It serves requests. It does not make decisions.

---

# Core Memory Design

## Markdown Files = Source of Truth

OpenClaw writes structured markdown memory. Files are human-readable, version-controllable, and independent of any database.

### Frontmatter Schema

Every memory file must include a YAML frontmatter block:

```yaml
---
type:       <project|goal|history|reflection|person|infra|preference>
priority:   <high|medium|low>
tags:       [list, of, tags]
updated:    YYYY-MM-DD
summary:    "One-sentence summary used in search result snippets"
---
```

### File Examples

**Project memory** (`/memory/projects/local-ai-os.md`):

```markdown
---
type: project
priority: high
tags:
  - ai
  - automation
  - homelab
updated: 2026-05-15
summary: "Building a persistent local AI operating system on Proxmox with Qdrant and llama.cpp."
---

# Local AI Operating System

## Goal
Build a persistent local AI assistant running entirely on local hardware.

## Current Stack
- OpenClaw (orchestration)
- Qdrant (semantic retrieval)
- llama.cpp with Qwen2.5-14B (inference)
- PostgreSQL (metadata)
- Redis (cache + retry queue)

## Current Problems
- memory retrieval latency under high load
- GPU VRAM limits restrict model size to 14B

## Next Steps
- complete Phase 3 memory infrastructure
- benchmark end-to-end retrieval latency
```

**Person memory** (`/memory/people/alice.md`):

```markdown
---
type: person
priority: medium
tags:
  - colleague
  - devops
updated: 2026-05-14
summary: "Alice is a DevOps engineer working on the same homelab project."
---

# Alice

## Role
DevOps engineer, collaborator on homelab automation.

## Notes
- Prefers Ansible over Terraform for infra provisioning
- Working on setting up a separate Kubernetes cluster
```

**Goal memory** (`/memory/goals/short-term.md`):

```markdown
---
type: goal
priority: high
tags:
  - milestone
updated: 2026-05-15
summary: "Complete Phase 3 memory infrastructure by end of May 2026."
---

# Short-Term Goals

## May 2026
- [ ] Deploy Qdrant on Memory LXC
- [ ] Deploy PostgreSQL with full schema
- [ ] Build and test embedding pipeline
- [ ] Validate retrieval quality
```

---

# Why Use a Vector Database?

Markdown files are excellent for:

* storage
* editing
* portability
* versioning
* human readability

But they are poor for:

* semantic retrieval
* contextual search
* fuzzy recall
* natural-language memory search

Qdrant acts as:

```text
Semantic Retrieval Infrastructure
```

NOT:

```text
Canonical Memory Storage
```

The vector database exists to retrieve relevant memories quickly and naturally.

---

# Memory Pipeline

## Write Flow — Full Detail

```text
┌──────────────────────────────────────────────────────────────┐
│                     OPENCLAW VM                               │
│                                                               │
│  Agent decides to persist memory                             │
│  (conversation outcome, project update, reflection, etc.)    │
│         │                                                     │
│         ▼                                                     │
│  Memory Writer formats YAML-frontmatter markdown             │
│  Writes to /memory/<category>/<slug>.md on disk              │
│         │                                                     │
│  ┌──────▼──────────────────────────────────┐                 │
│  │  Python watchdog (inotify-based)         │                 │
│  │  Detects: create / modify / delete       │                 │
│  └──────┬──────────────────────────────────┘                 │
└─────────┼────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────┐
│               INGESTION PIPELINE (runs in OpenClaw VM)        │
│                                                               │
│  1. Parse frontmatter (type, tags, priority, updated)        │
│  2. Chunk markdown body into ≤512-token segments             │
│     (prefer natural boundaries: sections, paragraphs)        │
│  3. For each chunk:                                           │
│     POST http://192.0.2.4 :8080/embed                        │
│       → FastAPI routes to AI VM :8081                        │
│       → Returns float32 vector (e.g. 768-dim or 1024-dim)    │
│  4. Upsert into Qdrant (collection: "memory")                │
│     payload: {file_path, chunk_index, tags, type, updated}  │
│  5. Upsert into PostgreSQL                                   │
│     table: memory_chunks                                     │
│     (chunk_id, file_path, tags[], summary, updated_at)       │
└────────────────────┬───────────────────────────────────┬─────┘
                     │ success                            │ failure
                     ▼                                   ▼
              Both stores updated          Push job to Redis retry queue
              Qdrant + PostgreSQL          (key: "ingest:retry")
              consistent                  Worker retries with backoff
                                          Alerts after 3 failed attempts
```

> **Delete Handling:** When watchdog detects a file deletion, the ingestion pipeline must also delete corresponding Qdrant points and PostgreSQL rows by `file_path`. Without this, orphaned vectors from deleted memories will pollute search results forever.

> **Chunk ID Strategy:** Use `sha256(file_path + chunk_index)` as the Qdrant point ID. This makes upserts idempotent — re-ingesting a modified file updates existing points rather than creating duplicates.

---

## Retrieval Flow — Full Detail

```text
┌──────────────────────────────────────────────────────────────┐
│                     OPENCLAW VM                               │
│                                                               │
│  Agent receives user query                                   │
│  Determines: does this need memory context?                  │
│  (heuristic or always-on RAG)                                │
│         │                                                     │
│         ▼                                                     │
│  POST http://192.0.2.4 :8080/memory/search                   │
│  Body: { "query": "...", "top_k": 5, "filters": {...} }      │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                   FASTAPI LXC :8080                           │
│                                                               │
│  1. Validate JWT token                                       │
│  2. POST http://192.0.2.2 :8081/embed                        │
│     → Embed the user query into a vector                     │
│  3. POST http://192.0.2.3 :6333/collections/memory/points    │
│     /search                                                   │
│     → Qdrant returns top-k {chunk_text, score, payload}      │
│  4. Optional: PostgreSQL lookup for tag-based pre-filtering  │
│     (e.g. filter by type="project" or tags=["infra"])        │
│  5. Re-rank results by recency × score (configurable)        │
│  6. Return: { "chunks": [...], "sources": [...] }            │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                     OPENCLAW VM                               │
│                                                               │
│  Build system prompt:                                        │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ [SYSTEM]                                             │    │
│  │ You are a persistent AI assistant.                   │    │
│  │                                                      │    │
│  │ [MEMORY CONTEXT]                                     │    │
│  │ <injected chunks from retrieval>                     │    │
│  │                                                      │    │
│  │ [CONVERSATION HISTORY]                               │    │
│  │ <recent N turns from rolling buffer>                 │    │
│  │                                                      │    │
│  │ [USER]                                               │    │
│  │ <current user message>                               │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                               │
│  POST http://192.0.2.4 :8080/llm/infer                       │
│  (FastAPI proxies to AI VM llama-server :8080)               │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│               AI VM — llama-server :8080                      │
│                                                               │
│  POST /v1/chat/completions                                   │
│  stream: true  (Server-Sent Events)                          │
│  Tokens stream back through FastAPI → OpenClaw → User        │
└──────────────────────────────────────────────────────────────┘
```

> **Context Window Budget:** With a 32K context window model, allocate approximately: 1K for system prompt, 4K for memory chunks, 4K for conversation history, leaving ~23K for the response. Tune `top_k` and chunk size to stay within budget. OpenClaw must count tokens before sending the request and truncate oldest history first if over budget.

---

## Inference-Only Flow (no memory retrieval)

For simple queries that do not require memory lookup (e.g. math, code generation):

```text
OpenClaw VM
    │  POST /llm/infer  (skip /memory/search)
    ▼
FastAPI LXC :8080
    │  POST /v1/chat/completions
    ▼
AI VM llama-server :8080
    │  streaming SSE tokens
    ▼
FastAPI LXC  →  OpenClaw VM  →  User
```

OpenClaw decides whether to skip retrieval based on query classification (simple heuristic or a lightweight intent classifier).

---

## Agentic RAG Flow — LLM-Triggered Memory Search

> **Why the LLM doesn't have a direct line to Qdrant:** In standard RAG, the LLM never contacts Qdrant at runtime. FastAPI retrieves context *before* the LLM sees the request, and injects the chunks as plain text into the prompt. The LLM reads that text and responds — it has no awareness of Qdrant at all.
>
> However, with **agentic RAG**, the LLM itself can *request* a memory search mid-response by outputting a tool call. OpenClaw intercepts the tool call, executes the search via FastAPI → Qdrant, and feeds results back. This creates a feedback loop where the LLM decides what it needs to know.

```text
┌──────────────────────────────────────────────────────────────┐
│                     OPENCLAW VM                               │
│                                                               │
│  User message arrives from Telegram / Discord                │
│         │                                                     │
│         ▼                                                     │
│  Standard RAG: embed query → Qdrant → inject top-k chunks   │
│         │                                                     │
│         ▼                                                     │
│  POST /llm/infer  (prompt includes injected chunks)          │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│               AI VM — llama-server                            │
│                                                               │
│  Generates tokens...                                         │
│                                                               │
│  Produces EITHER:                                            │
│                                                               │
│  A) A plain text response  → stream directly to user         │
│                                                               │
│  B) A tool call:                                             │
│     { "tool": "search_memory",                               │
│       "args": { "query": "what model am I running?" } }      │
└──────────────┬────────────────────────────────────────────────┘
               │ (B) tool call detected by OpenClaw
               ▼
┌──────────────────────────────────────────────────────────────┐
│                     OPENCLAW VM                               │
│                                                               │
│  Intercepts tool call before sending to user                 │
│  Executes: POST /memory/search  { query: "..." }             │
│         │                                                     │
│         ▼                                                     │
│  FastAPI → embed → Qdrant → returns chunks                   │
│         │                                                     │
│         ▼                                                     │
│  Appends tool result to conversation history:                │
│  { "role": "tool", "content": "<retrieved chunks>" }         │
│         │                                                     │
│         ▼                                                     │
│  Re-submits full conversation (with tool result) to LLM      │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│               AI VM — llama-server                            │
│                                                               │
│  Sees the tool result, generates final text response         │
│  Streams back to OpenClaw → Telegram / Discord               │
└──────────────────────────────────────────────────────────────┘
```

> **Loop limit:** Set a hard maximum of 3–5 tool call iterations per user message. Without a limit, a misbehaving model can loop indefinitely and consume all VRAM/tokens.

> **Available tools the LLM can call:**
> - `search_memory(query, filters?)` — search Qdrant for relevant context
> - `get_infra_status()` — fetch current homelab state
> - `read_memory_file(path)` — read a specific markdown file
> - `write_memory(content, type, tags)` — persist a new memory (always requires confirmation)
> - `run_workflow(name, params)` — trigger a named automation (requires approval gate)

## Phase 0 — OpenClaw Core Development

## Goals

Design and build the OpenClaw orchestration application. This is the most critical phase because every other phase depends on it. No deployment can happen until this software exists.

### Architecture Decisions to Make First

| Decision                  | Options                                      | Recommendation                          |
| ------------------------- | -------------------------------------------- | --------------------------------------- |
| Agent loop framework      | LangGraph, custom state machine, AutoGen     | LangGraph (structured, debuggable)      |
| Language                  | Python, Go                                   | Python (LLM ecosystem compatibility)    |
| HTTP client               | httpx (async), requests                      | httpx with async for streaming support  |
| Config management         | pydantic-settings, dynaconf                  | pydantic-settings (type-safe)           |
| Memory write format       | custom, frontmatter-yaml                     | python-frontmatter library              |
| Filesystem watcher        | watchdog, inotifywait                        | watchdog (cross-platform Python)        |
| Token counting            | tiktoken, transformers tokenizer             | tiktoken or model-specific tokenizer    |

### Tasks

* define OpenClaw internal architecture (agent loop, memory writer, workflow executor)
* implement the agent loop with intent classification (conversational / RAG / tool / automation)
* implement **Telegram bot handler** using aiogram (async, long-polling mode)
  — configure bot token via environment variable
  — implement user ID whitelist (only your Telegram user ID can interact)
  — implement `/start`, `/clear`, `/status` slash commands
* implement **Discord bot handler** using discord.py
  — configure bot token via environment variable
  — implement user ID whitelist (only your Discord user ID)
  — listen to DMs or a specific private server channel
* build message normalization layer: both handlers produce a unified `Message` object with `(text, user_id, platform, reply_fn)`
* build markdown memory read/write layer using python-frontmatter
* implement rolling conversation buffer with token-budget-aware truncation (per user ID)
* build filesystem watcher integration using Python watchdog
* implement ingestion pipeline: chunk → embed (via FastAPI) → upsert Qdrant + PostgreSQL
* implement Redis retry queue for failed ingestion jobs
* implement FastAPI client module (JWT token management, refresh logic)
* implement agentic RAG tool call handler with loop limit (max 5 iterations)
* build basic personality layer (persistent system prompt loaded from `/memory/preferences/`)
* write unit tests for memory write/read cycle
* write integration tests against mock FastAPI + mock Qdrant

### Deliverables

* deployable OpenClaw Python application package (with Dockerfile)
* `docker-compose.yml` for the OpenClaw VM
* working Telegram bot you can message and get responses from (even against a mock LLM)
* working Discord bot (same)
* documented internal API surface (OpenAPI spec for any endpoints it exposes)
* tested memory write → ingestion → retrieval cycle end-to-end
* agentic RAG loop working (tool call → Qdrant search → response)

> Phase 4 (OpenClaw Integration) cannot proceed without completing Phase 0 first.

---

## Phase 1 — Infrastructure Foundation

## Goals

Establish the physical and virtual infrastructure that all other phases run on top of.

### Tasks

* install and configure Proxmox on the host machine
* configure IOMMU and VFIO kernel parameters for GPU passthrough
* bind NVIDIA A5000 to VFIO driver, blacklist host NVIDIA modules
* configure OPNsense: create AI VLAN (192.0.2.0/24), set firewall rules per the allow/deny table
* provision all VMs and LXCs with resources per the allocation table
* configure internal DNS (OPNsense Unbound) for `.ai.local` domain resolution
* deploy NGINX reverse proxy with self-signed or Let's Encrypt cert
* deploy Monitoring LXC: Grafana, Prometheus, node_exporter on each host
* configure Prometheus scrape jobs for all services
* validate GPU passthrough with `nvidia-smi` inside AI VM

### Deliverables

* stable Proxmox virtualization platform
* isolated AI VLAN with firewall rules enforced
* GPU passthrough confirmed working in AI VM
* monitoring dashboards live in Grafana

---

## Phase 2 — Local Inference Stack

## Goals

Run local LLMs reliably on GPU with a stable OpenAI-compatible API.

### Tasks

* install Ubuntu 22.04 LTS on the AI VM
* install NVIDIA drivers (driver version ≥ 535 for CUDA 12.x)
* install CUDA toolkit 12.x and verify with `nvcc --version`
* clone and build llama.cpp with CUDA support (`cmake -DLLAMA_CUDA=ON`)
* select and download Qwen model (see VRAM constraint table — recommend Qwen2.5-14B-Q4_K_M)
* download a dedicated embedding model (recommend `nomic-embed-text-v1.5` in GGUF format)
* start `llama-server` on `:8080` with appropriate context size and GPU layers
* start embedding model server on `:8081`
* install NVIDIA DCGM Exporter, configure Prometheus to scrape `:9400`
* benchmark inference: tokens/second, VRAM usage, context fill time
* document final model choice and quantization level

### Model Selection Reference

| Model                    | Quant   | Est. VRAM | Notes                                  |
| ------------------------ | ------- | --------- | -------------------------------------- |
| Qwen2.5-7B-Instruct      | Q4_K_M  | ~5GB      | Fast, low quality ceiling              |
| Qwen2.5-14B-Instruct     | Q4_K_M  | ~10GB     | Good balance — recommended start       |
| Qwen2.5-32B-Instruct     | Q4_K_M  | ~20GB     | Near 24GB limit, monitor VRAM headroom |
| nomic-embed-text-v1.5    | F16     | ~0.6GB    | Embedding model, always loaded         |

### Deliverables

* GPU-accelerated local LLM inference confirmed
* OpenAI-compatible API on `:8080` returning streaming completions
* Embedding API on `:8081` returning float vectors
* VRAM utilization visible in Grafana

---

## Phase 3 — Memory Infrastructure

## Goals

Build the full semantic memory retrieval stack that OpenClaw will rely on.

> **Dependency:** Phase 3 requires Phase 2 to be complete. The embedding pipeline calls the AI VM (llama.cpp with an embedding model) to generate vectors. Ensure the AI VM inference API is operational and benchmarked before building the embedding pipeline.

### Tasks

* deploy Qdrant in Docker on Memory LXC, create `memory` collection with correct vector dimensions
* deploy PostgreSQL in Docker on Memory LXC, run schema migrations (memory_chunks, memory_files, entities, entity_mentions tables)
* deploy Redis in Docker on Memory LXC with AOF persistence enabled (`appendonly yes`)
* build and test the embedding pipeline: markdown file → chunks → embed → upsert Qdrant + PostgreSQL
* implement idempotent upsert using `sha256(file_path + chunk_index)` as point ID
* implement delete-on-file-removal logic (clean up orphaned Qdrant points and PostgreSQL rows)
* implement filesystem watcher using Python watchdog
* implement Redis retry queue with exponential backoff for failed writes
* write tests: ingest a file, search for it, verify results

### Deliverables

* semantic memory retrieval working end-to-end
* searchable memory corpus via Qdrant
* metadata indexed in PostgreSQL
* ingestion pipeline with failure recovery via Redis retry queue

---

## Phase 4 — OpenClaw Integration

## Goals

Deploy OpenClaw and wire it to all backend services to produce a working memory-aware assistant.

> **Dependency:** Requires Phase 0 (OpenClaw software built), Phase 2 (inference running), and Phase 3 (memory stack running).

### Tasks

* provision OpenClaw VM with resources per allocation table
* deploy OpenClaw application package (Docker Compose)
* configure environment variables: FastAPI URL, JWT secret, memory path, Telegram bot token, Discord bot token
* configure Telegram user ID whitelist and Discord user ID whitelist
* configure `/memory/` directory layout and seed initial preference/personality files
* start filesystem watcher (watchdog) monitoring `/memory/`
* configure OPNsense outbound rules: allow OpenClaw VM → `api.telegram.org:443` and `discord.com:443`
* test Telegram: send a message → get a response → verify RAG retrieval is working
* test Discord: same
* end-to-end test: send a message → RAG retrieval → LLM response → memory written → ingested
* configure conversation buffer size and token budget limits
* tune `top_k` and chunk size for retrieval quality
* verify agentic RAG tool calls loop correctly and respect the iteration limit

### Deliverables

* working persistent AI assistant reachable via Telegram and Discord
* memory-aware responses using RAG retrieval
* agentic RAG tool calls working correctly
* new memories automatically ingested after being written to disk
* approval-gate workflow messages delivered back to the correct platform

---

## Phase 5 — Infrastructure Awareness

## Goals

Allow the assistant to answer questions about and observe the state of the homelab.

### Tasks

* implement Proxmox API client in FastAPI (`GET /infra/status`, `GET /infra/vm/{id}`)
  — use Proxmox API token (not root password) with read-only permissions
* implement Docker API client (query container status via Docker socket inside each LXC)
* integrate Prometheus query API for metrics retrieval (GPU VRAM, CPU load, temperatures)
* implement OPNsense API client for firewall and network state
* implement log retrieval endpoints (tail systemd journal, Docker container logs)
* cache infra status in Redis (TTL 30s) to avoid hammering APIs on every query
* write infrastructure context summarizer: formats raw API responses into human-readable status

### Deliverables

* assistant can answer: "what's the VRAM usage right now?", "are all containers running?", "what's the Proxmox CPU load?"
* infrastructure status cached and refreshed automatically
* read-only API access only — no destructive operations in this phase

---

## Phase 6 — Automation Layer

## Goals

Enable the assistant to perform controlled actions on the infrastructure, with human approval gates.

### Tasks

* design tool execution framework: each tool is a Python function wrapped with metadata (name, description, parameters, risk_level)
* implement approval gate: high-risk tools require explicit user confirmation before execution
* implement container management tools: start/stop/restart Docker containers
* implement VM management tools: start/stop Proxmox VMs (read-only until approved)
* implement backup automation: trigger Proxmox VM snapshots, PostgreSQL dump, Qdrant snapshot
* implement monitoring-based triggers: alert + suggest action when threshold breached (e.g. VRAM > 90%)
* implement audit log: every tool execution written to PostgreSQL with timestamp, tool name, parameters, outcome

### Risk Classification

| Risk Level | Examples                              | Gate Required |
| ---------- | ------------------------------------- | ------------- |
| Low        | read status, tail logs, list VMs      | No            |
| Medium     | restart container, clear cache        | Confirm once  |
| High       | delete VM, modify firewall, rm -rf    | Explicit yes/no prompt with summary |
| Critical   | destroy dataset, push to production   | Never allow without multi-step approval |

### Deliverables

* semi-autonomous workflow execution with approval gates
* audit trail of all automated actions
* container and backup automation working

---

## Phase 7 — Voice Interface

## Goals

Enable real-time voice interaction with end-to-end streaming.

### Tasks

* deploy faster-whisper on AI VM in server mode on `:8082` (streaming transcription)
* deploy Piper TTS on AI VM in server mode on `:8083`
* implement wake-word detection (e.g. using openWakeWord or Picovoice Porcupine)
* implement audio capture and WebSocket streaming from client to STT
* implement sentence boundary detector in FastAPI streaming layer
* implement TTS per-sentence dispatch (don't wait for full LLM response)
* validate full pipeline end-to-end: speak → transcribe → retrieve → infer → synthesize → speak
* measure and document end-to-end latency (target: < 2 seconds time-to-first-audio)

### Planned Stack

#### STT

faster-whisper (CTranslate2-optimized Whisper) — runs on AI VM GPU

#### Wake Word

openWakeWord (lightweight, runs on CPU in OpenClaw VM)

#### TTS

Piper (fast neural TTS, CPU-capable, runs on AI VM or separate LXC)

> **Latency Warning:** The voice pipeline routes through STT → OpenClaw → FastAPI → LLM → TTS, crossing multiple network hops inside the VLAN. Without streaming, the user waits for the full LLM response before hearing any audio. Implement LLM token streaming and TTS sentence-level streaming to reduce perceived latency to an acceptable level. Validate end-to-end latency before shipping Phase 7.

> **GPU Contention:** faster-whisper and llama-server both use the A5000. During a voice query, both run sequentially (STT first, then LLM, then TTS). They should not run simultaneously. Implement a simple GPU lock (Redis semaphore) if contention is observed.

### Deliverables

* voice assistant with wake-word activation
* streaming conversational interaction (< 2s time-to-first-audio target)
* full pipeline latency documented and optimized

---

## Phase 8 — Advanced Memory and Intelligence

## Goals

Improve contextual understanding, memory quality, and long-term intelligence.

### Tasks

* build knowledge graph using Neo4j (preferred for Cypher queries and persistence)
  — extract entities (people, projects, concepts) from memory files using LLM-based NER
  — store relationships: Person KNOWS Project, Project USES Technology, etc.
* implement memory ranking: score chunks by recency, access frequency, and relevance
* add automatic summarization pipeline: after N conversation turns, summarize to `/memory/history/`
* implement periodic reflection: weekly summary generation from recent history
* add long-term preference tracking: detect patterns in user behavior and write to `/memory/preferences/`
* implement memory pruning: archive or compress old low-priority chunks to reduce Qdrant collection size
* add cross-memory relationship modeling using entity_mentions table (Phase 3 schema)

### Knowledge Graph Schema (Neo4j)

```cypher
(:Person {name, first_mentioned, last_mentioned})
(:Project {name, status, priority, updated_at})
(:Technology {name, category})
(:Goal {description, status, deadline})

(:Person)-[:WORKS_ON]->(:Project)
(:Project)-[:USES]->(:Technology)
(:Person)-[:HAS_GOAL]->(:Goal)
(:Project)-[:DEPENDS_ON]->(:Project)
```

### Deliverables

* richer memory system with entity relationships
* memory ranking producing better retrieval quality
* automatic summarization keeping memory corpus manageable
* persistent personality that improves with usage

---

# Security Principles

## Core Principle

The assistant should never have unrestricted system access. Every capability that touches infrastructure must be explicitly scoped and gated.

---

## Authentication and Secrets

* All service-to-service calls authenticated via short-lived JWT (HMAC-SHA256, 15-minute TTL)
* Secrets stored as environment variables, never hardcoded in application code
* Proxmox API access uses a dedicated read-only API token (not root credentials)
* Docker socket access is never exposed directly to OpenClaw — only via a FastAPI wrapper with scoped permissions
* No service should have credentials to another service's database (e.g. OpenClaw does not have a PostgreSQL connection string — it goes through FastAPI)

---

## Never Allow

* unrestricted shell execution on any host
* direct Docker socket exposure to the LLM or agent
* Proxmox destruction APIs callable without approval gate
* destructive automation triggered autonomously
* any service communicating outside the internal VLAN without explicit firewall rule
* credentials logged anywhere (application logs, memory files, LLM context)

---

## Always Require Approval

For:

* deleting or destroying VMs or LXCs
* modifying firewall rules
* any write to production infrastructure
* destructive workflows (rm, DROP TABLE, snapshot delete)
* changes that cannot be easily reversed

Approval must be an explicit user confirmation step in the OpenClaw workflow executor — not inferred from conversation context.

---

# Long-Term Goal

The long-term objective is not merely to build a chatbot.

The long-term objective is to build:

```text
A persistent local AI operating layer
for memory, infrastructure, workflows,
and long-term contextual intelligence.
```

An assistant that:

* evolves with usage
* retains continuity
* understands projects
* understands infrastructure
* assists operationally
* remains fully private and self-hosted
* becomes part of the overall homelab ecosystem

while remaining:

* modular
* inspectable
* portable
* maintainable
* replaceable
* future-proof
