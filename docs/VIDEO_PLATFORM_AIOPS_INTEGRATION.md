# 影片與 AIOps 展示流程

影片上傳、Job、媒體檔、播放與下載留在原影片平臺；AIOps 負責調查與受限修復協調。操作入口及登入方式見 [操作手冊](AIOPS_OPERATIONS.md)，完整調查流程見 [新版架構](HYPOTHESIS_RAG_V2.md)。

## 如何展示

1. 開啟網頁，先到「AIOps 智慧診斷」以 Operator token 登入，再回影片平臺。
2. 正常上傳 MP4；完成後從原影片頁播放或下載。
3. 展示故障時，勾選「展示 FFmpeg 故障與人工修復」後上傳。此選項需操作者認證，會建立真實失敗 Job。
4. 開啟該影片的 incident，查看 Gemma 假說、工具證據、Runbook 引用、替代原因及修復方案。
5. 診斷與政策皆通過時，出現待人工核准草案。操作者核對影片、失敗 task、目標、參數及期限後，再自行核准或拒絕。
6. 核准後由專用 executor 執行固定白名單操作，重跑原 Job 的新 task，並檢查輸出與部署復原條件。
7. 完成後回原影片頁看結果；完整驗證通過才標示復原。結果不明時，可唯讀重新驗證既有成果，不自動重跑。

公開 codec 只接受 h264／h265。前端憑證只存在頁面記憶體，不存 localStorage／sessionStorage；重新整理後須重新登入。CLI 的健康／故障驗收用法統一列在操作手冊。

## 每個狀態代表什麼

| 狀態 | 含義 |
| --- | --- |
| `insufficient_evidence` | 必要觀測、引用或格式仍不足，尚不能建立可執行結論。 |
| `validated` | 報告引用與觀測條件通過；不保證所有因果解釋正確，也不等於政策允許修復。 |
| `WAITING_APPROVAL`／`pending` | 已有待審草案，仍需有效人工核准。草案期限與目標變更可能使它無法繼續核准。 |
| `approved` | 核准已記錄；不代表 executor 已完成修復。 |
| `uncertain` | 執行或復原結果尚不確定，不自動重送修復。 |
| 復原驗證通過 | 同 Job 的新 task、輸出 codec、Deployment 和該修復的專屬條件皆已驗證。 |

影片顯示完成與整份修復驗證通過是不同檢查，應同時查看 Job 和 incident 的驗證結果。

## 身分與執行限制

video reader 只讀取原影片採證與既有驗證資料；video repair 依已綁定的 Job／failed task 和 reservation 重跑；operator 建立診斷及核准／拒絕；executor 使用受限 DB 欄位與 Kubernetes RBAC，不能自行核准或更換目標。

白名單是 H.264 重跑、Redis 還原後重跑、worker 記憶體提高後重跑，支援多副本／HPA。核准仍綁定目標設定、原任務、期限與參數。CLI 只提供既有 FFmpeg 受控故障展示，不額外破壞 Redis 或注入 OOM。

測試指令及合成測試／實際影片驗收的區別見 [操作手冊](AIOPS_OPERATIONS.md#測試與展示)。現在能否核准與是否復原，以事件頁的最新報告、政策及驗證結果為準。
