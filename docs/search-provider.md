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
