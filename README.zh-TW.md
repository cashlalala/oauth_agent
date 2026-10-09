# A2A 學習專案:聊天 Agent 伺服器 + 用戶端 (Python)

[English](README.md) | **繁體中文**

這是一個**手寫**的 **A2A (Agent2Agent) 協定**核心實作,**沒有使用任何 A2A SDK**,
因此網路上傳輸的每一個位元組都能在程式碼中看見,很適合用來理解協定本身。
Agent 的「大腦」是 OpenAI 相容的用戶端(`openai` 函式庫),連接到 **OpenRouter**(`openrouter/free` 免費模型路由)。

## 快速開始 (Windows PowerShell)

```powershell
python -m venv .venv                  # 已建立
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# .env 內含 OPENROUTER_API_KEY(範例見 .env.example);此檔已被 git 忽略

python -m a2a_demo.server             # 終端機 1:Agent 伺服器 http://127.0.0.1:9999
python -m a2a_demo.client             # 終端機 2:互動式聊天(試試 /trace)
python demo.py                        # 終端機 2:逐步導覽所有協定功能
pytest                                # 離線測試(使用假的 LLM,不需網路)
```

### 環境變數 (`.env`)

| 變數 | 預設值 | 說明 |
|---|---|---|
| `OPENROUTER_API_KEY` | (必填) | OpenRouter 的 API 金鑰 |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI 相容端點 |
| `OPENROUTER_MODEL` | `openrouter/free` | 模型;`openrouter/free` 會自動路由到可用的免費模型 |
| `A2A_SERVER_HOST` | `127.0.0.1` | 伺服器監聽位址 |
| `A2A_SERVER_PORT` | `9999` | 伺服器監聽埠 |

## 專案結構

```
a2a_demo/
  protocol.py        所有 A2A 型別(Message、Task、Artifact、事件、AgentCard)+ JSON-RPC 工具與錯誤碼
  config.py          從 .env 讀取設定
  server/
    app.py           FastAPI:Agent Card 端點、JSON-RPC 分派、SSE 串流
    handler.py       協定邏輯:任務生命週期、message/send|stream、tasks/get|cancel
    store.py         記憶體中的任務、依 context 區分的對話歷史、EventBus
    llm.py           透過 openai.AsyncOpenAI 呼叫 OpenRouter(串流)
  client/
    client.py        A2AClient:探索、JSON-RPC 呼叫、SSE 解析
    cli.py           互動式聊天(/stream /async /task /cancel /trace /card /new)
demo.py              帶有線路層(wire-level)追蹤的腳本化導覽
tests/test_a2a.py    用戶端 <-> 伺服器測試(行程內、假 LLM)
```

建議閱讀順序:`protocol.py` → `server/handler.py` → `server/app.py` → `client/client.py` → `demo.py`。

## 什麼是 A2A?

A2A 是讓**不同的 Agent 之間互相溝通**的開放協定。一個 Agent 不需要知道另一個 Agent 內部如何實作
(用什麼框架、什麼模型),只要雙方都遵守 A2A,就能:

1. **探索**對方有什麼能力(Agent Card)
2. **派工**給對方(Message → Task)
3. **追蹤**進度與取得結果(Task 狀態、Artifact、串流事件)

本專案中:**伺服器**是一個提供聊天功能的 Agent,**用戶端**則是呼叫它的另一方。

## 驗證(OIDC)

A2A 協議本身沒有驗證訊息:驗證就是一般的 HTTP 驗證,並由 **Agent Card 告訴用戶端該怎麼做**。
伺服器沒設 `OIDC_ISSUER` 時不啟用驗證。

```
用戶端                         Agent (A2A 伺服器)                OIDC 提供者
  | GET /.well-known/agent-card.json(公開)    |                        |
  |<-- securitySchemes.oidc.openIdConnectUrl, security: [{oidc: [a2a.invoke]}]
  | GET openIdConnectUrl  --------------------------------------------->|  discovery
  | POST token_endpoint  grant_type=client_credentials  ---------------->|
  |<-- access_token (JWT) ----------------------------------------------|
  | POST /  Authorization: Bearer <jwt>  ----->|                        |
  |                                            | GET jwks_uri(快取)--->|
  |                                            | 驗簽章、iss、aud、exp、scope
  |<-- JSON-RPC 回應(或 HTTP 401/403 + WWW-Authenticate)               |
```

用 mock 提供者試試(需要 Docker):

```powershell
docker compose up -d                       # mock-oauth2-server 在 http://localhost:8080,issuer 為 .../a2a
$env:OIDC_ISSUER = "http://localhost:8080/a2a"
python -m a2a_demo.server                  # Agent Card 現在宣告 OIDC;POST / 需要 token
python -m a2a_demo.client --client-id agent-client --client-secret anything
python -m a2a_demo.client                  # 沒帶憑證 -> "auth error: HTTP 401 ... card security: ..."
python demo.py --client-id agent-client --client-secret anything
```

