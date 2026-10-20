# 使用文件

第一次使用，請從 [操作手冊的首次安裝](AIOPS_OPERATIONS.md#首次安裝) 開始，依 1～10 步準備工具、模型、Kubernetes、儲存、監控及應用服務。全新環境的手動安裝順序尚未在獨立空白主機完成端到端驗收；每一步的成功條件與限制都列於手冊。

| 需求 | 文件 |
| --- | --- |
| 從零安裝、登入、日常啟動、升級、測試與排錯 | [操作手冊](AIOPS_OPERATIONS.md) |
| 了解 Gemma 假說、RAG、工具查核及修復政策 | [調查架構](HYPOTHESIS_RAG_V2.md) |
| 了解 Kubernetes、模型容器、儲存與兩個資料庫 | [部署架構](K8S_INTEGRATION.md) |
| 展示影片故障、人工核准、修復與復原驗證 | [影片與 AIOps 使用流程](VIDEO_PLATFORM_AIOPS_INTEGRATION.md) |

映像版本以 [k8s/kustomization.yaml](../k8s/kustomization.yaml) 為準。服務即時狀態請依操作手冊查詢，不以文件中的測試數或事件快照判定。

這個目錄只放使用與架構說明，不會自動匯入 RAG。系統初始化需要的知識文件在 [runbooks/](../runbooks/)，需隨專案保留；部署時會版本化匯入 control DB。
