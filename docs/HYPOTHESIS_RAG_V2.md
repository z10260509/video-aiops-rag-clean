# 新版假說調查架構

現行新診斷固定使用 `aiops-hypothesis-v2`，提示詞／報告版本為 `hypothesis-rag-v2`。調查、證據驗證與人工核准皆使用同一套流程。部署命令集中於 [操作手冊](AIOPS_OPERATIONS.md)。

## 從影片失敗到復原

1. **影片處理**：影片 API 建立 Job，透過 Redis／Celery 交給 FFmpeg worker，保存任務結果、原 Pod／task 身分及 encoder 快照。
2. **建立事件**：`video_bridge` 讀取原影片私有 API 的失敗紀錄並去重，建立 Incident／DiagnosisRun。系統也可接收受認證的 Alertmanager 告警。
3. **初始採證**：對已連結原影片的事件，讀取原 task 與 Pod 狀態。工具目標由服務端綁定，模型不能指定其他 Job、Pod 或任意命令。
4. **模型提出假說**：Gemma 根據實際觀測提出最多三個原因，包含預期條件、必要工具、缺少資訊及 Runbook 搜尋問題。
5. **動態 RAG 與調查**：按各假說檢索版本化 Runbook；模型根據缺口選擇受限唯讀工具，再用回傳觀測更新假說。
6. **服務端查核**：檢查模型引用的證據、型別與具體條件，保留支持、反證及未解問題，產生報告並比較修復方案。
7. **政策與人工核准**：只有白名單方案、足夠證據及目標前置條件皆通過，才建立人工核准草案。
8. **受限執行與驗證**：人工核准後，由專用 executor 重查政策、執行固定操作，再驗證同一影片的新任務、輸出與部署狀態。

模型可以提出既有三種修復以外的原因；原因無法對應已支持的白名單操作時，交由人工查核。

## 模型如何引用觀測

模型從 `scalar-catalog-v2-stable` 欄位目錄選擇 `field_id`、運算子與自己預測的 `expected`。伺服器把代號綁回原 Evidence ID、JSON path 與型別，再執行 `equals`／`contains`／`greater_than` 比較。代號由原證據、路徑與型別穩定產生，不因顯示順序改指其他欄位。

引用必須符合本事件／run、namespace／Pod／Job／task、checksum、採證時間及來源狀態。錯誤來源、截斷或未知欄位都不能充當有效證據；數字 0 不等於布林 false。每個必要工具都必須有實際 supported assertion 引用其結果，工具只是回傳成功並不算完成查核。

`supported`表示具體觀測條件獲得支持，並不證明所有自然語言因果解釋都正確。假說也可能是 contradicted 或 unresolved；過往反證保留，不能刪除假說或重用相同 ID 更換原因來抹去反證。

## 工具與調查上限

最多三輪模型調查、八次唯讀工具查詢，每輪最多兩個額外查詢；初始採證也計入預算。最後一輪整理既有觀測，不再發出新工具查詢，因此不一定用滿八次。服務端優先補齊模型領先假說已宣告、尚未執行的固定必要查核（原任務、Pod、encoder inventory、輸入 FFprobe），並記錄實際排程；不替模型發明指標或日誌查詢參數。補查仍使用相同預算、目標綁定與不可變證據，模型仍須引用結果才能通過驗證。

工具包括原任務狀態、Pod 狀態／事件、任務綁定的 Loki 日誌、固定 Prometheus 指標、queue、輸入檔 FFprobe 與原 worker encoder inventory。FFprobe 可讀取 stream，不代表每一幀完整；缺少 encoder 快照也不代表該 encoder 一定不存在。

同參數工具結果採不可變證據重用。工具執行前先記 reservation，中斷且沒有結果時保留 unknown，避免 checkpoint 重播偷偷突破預算。模型格式錯誤可在預算內取得欄位回饋；無法完成則維持 insufficient_evidence，不由規則代答。

## RAG、模型與儲存

預設 Runbook 來源為 [runbooks/](../runbooks/)，也有受認證的知識文件匯入 API。首次安裝與升級均以 `aiops-repo/runbooks/` 為來源同步全部八份文件；內容變更會產生新版 chunks／vectors，停用的歷史 chunks 仍可供既有事件引用。自行上傳同名文件屬不同來源，不會自動取代 repository 文件。文件切塊、版本、checksum 和 embedding profile 存於 control DB，正式向量檢索使用 pgvector。參考文件當作不受信任資料，不能提供執行命令或代替實際故障觀測。

Gemma／EmbeddingGemma 由外部 GPU Ollama 容器提供；調查程式在 Kubernetes。診斷請求會把經範圍限制與去敏的觀測及檢索內容傳給模型。`docs/` 的使用與架構說明不會自動進入 RAG。

Diagnosis 保存報告與逐輪查核，Evidence 保存觀測，Timeline 保存調查／核准事件，官方 PostgreSQL checkpointer 保存 LangGraph 恢復狀態。舊 migration 和歷史報告保留，舊 workflow 不再恢復執行。FakeEmbedding／SQLite 與 mock 是隔離測試依賴；fake provider 不會替正式 v2 產生規則診斷。

## 人工核准、白名單與多副本

可執行操作只有 H.264 重跑、Redis 還原後重跑，以及 worker 記憶體由既定 1Gi 提高為 2Gi 後重跑。模型可以描述建議，但不能新增可執行能力；三道修復開關、證據、Runbook 引用、目標政策及 SQL 核准紀錄仍須通過。

核准綁定方案參數、原 Job／failed task、目標設定、期限及 binding hash。新版 worker 綁定 Deployment UID 與排除 replicas 的 spec 雜湊；HPA 調整副本數本身不使授權失效，其他設定或 UID 變更仍會阻擋。提案、核准及執行前重查可用 worker 與擁有者關係；歷史核准維持原有契約。

復原要求同 Job 的新 task 成功、實際 FFprobe 輸出 codec 符合方案、Deployment 已更新且目標副本全部可用，並通過 Redis 或記憶體修復的專屬條件。不明結果不自動重送；唯讀重新驗證只查既有成果，不重跑影片。

測試方式與限制見 [操作手冊](AIOPS_OPERATIONS.md#測試與展示)。模型搭配合成觀測的測試，不能代替自己環境的原影片故障與人工修復驗收。