伺服器設定:`OIDC_ISSUER`(空 = 不驗證)、`OIDC_AUDIENCE`(預設 `a2a-agent`)、`OIDC_SCOPE`
(預設 `a2a.invoke`,比對 token 的 `scope` 或 `scp`)。用戶端:`--client-id/--client-secret/--scope`
(或環境變數 `OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET`),或用 `--token` / `A2A_TOKEN` 直接給現成 token。
`401` = 沒帶或無效的 token(用戶端會換新 token 重試一次),`403` = token 有效但缺少 scope。

關於 mock 提供者([mock-oidc/config.json](mock-oidc/config.json)):
- 它會簽出真正的 RS256 JWT,並提供 JWKS 與 OIDC discovery;但預設把 *scope* 放進 `aud`,且沒有 `scope` claim。
  設定檔針對 client-credentials 請求固定 `aud=a2a-agent`、`scope=a2a.invoke`,讓 Agent 的 audience 與 scope 檢查有意義。
- 它**不會**檢查 client secret,錯誤的 secret 一樣拿得到 token;secret 只有真正的提供者才會把關
  (tests/test_auth.py 用行程內的假 IdP 測了這條路徑)。
- `iss` claim 跟著你呼叫的 URL 走(`localhost:8080`),所以 `OIDC_ISSUER` 必須用同一個 host。

## 核心概念與對應程式碼

| 概念 | 說明 | 程式碼位置 |
|---|---|---|
| **Agent Card** | 位於 `/.well-known/agent-card.json` 的 JSON「名片」:名稱、端點 `url`、能力(`streaming`)、技能(skills)。用戶端靠它發現 Agent。 | `server/app.py::build_agent_card`、`client.get_agent_card` |
| **JSON-RPC 2.0** | 傳輸格式:`POST /`,內容 `{jsonrpc, id, method, params}`。錯誤也是 HTTP 200,內含 `error` 物件。 | `app.py::jsonrpc`、`client.rpc` |
| **Message** | 一個對話回合,`role` 為 `user` 或 `agent`,內容放在 `parts`(本專案實作 `TextPart`)。 | `protocol.Message` |
| **Task** | 具有 id 與**狀態機**的工作單元:`submitted → working → completed / failed / canceled`。 | `protocol.Task`、`handler._run` |
| **Artifact** | 任務的**產出物**(此處是回覆內容),與訊息歷史(history)分開存放。 | `protocol.Artifact` |
| **contextId** | 把多個任務歸為同一場對話。相同 `contextId` = Agent 記得先前內容;但每則訊息仍會產生**新的 task id**。 | `store.contexts`、demo 步驟 3 |
| **message/send** | 請求/回應。`blocking=true` 等到任務結束才回傳;`blocking=false` 立即回傳 `submitted` 的任務,由用戶端輪詢。 | `handler.message_send` |
| **message/stream** | 同樣的呼叫,但回應是 SSE 串流:`Task` → `status-update(working)` → 多個 `artifact-update`(`append`)→ `status-update(completed, final=true)`。 | `handler.message_stream`、`client.rpc_stream` |
| **tasks/get** | 查詢任務目前狀態(用於輪詢)。 | `handler.tasks_get` |
| **tasks/cancel** | 取消執行中的任務(取消 asyncio 工作,狀態變為 `canceled`)。 | `handler.tasks_cancel` |
| **錯誤碼** | JSON-RPC:`-32700/-32600/-32601/-32602/-32603`;A2A:`-32001 TaskNotFound`、`-32002 TaskNotCancelable`。 | `protocol.ErrorCode` |

### 任務狀態機

```
            ┌──────────► failed     (LLM / 網路錯誤)
submitted ─► working ──► completed
            └──────────► canceled   (tasks/cancel)
```

`completed`、`failed`、`canceled`、`rejected` 為**終止狀態**:終止後的任務不能再接收訊息或被取消
(分別回傳 `-32602`、`-32002`)。要延續對話,請**省略 `taskId`、沿用 `contextId`** 開新任務。

## 請求流程(串流模式)

```
用戶端                                   伺服器                          OpenRouter
  | GET /.well-known/agent-card.json ---->|
  |<-------------------------- AgentCard -|
  | POST / message/stream {message} ----->| 驗證 -> 建立 Task(submitted)
  |<-- SSE: Task(submitted) --------------| 啟動工作 (asyncio) ------------> chat.completions(stream)
  |<-- SSE: status-update working ---------|<------------------------------ delta "你"
  |<-- SSE: artifact-update "你" ----------|<------------------------------ delta "好"
  |<-- SSE: artifact-update "好" (append) -|
  |<-- SSE: status-update completed final -| 儲存 artifact + history + context 記憶
```

