# Kubernetes 架構與部署責任

部署命令、環境前置與修復開關集中在 [操作手冊](AIOPS_OPERATIONS.md)。本文件說明資源如何分工，不記錄會過期的即時副本數或待核准事件狀態。

## 服務分工

| 位置 | 元件 | 用途 |
| --- | --- | --- |
| `video-platform` | frontend | 同一個網頁提供影片上傳／播放與 AIOps 介面；沿用原 Service、Ingress 和 PDB。 |
| `video-platform` | backend、worker | 原影片 API 與 Celery／FFmpeg；保存 Job、任務觀測、執行與修復結果。 |
| `video-platform` | PostgreSQL、Redis、媒體儲存 | 影片資料、任務 broker，以及輸入／輸出／縮圖。 |
| `video-platform` | worker-metrics | 從 DB／Redis 讀取任務與 queue 指標，供 ServiceMonitor 抓取。 |
| `aiops` | aiops-api | 事件、證據、報告、知識與受認證的人工核准 API。 |
| `aiops` | aiops-graph-worker | 執行假說調查、RAG、唯讀工具及 checkpoint 恢復。 |
| `aiops` | aiops-executor | 以獨立最小權限執行已核准的白名單操作。 |
| `aiops` | aiops-postgres | control 資料、官方 graph checkpoint 與 pgvector 知識檢索。 |
| `monitoring` | Prometheus／Grafana、Loki／Alloy | 指標、面板與原影片 worker 日誌；基礎監控與 Helm 設定分開管理。 |
| Kubernetes 外的 Docker Compose | GPU Ollama、模型下載器 | Gemma 與 EmbeddingGemma；模型檔保存在 Docker volume，不放進 Git 或應用映像。 |

AIOps 透過受認證的原影片私有 API 讀取 task、probe 與驗證資料，再透過受限修復 API 重跑同 Job；control 程序不直接接管影片 DB。模型推論在外部 Ollama，會接收服務端限制範圍並去敏後的調查上下文。

## 宣告與版本來源

[k8s/kustomization.yaml](../k8s/kustomization.yaml) 是資源及映像版本來源，以 JSON 語法書寫，亦為合法 YAML。[scripts/project_images.py](../scripts/project_images.py) 從同一份設定讀取版本。個別 YAML 的無 tag 映像名稱由 Kustomize 解析後才部署。

目前清單渲染為 47 個資源，含影片、AIOps、exporter、監控、RBAC、Ingress、PDB、儲存及兩個 schema Job。Helm values 不當作一般 Kubernetes 資源套用；私密 Secret 另由初始化程序處理。

API、graph、executor、影片 backend 共用 backend source 映像，worker／exporter 使用 worker 映像。正式映像排除測試資料夾及 pytest；專用 Docker test stage 用於回歸。映像預設啟動不自動執行 Alembic。

## 兩個資料庫各自初始化

| 工作 | 允許處理的內容 |
| --- | --- |
| 影片 schema Job | `app.services.provision_video_schema` 建立 `jobs`、`video_task_outcomes`、`video_job_executions`、`video_job_repairs`。保留既有 Job；欄位不足時拒絕自動改寫。PostgreSQL 目標必須是 `video_platform`。 |
| AIOps control schema Job | Alembic head、官方 checkpoint、runtime／executor 角色。首次 bootstrap 的 owner 階段另匯入 Runbook 並建立向量索引。 |

歷史 Alembic 鏈保留在 [backend/alembic/](../backend/alembic/)。保留歷史建表步驟不表示舊診斷功能仍啟用；也不能把完整 control migration 套用到影片 DB。

## 首次初始化與既有環境升級

首次 AIOps bootstrap 需既有影片與監控基礎環境，依 DB 就緒、control schema／權限／知識、影片整合與應用程序的階段執行。修復預設關閉，已有 AIOps API 時拒絕重跑。

既有環境升級先檢查進行中影片、診斷／待核准工作及修復，再更新應用版本與所需服務／監控／權限。升級不重新套用 PVC、DB、schema Job 或 HPA，不強制更改副本數。根清單含初始化資源，不能用一次 `kubectl apply -k k8s` 取代這個流程。

監控沿用目前 Loki／Alloy release，資料庫沿用固定的 pgvector image 與既有 PVC。資源名稱與映像 tag 對應實際服務及儲存身分。

## 部署後確認

依 [操作手冊](AIOPS_OPERATIONS.md)檢查各個 Deployment、PVC、API readiness 與 RAG，再用正常影片及受控故障確認自己的環境。Pod Running、引用驗證通過與修復成功是不同條件，不能互相取代。

目前使用的 image tag 一律查 Kustomization；文件不固定記載某次本機部署版號或事件狀態。
