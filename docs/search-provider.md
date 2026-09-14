# Search Provider 配置

`internet_search`、`batch_search`、MCP Server 与 Research Worker 都继续走
`app.tools.tavily_core.search_internet()`。搜索后端通过配置切换，主流程不感知供应商差异。

## Bocha

```env
SEARCH_PROVIDER=bocha
BOCHA_API_KEY=你的_BOCHA_API_KEY
BOCHA_TIMEOUT_SEC=20
```

- API：`POST https://api.bocha.cn/v1/web-search`
- 鉴权：`Authorization: Bearer <BOCHA_API_KEY>`
- 适配器：`app/tools/bocha_provider.py`
- 返回结构：转换为现有 Tavily 兼容结构，字段包括 `query`、`results`、`response_time`。

## 失败语义

搜索是环境工具，不是控制面。Provider 调用失败时会做一次短间隔重试；两次都失败时返回结构化空结果：

```json
{
  "ok": false,
  "error": "search_failed",
  "results": [],
  "provider_error_code": "http_403",
  "provider_status_code": 403,
  "provider_error_message": "You do not have enough money or package quota"
}
```

该结果不会抛出异常打断 Worker。Worker 可以继续使用已抓取证据并进入 Finalization Mode；Coverage 与 Quality Gate 仍按真实证据判断，不会把搜索失败伪装成成功。
`provider_error_message` 来自供应商返回的安全摘要，并会先做密钥脱敏；它用于定位账户额度、鉴权或服务问题，不会进入研究事实。

`topic` 映射：

| 现有 topic | Bocha freshness |
| --- | --- |
| `news` | `oneWeek` |
| `finance` | `oneMonth` |
| `general` | `noLimit` |

## Tavily fallback

```env
SEARCH_PROVIDER=tavily
TAVILY_API_KEY=你的_TAVILY_API_KEY
TAVILY_TIMEOUT_SEC=20
```

不配置 `SEARCH_PROVIDER` 时默认使用 Tavily，保持既有部署兼容。