### 線路上的真實樣子

`message/send` 請求:

```json
{
  "jsonrpc": "2.0", "id": 1, "method": "message/send",
  "params": {
    "message": {
      "kind": "message", "messageId": "…", "role": "user",
      "parts": [{ "kind": "text", "text": "你好" }]
    },
    "configuration": { "blocking": true }
  }
}
```

回應(節錄):

```json
{
  "jsonrpc": "2.0", "id": 1,
  "result": {
    "kind": "task", "id": "<taskId>", "contextId": "<contextId>",
    "status": { "state": "completed", "message": { "role": "agent", "parts": [{ "kind": "text", "text": "你好!" }] } },
    "artifacts": [{ "artifactId": "…", "name": "reply", "parts": [{ "kind": "text", "text": "你好!" }] }],
    "history": [ /* user 與 agent 的訊息 */ ]
  }
}
```

想看完整流量,在聊天用戶端輸入 `/trace`,或直接執行 `python demo.py`。

## 設計重點:EventBus 解耦

「做事的工作」與「HTTP 請求」透過 **EventBus** 解耦:

- 工作(`handler._run`)一邊呼叫 LLM,一邊把事件**發佈**到 EventBus。
- `message/stream` 與阻塞式 `message/send` 會**訂閱**事件並取用。
- 非阻塞的 `message/send` **不訂閱**,工作自行繼續,用戶端之後用 `tasks/get` 讀取儲存的狀態。
- 因此你可以從**第二條連線**呼叫 `tasks/cancel`,中止第一條連線正在串流的任務(demo 步驟 6)。
- 一定要在工作啟動**之前**先訂閱,才不會漏掉事件。

## 互動式用戶端指令

| 指令 | 作用 |
|---|---|
| `/card` | 顯示 Agent Card |
| `/stream` | 切換串流(`message/stream`)與阻塞(`message/send`) |
| `/async <文字>` | 非阻塞送出,並輪詢 `tasks/get` 直到完成 |
| `/task [id]` | 對最近(或指定)任務呼叫 `tasks/get` |
| `/cancel [id]` | 對最近(或指定)任務呼叫 `tasks/cancel` |
| `/trace` | 開關原始 JSON-RPC 線路追蹤 |
| `/new` | 開新對話(新的 contextId) |
| `/quit` | 離開 |

## `demo.py` 導覽的七個步驟

1. **探索**:GET Agent Card
2. **message/send(阻塞)**:一次請求、一次回應,回覆在 Task 的 artifact 中
3. **contextId = 對話記憶**:沿用 contextId 開新任務,Agent 記得先前內容
4. **message/stream (SSE)**:觀察事件順序
5. **非阻塞送出 + 輪詢**:`blocking=false` 後以 `tasks/get` 輪詢
6. **tasks/cancel**:串流途中從另一個請求取消任務
7. **錯誤處理**:`-32001`、`-32002`、`-32602`、`-32601`

## 刻意簡化之處(下一步的好練習)

- 只支援 `TextPart`(規格另有 `FilePart`、`DataPart`)。
- 任務狀態存在記憶體,重啟即消失。可把 `TaskStore` 換成 SQLite/Redis。
- 沒有 `input-required` 流程、`tasks/resubscribe`、推播通知(push notification)、需驗證的擴充 Agent Card。
- 驗證只涵蓋機器對機器的 OIDC(client-credentials)。沒有使用者登入(authorization code + PKCE)、
  依技能區分的 scope、token introspection/撤銷。
- 只實作 JSON-RPC 傳輸(規格另有 gRPC 與 HTTP+JSON)。
- 進階挑戰:寫第二個 Agent,同時是本 Agent 的 A2A **用戶端**與自己的 A2A **伺服器**(Agent 串接)。

## 注意事項

- **金鑰安全**:OpenRouter 金鑰只放在 `.env`(已被 git 忽略),不在原始碼中。若金鑰曾以明文分享,
  建議到 https://openrouter.ai/keys 重新產生。
- `openrouter/free` 會路由到當下可用的免費模型,因此回覆內容會有差異,且免費模型可能被限流;
  發生時任務會以 `failed` 結束,錯誤訊息在 `status.message`。

## 疑難排解

| 現象 | 可能原因 / 處理 |
|---|---|
| 用戶端顯示 `cannot reach agent` | 伺服器沒啟動,或 `--url` / 埠號不一致 |
| 任務 `failed`,訊息含 `401` | `.env` 的 API 金鑰錯誤或已失效 |
| 任務 `failed`,訊息含 `429` | 免費模型被限流,稍後重試 |
| 任務 `failed`,訊息為 empty response | 模型回傳空內容,重送即可 |
| PowerShell 無法啟動 venv | 執行 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
