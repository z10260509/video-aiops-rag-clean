# Kubernetes 部署宣告

[kustomization.yaml](kustomization.yaml) 是資源與映像版號的設定來源，以 JSON 語法書寫，亦為合法 YAML；部署工具讀取同一份設定。個別資源的無 tag 映像名稱是 Kustomize 佔位名稱。

- [操作手冊](../docs/AIOPS_OPERATIONS.md)：環境前置、初始化、升級、修復開關與驗證命令。
- [Kubernetes 架構](../docs/K8S_INTEGRATION.md)：服務、權限、監控、兩個資料庫與 schema Job 的分工。
- [文件入口](../docs/README.md)：整體系統與使用文件。

根清單包含 DB、PVC 與一次性 schema Job，既有環境透過專用升級工具處理，不以直接套用完整根清單取代。影片 DB 只使用影片 schema 初始化；control DB 才執行完整 Alembic、checkpoint 與角色初始化。

首次從 GitHub 建置，請依 [操作手冊 1～10 步](../docs/AIOPS_OPERATIONS.md#首次安裝)建立叢集、儲存、監控及基礎影片平臺，再初始化 AIOps。不要把完整根清單當成無相依順序的首次安裝器。
