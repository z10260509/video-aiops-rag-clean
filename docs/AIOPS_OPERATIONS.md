# 操作手冊：從零安裝、日常使用與驗收

更新日期：2026-10-21。

本手冊以 **Ubuntu 24.04／Linux x86_64、Bash、Docker、NVIDIA GPU** 的單機開發／展示環境為主。應用程式在三節點 Kind 叢集執行，Ollama 在主機的 Docker Compose 執行。這不是公開網路的正式服務部署指南。

**驗證範圍：**既有環境曾完成部署、影片與 RAG 驗收；「空白環境手動安裝順序」已核對來源及資源相依，但尚未在獨立空白主機完成端到端實測。不能把既有環境驗收視為全新安裝保證。Docker Desktop／WSL2 的主機連線位址、GPU 與 NFS 掛載需依其環境確認。

- [首次安裝](#首次安裝)：依序完成 1～10 步。
- [日常啟動](#日常啟動)：已安裝的人從這裡開始。
- [版本升級](#版本升級)：保留資料更新應用程式。
- [測試與展示](#測試與展示)：先正常影片，再人工修復。
- [故障排查](#故障排查)：依卡住的階段查證。

## 首次安裝

### 1. 準備電腦與工具

需要可連外下載 Docker images、Helm charts 與模型。規劃資源可先以 8 CPU、32 GiB RAM、50 GiB 可用磁碟為起點；這是開發配置建議，不是經壓測保證的最低需求。模型推論、同時轉檔及監控會共用主機資源。

安裝下列工具。系統套件的安裝可以使用 `sudo`；之後的專案指令使用一般使用者，並確認該使用者已能執行 Docker。

```bash
sudo apt-get update
sudo apt-get install -y git curl make python3 python3-venv nfs-common
```

| 工具 | 安裝來源／用途 |
| --- | --- |
| Docker Engine 與 Compose plugin | [Docker Ubuntu 安裝](https://docs.docker.com/engine/install/ubuntu/)、[Compose plugin](https://docs.docker.com/compose/install/linux/)；執行容器與 Kind 節點。 |
| NVIDIA 驅動與 Container Toolkit | 先讓主機 `nvidia-smi` 成功，再依 [NVIDIA 官方說明](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)設定 Docker GPU。 |
| Kind | [官方 Quick Start](https://kind.sigs.k8s.io/docs/user/quick-start/)；用 Docker 建立 Kubernetes。 |
| kubectl | [Linux 安裝](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/)；操作叢集。版本需與選用的 Kubernetes 相容。 |
| Helm 3 | [官方安裝](https://helm.sh/docs/intro/install/)；安裝儲存與監控。 |
| Python 3.12 | 執行部署工具；應用程式使用 image 內的 Python。 |

在同一個終端確認：

```bash
git --version
docker version
docker compose version
kind version
kubectl version --client
helm version
python3 --version
nvidia-smi
docker info
```

成功條件：Docker client 能連到 server，主機 GPU 正常，以上工具皆可執行。GPU 尚未可供 Docker 使用時先完成 Toolkit 設定；目前 Compose 會要求 NVIDIA GPU，沒有自動 CPU fallback。

### 2. 從 GitHub 取得程式

預定的公開倉庫位址如下；需先完成首次上傳，若使用自己的 fork，請替換成 fork 的 URL：

```bash
git clone https://github.com/z10260509/video-aiops-rag-clean.git
cd video-aiops-rag-clean
```

本手冊所有後續指令都從**包含 `Makefile`、`backend/`、`scripts/` 和 `k8s/` 的目錄**執行。若 repository 外面還包一層，先 `cd kube_project`。不要使用外層原始包中的部署檔。

建立專案 Python 環境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements-test.txt
```

關閉終端後，執行 Python／Make 測試前需重新 `source .venv/bin/activate`。套件、前端編譯與模型都不會隨 Git clone 自動完成。正式 UI 會在 Docker build 中編譯，首次安裝不必另外在主機安裝 Node。

### 3. 啟動 GPU Ollama 與下載模型

```bash
docker compose -p video-diag up -d ollama
docker compose -p video-diag run --rm ollama-models
docker exec video-diag-ollama-1 ollama list
curl --fail http://127.0.0.1:11434/api/tags
```

成功條件：模型清單有 `gemma4:e2b-it-qat` 與 `embeddinggemma:300m`，HTTP 回應成功。首次下載需等到 `ollama-models` 結束，不能只看到容器啟動就繼續。Compose 以 repository 根目錄的 `.env` 或 shell 變數取得設定；`.env.example` 不會自動載入。預設模型已符合 bootstrap 檢查，不需先複製 `.env`。

需要確認 GPU 真正執行推論時，在另一個終端執行：

```bash
docker exec video-diag-ollama-1 ollama run gemma4:e2b-it-qat '只回答：模型測試成功'
docker exec video-diag-ollama-1 ollama ps
```

在模型仍駐留期間看 `PROCESSOR`；模型閒置卸載後，`ollama ps` 空白是正常狀態。資料保存在 `video-diag_ollama_data` volume，不在 Git，也不在 image 內。

### 4. 建立專用 Kind 叢集

這一步只用於全新安裝。若 `kind get clusters` 已有 `video-platform`，先判斷是不是既有專案，勿刪除或重建它。

```bash
kind get clusters
kind create cluster --name video-platform --config k8s/kind-config.yaml
kubectl config current-context
kubectl wait --for=condition=Ready nodes --all --timeout=180s
kubectl label nodes video-platform-worker video-platform-worker2 video-platform.io/workload=true
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/aiops/namespace.yaml
kubectl get nodes --show-labels
kubectl get storageclass
```

成功條件：context 是 `kind-video-platform`，三個 Node 是 Ready，兩個 worker 有 `video-platform.io/workload=true`，有 `standard` StorageClass。設定中的 nodeSelector 使用固定節點名稱，所以 cluster 名稱不能自行換掉。預設 node image 由安裝的 Kind 版本選定；若需固定 Kubernetes 版本，依 Kind 官方相容清單選定 `--image` 並記錄使用的 digest。

讓 Kind 節點具備 NFS client：

```bash
for task_node in $(kind get nodes --name video-platform); do
  docker exec "$task_node" sh -c 'command -v mount.nfs >/dev/null || (apt-get update && apt-get install -y nfs-common)'
done
```

這會修改本次叢集的節點容器，不會把工具安裝進應用 image。主機 kernel 仍需支援 NFS；若之後出現 `FailedMount`，看本手冊排查表。

### 5. 建立共享儲存、Prometheus 與 Grafana

以下 chart 版本沿用現有專案環境；安裝前可用 `helm show chart ... --version ...` 確認 repository 可取得。不要省略版本而無意換成另一版。

```bash
helm repo add nfs-ganesha-server-and-external-provisioner https://kubernetes-sigs.github.io/nfs-ganesha-server-and-external-provisioner/
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install nfs-server-provisioner nfs-ganesha-server-and-external-provisioner/nfs-server-provisioner \
  --version 1.8.0 --namespace nfs-provisioner --create-namespace \
  -f k8s/nfs-values.yaml --wait --timeout 10m
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --version 91.4.0 --namespace monitoring --create-namespace \
  -f k8s/monitoring-values.yaml --wait --timeout 10m
kubectl get storageclass
kubectl -n nfs-provisioner get pods,pvc
kubectl -n monitoring get pods,services
kubectl get crd servicemonitors.monitoring.coreos.com prometheusrules.monitoring.coreos.com
```

成功條件：有 `video-nfs` StorageClass，NFS 的底層 PVC 是 Bound，監控 Pod 正常，兩個 CRD 存在。NFS server 底層使用 Kind 的 `standard` 儲存，影片 PVC 用 `video-nfs` 共享給 backend 與 worker；這種資料保留於本地叢集生命週期，不能當成叢集外備份。

Prometheus URL 綁定 release 名稱 `monitoring`，應能找到 `monitoring-kube-prometheus-prometheus` Service。名稱不同時不要繼續，先核對 chart 版本／release 及 `k8s/aiops/api.yaml` 的 `PROMETHEUS_URL`。

### 6. 建置四個專案映像

GitHub 只提供原始碼，目前沒有保證能直接拉取的公開專案 image。從根目錄執行：

```bash
python - <<'PY'
from scripts.image_versions import build
from scripts.project_images import CONTROL, VIDEO, UI, DATABASE
for image, dockerfile in [
    (CONTROL, 'backend/Dockerfile'),
    (VIDEO, 'worker/Dockerfile'),
    (UI, 'frontend/Dockerfile.aiops'),
    (DATABASE, 'backend/Dockerfile.pgvector'),
]:
    build(image, dockerfile)
PY
python - <<'PY'
from scripts.deploy_common import command
from scripts.project_images import CONTROL, VIDEO, UI, DATABASE
command('kind', 'load', 'docker-image', CONTROL, VIDEO, UI, DATABASE, '--name', 'video-platform')
PY
```

成功條件：四個 image 建置及載入皆成功。版號來源只有 [kustomization.yaml](../k8s/kustomization.yaml)，個別 YAML 的無 tag 名稱是佔位值，不要自行補上文件或其他電腦使用的固定 tag。建置工具拒絕覆寫既有 tag；若前一步部分成功，先 `docker image inspect <完整名稱:tag>` 核對，只補建缺少的 image。程式有改動則先在 Kustomization 設定新 tag。不要為了重試覆寫舊 tag。

### 7. 先建立基礎影片平臺

現有 AIOps bootstrap 需要先能連到影片 API。相對地，完整 backend 宣告需要稍後才會產生的 AIOps token。下面一次性的 Python 區塊使用既有工具：產生本機 Secret、建立影片 DB／Redis／共享儲存、初始化**僅影片用 schema**，最後先關閉整合功能啟動 backend／worker。

**僅限第 4 步建立的空白叢集**；區塊會拒絕已存在的 backend 與影片 DB Secret。若中途失敗，保留資源與錯誤，依排查表處理，勿反覆產生新密碼。

```bash
python - <<'PY'
import secrets
from scripts.deploy_common import command, apply, render, validate, secret_env, deployment_lock
from scripts.bootstrap_hypothesis_v2 import schema_job
from scripts.project_images import CONTROL

with deployment_lock():
    validate()
    for kind, name in [('deployment', 'backend'), ('secret', 'video-platform-secrets')]:
        found = command('kubectl', '-n', 'video-platform', 'get', kind, name,
                        '--ignore-not-found', '-o', 'name', capture=True)
        if found.strip():
            raise RuntimeError('Existing video installation or partial setup: inspect it before continuing')
    for path in ['k8s/configmap.yaml']:
        for obj in render(path):
            apply(obj)
    password = secrets.token_hex(24)
    apply({'apiVersion': 'v1', 'kind': 'Secret',
           'metadata': {'name': 'video-platform-secrets', 'namespace': 'video-platform'},
           'stringData': {'POSTGRES_USER': 'video', 'POSTGRES_PASSWORD': password,
                          'DATABASE_URL': 'postgresql+psycopg://video:' + password + '@postgres:5432/video_platform'}})
    for path in ['k8s/postgres.yaml', 'k8s/redis.yaml', 'k8s/media-pvc.yaml']:
        for obj in render(path):
            apply(obj)
    command('kubectl', '-n', 'video-platform', 'rollout', 'status', 'statefulset/postgres', '--timeout=180s')
    command('kubectl', '-n', 'video-platform', 'rollout', 'status', 'deployment/redis', '--timeout=180s')
    schema_job('video-platform', CONTROL, 'python -m app.services.provision_video_schema',
               [secret_env('DATABASE_URL', 'video-platform-secrets', 'DATABASE_URL')])
    integration_tokens = {'AIOPS_VIDEO_READER_TOKEN', 'AIOPS_VIDEO_REPAIR_TOKEN', 'AIOPS_OPERATOR_TOKEN'}
    for path in ['k8s/backend.yaml', 'k8s/worker.yaml']:
        for obj in render(path):
            if obj['kind'] == 'Deployment':
                container = obj['spec']['template']['spec']['containers'][0]
                container['env'] = [item for item in container.get('env', [])
                                    if item['name'] not in integration_tokens]
                for item in container['env']:
                    if item['name'] in {'AIOPS_VIDEO_ENABLED', 'AIOPS_VIDEO_DEMO_ENABLED'}:
                        item['value'] = 'false'
            apply(obj)
    for name in ['backend', 'worker']:
        command('kubectl', '-n', 'video-platform', 'rollout', 'status', 'deployment/' + name, '--timeout=180s')
print('Base video API, worker, database and storage ready; AIOps integration is not initialized yet')
PY
kubectl -n video-platform get deployments,pods,pvc
kubectl -n video-platform exec deployment/backend -- python -c 'import httpx; r=httpx.get("http://127.0.0.1:8000/ready"); r.raise_for_status(); print(r.json())'
```

成功條件：backend 2 個副本、worker 1 個副本 Ready，影片 PostgreSQL／Redis 正常，媒體 PVC Bound，API 回覆 ready。此時先不要上傳影片，前端也尚未啟動。沒有把完整 control Alembic 套進影片 DB。

### 8. 確認叢集能連到主機模型，安裝日誌與 AIOps

`localhost` 在 Pod 內指 Pod 自己，不能當作主機 Ollama。Linux Docker 可先用 Kind network 的主機 gateway 當候選位址：

```bash
export PROJECT_OLLAMA_URL="http://$(docker network inspect kind --format '{{(index .IPAM.Config 0).Gateway}}'):11434"
printf '%s\n' "$PROJECT_OLLAMA_URL"
kubectl -n video-platform exec deployment/backend -- python -c 'import sys,httpx; r=httpx.get(sys.argv[1]+"/api/tags",timeout=15); r.raise_for_status(); print([m["name"] for m in r.json()["models"]])' "$PROJECT_OLLAMA_URL"
```

成功條件：**從 Pod 內**看到兩個必要模型。若失敗，改用這台電腦實際可由叢集連線的位址，再重做上面的檢查；不要照抄別人電腦的 `172.x.x.x`。Docker Desktop／WSL 的 gateway 不保證就是主機服務入口。保持此 shell，後續會使用 `PROJECT_OLLAMA_URL`。

安裝影片日誌收集與 AIOps：

```bash
python scripts/setup_monitoring.py
python scripts/bootstrap_hypothesis_v2.py --ollama-url "$PROJECT_OLLAMA_URL" --dry-run
python scripts/bootstrap_hypothesis_v2.py --ollama-url "$PROJECT_OLLAMA_URL"
```

第 6 步已建置映像，這裡**不要再帶 `--build`**。bootstrap 會再次載入映像、備份影片 DB、建立 AIOps control DB／角色／checkpoint、匯入八份 Runbook 和向量索引、產生整合 token，再切換影片 backend／worker 並部署前端與 exporter。備份位置會印在終端，權限為私有；不要上傳 GitHub。

Loki／Alloy release 名稱沿用程式設定的 `p1-loki`／`p1-alloy`，屬於實際服務名稱。bootstrap 只適用第一次 AIOps 初始化；若 API 已存在會拒絕重跑。

```bash
kubectl -n aiops get deployments,pods,pvc
kubectl -n video-platform get deployments,pods,pvc
kubectl -n aiops exec deployment/aiops-api -- python -c 'import httpx; r=httpx.get("http://127.0.0.1:8010/readyz"); r.raise_for_status(); print(r.json())'
python scripts/evaluate_rag.py --output /tmp/first-install-rag.json
```

成功條件：兩個 DB／媒體 PVC Bound，backend、worker、frontend、worker-metrics、aiops-api、aiops-graph-worker Ready；executor 目前 0 副本屬預期。RAG 評估需達 Recall@3 ≥ 0.8、引用檢查全部通過。RAG 通過不等於模型根因診斷或修復已驗收。

### 9. 開啟網頁、登入與開啟受限修復

```bash
make aiops-ui
```

保持此終端，瀏覽 `http://127.0.0.1:13010`。此路徑使用 frontend port-forward，**不必先安裝 Ingress controller**。`k8s/ingress.yaml` 只建立規則；若要使用主機 8080，需另行安裝相容 controller，並非本手冊的必要入口。

另一個終端回到專案根目錄，取得本次安裝產生的登入 token：

```bash
kubectl -n aiops get secret aiops-secrets -o jsonpath='{.data.operator-token}' | base64 --decode
```

影片上傳不需 Operator token；「AIOps 智慧診斷」、受控故障展示與人工核准需要它。不要把 token 貼進 issue、截圖或 Git。頁面重新整理後需要重新登入。

第一次 bootstrap 的修復預設關閉。先確認頁面、影片上傳與診斷可用，再依需求執行：

```bash
source .venv/bin/activate
python scripts/setup_hypothesis_v2.py --dry-run
python scripts/setup_hypothesis_v2.py --enable-remediation
kubectl -n aiops get deployment aiops-executor
```

升級腳本會檢查進行中影片、診斷與核准工作。若已有等待核准事件，先由操作者處理，不會自動取消。完成後 executor 應 1/1；這個開關只允許提出受限修復，**不會替任何事件人工核准**。

### 10. 驗收與可選 HPA

先照下方「測試與展示」完成正常影片，再測故障展示。這是「可以實際使用」的必要確認，不能只看 Pod Running。

固定副本的最小路徑已能使用。需要 worker CPU 自動擴縮時，先依 [Metrics Server 官方安裝與相容性說明](https://github.com/kubernetes-sigs/metrics-server)安裝相容版本、確認 `kubectl top nodes`／`kubectl top pods -n video-platform` 有資料，再執行：

```bash
kubectl apply -f k8s/hpa.yaml
kubectl -n video-platform get hpa worker
```

Kind 若有 kubelet 自簽憑證問題，官方提供 `--kubelet-insecure-tls` 作為略過驗證選項；僅限自己的本機測試叢集，先確認錯誤再調整。HPA 指標來自 Metrics Server，安裝 Prometheus 不會自動滿足它。記憶體修復仍只允許固定 1Gi → 2Gi，副本增加也會增加主機容量需求。

## 日常啟動

已安裝完成且資料／叢集仍保留時，不再跑首次安裝：

```bash
source .venv/bin/activate
docker compose -p video-diag up -d ollama
kubectl config use-context kind-video-platform
make aiops-status
kubectl -n video-platform get deployments,pods
make aiops-ui
```

Kind 節點容器由 Docker 管理；若 Docker 關閉，先啟動 Docker 並確認原節點恢復，勿以重建叢集取代開機。重新登入需再讀取既有 Operator token；不需重新產生 Secret 或下載模型。Ollama 暫時離線時修復工具仍有其政策限制，診斷可能失敗。

只結束 `make aiops-ui` 會關閉網頁轉發，服務和資料仍保留。`docker compose -p video-diag stop ollama` 可停止模型服務；不要用刪 volume 或刪 Kind 叢集作為日常關機方式。

## 版本升級

先停止新增上傳／診斷並等工作完成，保存修改、資料庫與媒體備份，再取得欲部署的 Git 版本。Git checkout 只切換來源，不會復原 DB、影片或模型，也不會自動回滾已部署 image／schema。

映像版號來源為 [k8s/kustomization.yaml](../k8s/kustomization.yaml)。修改程式的角色設定未用過的新 tag，再建置；未改動的 UI／DB 可沿用既有 image。升級只需 CONTROL／VIDEO／UI，DB 變更另行評估，勿用例行升級重建 DB。`--build` 會同時建置 CONTROL 與 VIDEO，因此兩者都必須是尚未使用的新 tag；若只改其中一個角色，依第 6 步的 `build(image, dockerfile)` 方式只建該角色，再不帶 `--build` 升級。

```bash
source .venv/bin/activate
python scripts/setup_hypothesis_v2.py --dry-run
# 新 tag 尚未建置時，才增加 --build；已建好的 image 不重建：
python scripts/setup_hypothesis_v2.py --enable-remediation
```

`--dry-run` 只顯示目標與模式，不會建置、部署或完成線上健康檢查。正式升級須等進行中影片、診斷／核准及修復結束；工具不自動取消。升級保留 DB、PVC、HPA，並同步八份 Runbook 的 repository 來源及新版 chunks，舊引用保留。

**不帶 `--enable-remediation` 會關閉修復旗標與 executor。** 若先前需保留人工修復能力，升級時也需帶此選項。不要對既有環境直接 `kubectl apply -k k8s`，根清單包含資料庫與一次性初始化工作。

## 測試與展示

### 正常影片與人工修復

1. `make aiops-ui` 後開頁面，上傳一支小型、已知可播放的 MP4（小於 100 MiB）。
2. 等影片從等待／處理中變為完成，測播放、下載與縮圖。
3. 登入 AIOps，再回影片頁勾選「展示 FFmpeg 故障與人工修復」並上傳。它會留下真實失敗 Job。
4. 進入該影片事件，等待 Gemma／RAG／工具調查，檢查原 Job／task、證據、引用、政策及修復方案。
5. 有有效草案才由操作者核准；沒有按鈕時先看證據與政策原因，不反覆診斷到出現按鈕為止。
6. 核准後等 executor 執行，確認同一 Job 的新 task、H.264 新輸出與完整復原驗證，再回原影片列表播放。

`validated`＝引用／工具條件通過；`approved`＝人工核准已記錄；只有完整復原驗證通過才算修復成功。`uncertain` 不自動重送，頁面提供唯讀重新驗證。詳見 [展示與狀態說明](VIDEO_PLATFORM_AIOPS_INTEGRATION.md)。

### 命令列與隔離測試

| 指令 | 檢查內容與影響 |
| --- | --- |
| `make k8s-check` | 渲染與契約檢查，不套用資源；目前渲染工具會讀取 Kubernetes discovery，先建立可連線叢集。 |
| `make aiops-test-docker` | 專用 test stage、唯讀掛載、關閉網路；線上 integration 預設跳過。 |
| `make aiops-test` | 使用本機 venv 執行回歸；不是線上修復。 |
| `make aiops-security` | 政策、人工核准、工具及復原相關測試。 |
| `make aiops-model-check` | 真實模型＋合成觀測／暫存 SQLite；不是線上故障驗收。預設結果寫入 `/tmp/aiops-hypothesis-v2-real-model.json`，需保留多次結果時指定新的 `--output`。 |
| `make aiops-migrations OUTPUT=/tmp/new-migration.json` | 建立並移除臨時 Docker PostgreSQL／網路，驗證 migration，不操作線上 DB。 |

以下會真正上傳影片並留下 Job／事件，不自動核准或刪除：

```bash
make aiops-video-check VIDEO=/path/to/sample.mp4 OUTPUT=/tmp/new-healthy.json
make aiops-fault-codec VIDEO=/path/to/sample.mp4 OUTPUT=/tmp/new-fault.json
make aiops-benchmark VIDEO=/path/to/sample.mp4 OUTPUT=/tmp/new-benchmark.json REPEAT=1
```

各次使用新的結果路徑。benchmark 最多五次，首度失敗就保存並停止。線上 pytest 需明確開啟：

```bash
VIDEO_V2_INTEGRATION=1 VIDEO_SOURCE=/path/to/sample.mp4 PYTHONPATH=backend:. python -m pytest tests/integration
```

可選 UI fixture 測試需要主機 Node／npm、已啟動的網頁與 Playwright。將測試依賴放在專案外暫存目錄：

```bash
mkdir -p /tmp/video-platform-browser
npm install --prefix /tmp/video-platform-browser playwright
/tmp/video-platform-browser/node_modules/.bin/playwright install --with-deps chromium
PLAYWRIGHT_PACKAGE=/tmp/video-platform-browser/node_modules/playwright make aiops-browser
```

此測試攔截 API，包含的核准／拒絕只送到 mock，不會對線上事件核准。首次下載瀏覽器或安裝系統依賴可能需較長時間。

## 故障排查

| 現象 | 查核與處理 |
| --- | --- |
| Ollama 容器因 GPU 啟動失敗 | 檢查 `nvidia-smi`、Docker GPU Toolkit 與 `docker compose -p video-diag logs ollama`；目前不是 CPU Compose。 |
| `ollama ps` 空白 | 它列的是駐留模型；先用 `ollama list` 確認安裝，再啟動推論時觀察。 |
| Pod 連不到 Ollama | 從 backend Pod 查 `/api/tags`；確認主機位址、11434 publish／防火牆。Pod 的 localhost 不是主機。 |
| backend／worker Pending | `kubectl -n video-platform describe pod <Pod名稱>`；看 nodeSelector、資源配額與 PVC，核對兩個 worker 的標籤。 |
| PVC Pending／FailedMount | 查 namespace 的 PVC／Events、StorageClass、NFS server 及底層 PVC、節點 `mount.nfs` 和主機 NFS kernel 支援。 |
| `ImagePullBackOff` | 核對 Kustomization 的完整 tag 已建置，且 `kind load` 成功載入三個節點；GitHub 不提供你的本機 image。 |
| Secret 缺 key／登入失敗 | 初次影片 Secret 與 AIOps Secret 是不同身分；讀取本次叢集的 operator-token，勿複製其他人的 Secret。 |
| `no matches for kind ServiceMonitor` | 先安裝 kube-prometheus-stack 並等 CRD 就緒，再重做失敗的資源步驟。 |
| schema Job 失敗 | `kubectl -n <namespace> get jobs`、`kubectl -n <namespace> logs job/<名稱>`；保留失敗 Job，查明前不重跑 migration。 |
| 首次安裝區塊拒絕重跑 | 已存在部分安裝；先查 DB、Secret、schema 與 Deployment。Secret 的資料庫密碼不能任意換新。 |
| bootstrap／升級拒絕 | 確認 context 與資源擁有標籤，並處理進行中影片、診斷／待核准及修復。bootstrap 不接受已存在的 AIOps API。 |
| 沒有人工核准按鈕 | 查模型／工具證據、最新報告、目標政策、有效期限與三個修復旗標；不要繞過後端政策。 |
| 已核准但未成功 | 查 executor 是否 1/1、事件時間線與執行結果；核准不代表完成，`uncertain` 不應盲目重送。 |
| localhost:13010 開不了 | 保持 `make aiops-ui` 執行，檢查 frontend Pod；Pod 更新後 port-forward 可能需重新啟動。 |
| HPA 顯示 unknown | 先驗證 Metrics Server／`kubectl top`，再看 HPA Events；與 Prometheus 是不同管線。 |

查服務與最近事件（只讀）：

```bash
kubectl -n video-platform get pods,pvc
kubectl -n aiops get pods,pvc
kubectl -n video-platform get events --sort-by=.lastTimestamp
kubectl -n aiops get events --sort-by=.lastTimestamp
kubectl -n aiops logs deployment/aiops-api --tail=100
kubectl -n aiops logs deployment/aiops-graph-worker --tail=100
kubectl -n aiops logs deployment/aiops-executor --tail=100
```

分享錯誤時先遮蔽 token、連線密碼與個人檔名。首次安裝的基礎區塊與 bootstrap 不是失敗後可任意重跑的全自動修復工具；它們會保留失敗證據與資料。

## 資料、備份與分享

- GitHub 保存來源、設定範本與文件，不保存本機 DB、原影片／輸出、Kubernetes Secret 或模型 volume。
- 影片 DB 的 `postgres-data`、control DB 的 `aiops-postgres` 與共享媒體 `media-storage` 都需備份；只備份 Git 不足以復原服務資料。
- bootstrap 的影片 DB dump 是初始化前快照，不是持續備份。監控設定的 Prometheus 使用 emptyDir，Pod 重建可能失去歷史 metrics。
- 保持 `.env` 與真實 Secret 在 Git 之外。安裝使用新產生的憑證，不使用 `secret.example.yaml` 的 `REPLACE_ME`。
- 不把刪除 PVC、`docker compose down -v`、刪除 Kind cluster 當作重試或日常關機步驟。

## 外部安裝參考與專案驗證

儲存 chart 的 repository、參數與持久化說明以 [NFS provisioner 官方文件](https://github.com/kubernetes-sigs/nfs-ganesha-server-and-external-provisioner/blob/master/charts/nfs-server-provisioner/README.md)為依據；監控 chart 的來源見 [Prometheus Community](https://prometheus-community.github.io/helm-charts/)。Metrics Server 的憑證選項及相容性以其官方文件為準。這些外部來源支援安裝方式，不代表本專案已在所有平台完成驗收。

目前系統架構見 [Kubernetes 架構](K8S_INTEGRATION.md)、[假說調查架構](HYPOTHESIS_RAG_V2.md)。首次安裝完成後，依本手冊的成功條件與測試流程驗收；測試輸出自行保存於專案外，不會隨來源提供。


## 統一程式排版

Python 使用 Ruff 0.17.1，JS／JSX、CSS、HTML、JSON 與 YAML 使用 Prettier 3.9.10，Shell 使用 shfmt 3.14.1。它們是開發工具；執行中的服務不需要另外安裝。Python 每層四個空格，前端與 YAML 每層兩個空格，行寬以 100 字元為目標；Makefile 保留必要的 Tab。

先啟用專案 venv、更新測試依賴，並在前端安裝 lockfile 中的套件：

```bash
source .venv/bin/activate
python -m pip install -r backend/requirements-test.txt
npm --prefix frontend ci
```

另外從 [shfmt 官方發行頁](https://github.com/mvdan/sh/releases/tag/v3.14.1)安裝對應系統的執行檔，放入 PATH，確認 `shfmt --version` 為 v3.14.1。工具備妥後，從專案根目錄執行：

```bash
make format
make format-check
```

共用設定為 `.editorconfig`、`pyproject.toml`、`.prettierrc.json` 與 `.prettierignore`。排版範圍涵蓋程式與設定；Runbook／Markdown 文字、生成的 lockfile、影片、套件與編譯輸出保持其原用途。Kustomization 雖以 `.yaml` 命名，仍維持 JSON 語法供部署工具讀取。Grafana 內嵌 JSON 以多行區塊保存。

Ruff 的 format 只做排版，不執行 lint 自動修復；相關行為見 [Ruff 格式器文件](https://docs.astral.sh/ruff/formatter/)。前端與設定格式選項見 [Prettier 設定說明](https://prettier.io/docs/configuration)。
