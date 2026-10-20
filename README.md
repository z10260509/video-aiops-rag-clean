# 影片平臺與 Hypothesis RAG AIOps

本專案提供以下流程：上傳影片 → Celery／Redis／FFmpeg → 發現失敗任務 → Gemma 提出假說 → Runbook RAG 與唯讀工具查核 → 三種修復政策 → 人工核准 → 重跑原影片與復原驗證。

應用服務部署於 Kubernetes，GPU Ollama 由 Docker Compose 啟動。映像版本統一設定於 [k8s/kustomization.yaml](k8s/kustomization.yaml)，部署架構見 [Kubernetes 架構](docs/K8S_INTEGRATION.md)。

## 程式分工

- `frontend/`：上傳、播放、下載及 AIOps 事件／證據／核准介面。
- `backend/app/api/`、`worker/`：原影片 API、FFmpeg、任務觀測與私有採證／修復 API。
- `backend/app/aiops/`：v2 假說、LangGraph、Gemma、pgvector RAG、修復政策與 executor。
- `scripts/`：新版初始化、升級、監控設定、原影片驗收、RAG 評估、臨時 DB migration 驗證與程式盤點。
- `backend/tests/`、`worker/tests/`：功能／政策／復原／工具回歸；`tests/`：新版 UI 契約及可選現場影片驗收。
- `k8s/`、`runbooks/`、`backend/alembic/`：部署與監控、知識文件、完整歷史 schema 升級鏈。
- `docker-compose.yml`：GPU Ollama 與模型下載器；應用服務在 Kubernetes。

## 第一次從 GitHub 安裝

從 [完整操作手冊](docs/AIOPS_OPERATIONS.md#首次安裝) 的第 1 步開始。手冊包含工具／GPU、取得程式、模型、Kind、共享儲存、監控、映像、基礎影片平臺、AIOps、登入及驗收的完整手動順序，每一步都有成功條件與排查方式。

適用主線是 Linux／Ubuntu 24.04、NVIDIA GPU 與本機 Kind。首次安裝需要從來源建置映像、下載模型並產生自己的憑證。[AIOps 初始化工具](scripts/bootstrap_hypothesis_v2.py)初始化的是已有基礎影片平臺上的 AIOps，請勿跳過手冊的基礎環境步驟，或直接對空叢集套用完整根清單。

全新環境的手動步驟已依程式與資源相依核對，尚未在獨立空白主機完成端到端驗收。請依操作手冊的成功條件與測試流程確認自己的環境。

## 安裝完成後的日常使用

```bash
docker compose -p video-diag up -d ollama
# 第一次下載模型才需要：
docker compose -p video-diag run --rm ollama-models
make aiops-status
make aiops-ui
```

開啟 `http://127.0.0.1:13010`。AIOps 的 Operator token 存於 Kubernetes Secret `aiops/aiops-secrets`，不放 Git。Ollama 保留 `video-diag_ollama_data`，不刪 volume。

## 使用文件

從 [文件入口](docs/README.md) 找到操作、架構、展示及驗證方式：

- [操作手冊](docs/AIOPS_OPERATIONS.md)：初始化／升級、修復開關、登入與全部測試命令。
- [新版調查架構](docs/HYPOTHESIS_RAG_V2.md)：Gemma、RAG、唯讀工具、政策與人工核准。
- [Kubernetes 架構](docs/K8S_INTEGRATION.md)：服務與資料庫分工、映像版本來源。
- [影片展示流程](docs/VIDEO_PLATFORM_AIOPS_INTEGRATION.md)：從上傳、故障到核准、修復與播放。

## 架構圖

<img width="1200" height="1121" alt="image" src="https://github.com/user-attachments/assets/3e941309-a0eb-413b-8cfe-ec6d4804fc57" />
